"""Browser helpers for iCloud authentication via Chrome DevTools Protocol."""

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, Browser, Page, BrowserContext


ICLOUD_RECOVERY_URL = "https://www.icloud.com/recovery/"
CHROME_DEBUG_URL = "http://127.0.0.1:9222"


@dataclass
class Credentials:
    """iCloud API credentials extracted from browser."""
    cookies: str  # Cookie header string
    client_id: str
    dsid: str
    client_build_number: str = "2546Build54"
    client_mastering_number: str = "2546Build54"


def _get_chrome_path() -> str | None:
    """Get path to Chrome on this system."""
    if sys.platform == "darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif sys.platform == "win32":
        import os
        paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in paths:
            if os.path.exists(p):
                return p
    else:  # Linux
        for p in ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
            import os
            if os.path.exists(p):
                return p
    return None


def _is_chrome_running() -> bool:
    """Check if Chrome is already running (without debugging)."""
    import os
    if sys.platform == "darwin":
        result = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True)
        return result.returncode == 0
    elif sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
            capture_output=True,
            text=True,
        )
        return "chrome.exe" in result.stdout.lower()
    else:  # Linux
        result = subprocess.run(["pgrep", "-x", "chrome"], capture_output=True)
        if result.returncode != 0:
            result = subprocess.run(["pgrep", "-x", "chromium"], capture_output=True)
        return result.returncode == 0


def _is_port_open(port: int) -> bool:
    """Check if a port is open."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch_chrome_with_debugging() -> str | None:
    """Launch Chrome with remote debugging enabled using a fresh temp profile.

    Chrome requires a non-default user-data-dir for remote debugging.
    Keychain autofill still works since it's a system-level feature.

    Returns:
        Path to temp profile directory if launched (caller should clean up), None if failed
    """
    import tempfile

    chrome_path = _get_chrome_path()
    if not chrome_path:
        return None

    # Create temp profile - Chrome requires non-default dir for debugging
    temp_profile = tempfile.mkdtemp(prefix="icloud-restore-")

    try:
        subprocess.Popen(
            [
                chrome_path,
                "--remote-debugging-port=9222",
                f"--user-data-dir={temp_profile}",
                "--no-first-run",
                ICLOUD_RECOVERY_URL,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return temp_profile
    except Exception:
        import shutil
        shutil.rmtree(temp_profile, ignore_errors=True)
        return None


class ICloudBrowser:
    """Connects to Chrome via CDP for iCloud authentication."""

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._credentials: Credentials | None = None
        self._login_event = asyncio.Event()
        self._temp_profile: str | None = None

    async def connect(self) -> bool:
        """Launch Chrome and connect via remote debugging.

        Returns:
            True if connected successfully, False otherwise
        """
        self._playwright = await async_playwright().start()

        # Try to connect to existing Chrome with debugging first
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(CHROME_DEBUG_URL)
        except Exception:
            # Launch Chrome with fresh temp profile
            print("Launching Chrome...")
            self._temp_profile = launch_chrome_with_debugging()
            if not self._temp_profile:
                return False

            # Wait for Chrome to start with retries
            for i in range(5):
                await asyncio.sleep(1 + i)  # 1, 2, 3, 4, 5 seconds
                if _is_port_open(9222):
                    break
            else:
                return False

            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(CHROME_DEBUG_URL)
            except Exception:
                return False

        # Get the first context and page
        contexts = self._browser.contexts
        if not contexts:
            return False

        self._context = contexts[0]
        pages = self._context.pages

        if not pages:
            # Create a new page if none exist
            self._page = await self._context.new_page()
            await self._page.goto(ICLOUD_RECOVERY_URL)
        else:
            self._page = pages[0]
            # Navigate to iCloud if not already there
            if "icloud.com" not in self._page.url:
                await self._page.goto(ICLOUD_RECOVERY_URL)

        # Set up request listener to detect login
        self._page.on("request", self._handle_request)

        print(f"Connected to Chrome: {self._page.url}")
        return True

    def _handle_request(self, request) -> None:
        """Watch for iCloud API requests that indicate successful login."""
        url = request.url

        # Look for authenticated API requests (contain clientId and dsid)
        if "icloud.com" in url and "clientId=" in url and "dsid=" in url:
            params = parse_qs(urlparse(url).query)

            client_id = params.get("clientId", [None])[0]
            dsid = params.get("dsid", [None])[0]
            build = params.get("clientBuildNumber", ["2546Build54"])[0]
            mastering = params.get("clientMasteringNumber", ["2546Build54"])[0]

            if client_id and dsid and not self._login_event.is_set():
                # Store partial credentials (cookies added later)
                self._credentials = Credentials(
                    cookies="",  # Will be populated after
                    client_id=client_id,
                    dsid=dsid,
                    client_build_number=build,
                    client_mastering_number=mastering,
                )
                self._login_event.set()

    async def wait_for_login(self, timeout: float = 300) -> Credentials:
        """Wait for user to log in and return credentials.

        Args:
            timeout: Maximum seconds to wait for login (default 5 minutes)

        Returns:
            Credentials object with cookies, clientId, dsid, etc.

        Raises:
            TimeoutError: If login not detected within timeout
        """
        print("\nWaiting for you to log in...")
        print("(Complete login including any 2FA, then wait a moment)\n")

        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Login not detected within {timeout} seconds")

        # Extract cookies from browser context
        await self._extract_cookies()

        print(f"Login detected!")
        print(f"  dsid: {self._credentials.dsid}")
        print(f"  clientId: {self._credentials.client_id[:20]}...")

        return self._credentials

    async def _extract_cookies(self) -> None:
        """Extract cookies from browser context and update credentials."""
        browser_cookies = await self._context.cookies(["https://www.icloud.com"])
        cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in browser_cookies)

        if self._credentials:
            self._credentials.cookies = cookie_string

    async def refresh_credentials(self) -> Credentials:
        """Reload page to get fresh credentials.

        Call this when you get auth errors (401, 403, 421).

        Returns:
            Fresh Credentials object
        """
        print("\n  Refreshing credentials...")

        # Reset login detection
        self._login_event.clear()
        self._credentials = None

        # Reload page to trigger fresh API requests
        await self._page.reload(wait_until="domcontentloaded")

        # Wait for API requests with credentials
        try:
            await asyncio.wait_for(self._login_event.wait(), timeout=30)
        except asyncio.TimeoutError:
            raise TimeoutError("Failed to refresh credentials - no API requests detected")

        # Extract fresh cookies
        await self._extract_cookies()

        print(f"  Credentials refreshed (clientId: {self._credentials.client_id[:20]}...)")
        return self._credentials

    @property
    def credentials(self) -> Credentials | None:
        """Current credentials, if available."""
        return self._credentials

    async def close(self) -> None:
        """Close the browser connection and clean up temp profile."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

        # Clean up temp profile directory
        if self._temp_profile:
            import shutil
            shutil.rmtree(self._temp_profile, ignore_errors=True)
            self._temp_profile = None
