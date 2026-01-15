"""iCloud Drive API client for fetching and restoring deleted files."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable
from urllib.parse import urlencode

import httpx

from .browser import Credentials


# API endpoint (p107 is one of Apple's server pools)
BASE_URL = "https://p107-docws.icloud.com"

# Tuning parameters
RESTORE_BATCH_SIZE = 100   # Files per restore request
CONCURRENT_RESTORES = 5    # Parallel restore requests
FETCH_PAGE_SIZE = 2000     # Files per list request
MAX_RETRIES = 5            # Retries per batch
RETRY_DELAY = 2            # Base delay (exponential backoff)


class AuthExpiredError(Exception):
    """Raised when authentication has expired and needs refresh."""
    pass


@dataclass
class RestoreStats:
    """Statistics from a restore operation."""
    restored: int = 0
    failed: int = 0
    failed_ids: list = None

    def __post_init__(self):
        if self.failed_ids is None:
            self.failed_ids = []


def _get_headers() -> dict:
    """Standard headers for iCloud API requests."""
    return {
        'Accept': '*/*',
        'Content-Type': 'text/plain',
        'Origin': 'https://www.icloud.com',
        'Referer': 'https://www.icloud.com/',
    }


def _get_params(creds: Credentials) -> dict:
    """Build query params from credentials."""
    return {
        "clientBuildNumber": creds.client_build_number,
        "clientMasteringNumber": creds.client_mastering_number,
        "clientId": creds.client_id,
        "dsid": creds.dsid,
    }


def _parse_cookies(cookie_string: str) -> dict:
    """Parse cookie header string into dict for httpx."""
    cookies = {}
    for item in cookie_string.split('; '):
        if '=' in item:
            key, value = item.split('=', 1)
            cookies[key] = value.strip('"')
    return cookies


async def fetch_deleted_files(
    creds: Credentials,
    checkpoint_file: Path = Path("icloud_restore_checkpoint.json"),
) -> list[str]:
    """Fetch all deleted file IDs from iCloud.

    Args:
        creds: Authentication credentials
        checkpoint_file: File to save/resume progress

    Returns:
        List of item IDs for deleted files

    Raises:
        AuthExpiredError: If authentication has expired
    """
    all_item_ids = []
    continuation_marker = None
    page = 0

    # Try to resume from checkpoint
    if checkpoint_file.exists():
        try:
            checkpoint = json.loads(checkpoint_file.read_text())
            all_item_ids = checkpoint.get("item_ids", [])
            continuation_marker = checkpoint.get("continuation_marker")
            page = checkpoint.get("page", 0)
            if all_item_ids:
                print(f"  Resuming from checkpoint: {len(all_item_ids)} IDs, page {page}")
        except (json.JSONDecodeError, KeyError):
            pass

    cookies = _parse_cookies(creds.cookies)

    async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as client:
        while True:
            page += 1
            params = {
                **_get_params(creds),
                "limit": str(FETCH_PAGE_SIZE),
                "unified_format": "true",
            }
            if continuation_marker:
                params["nextPage"] = continuation_marker

            url = f"{BASE_URL}/ws/_all_/list/enumerate/tombstones?{urlencode(params)}"

            print(f"  Page {page}...", end=" ", flush=True)

            try:
                response = await client.get(url, headers=_get_headers())
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403, 421):
                    raise AuthExpiredError(f"Auth expired (HTTP {e.response.status_code})")
                raise

            data = response.json()
            documents = data.get("documents", [])
            item_ids = [doc["item_id"] for doc in documents if "item_id" in doc]
            all_item_ids.extend(item_ids)

            print(f"{len(documents)} files (total: {len(all_item_ids)})")

            # Save checkpoint
            continuation_marker = data.get("continuationMarker")
            checkpoint_file.write_text(json.dumps({
                "item_ids": all_item_ids,
                "continuation_marker": continuation_marker,
                "page": page,
            }))

            if data.get("status") != "MORE_AVAILABLE" or not continuation_marker:
                break

    return all_item_ids


async def restore_files(
    creds: Credentials,
    item_ids: list[str],
    on_auth_expired: Callable[[], Awaitable[Credentials]],
    progress_file: Path = Path("icloud_restore_progress.json"),
) -> RestoreStats:
    """Restore deleted files.

    Args:
        creds: Authentication credentials
        item_ids: List of file IDs to restore
        on_auth_expired: Async callback to refresh credentials when expired
        progress_file: File to save/resume progress

    Returns:
        RestoreStats with counts of restored/failed files
    """
    # Load previous progress
    restored_ids = []
    failed_ids = []

    if progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text())
            restored_ids = progress.get("restored_ids", [])
            failed_ids = progress.get("failed_ids", [])
            if restored_ids:
                print(f"  Resuming: {len(restored_ids)} already restored")
        except (json.JSONDecodeError, KeyError):
            pass

    # Filter out already restored
    restored_set = set(restored_ids)
    remaining_ids = [id for id in item_ids if id not in restored_set]

    if len(remaining_ids) < len(item_ids):
        print(f"  Skipping {len(item_ids) - len(remaining_ids)} already restored files")

    if not remaining_ids:
        return RestoreStats(restored=len(restored_ids))

    batches = [
        remaining_ids[i:i + RESTORE_BATCH_SIZE]
        for i in range(0, len(remaining_ids), RESTORE_BATCH_SIZE)
    ]
    total_batches = len(batches)

    print(f"\nRestoring {len(remaining_ids)} files in {total_batches} batches...\n")

    stats = RestoreStats()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(CONCURRENT_RESTORES)
    current_creds = creds
    save_counter = 0
    start_time = time.time()

    async def restore_batch(batch: list[str], batch_num: int) -> None:
        nonlocal current_creds, save_counter

        async with semaphore:
            cookies = _parse_cookies(current_creds.cookies)

            async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as client:
                url = f"{BASE_URL}/v1/items?{urlencode(_get_params(current_creds))}"
                payload = {
                    "drive_item_update_request": {"is_recover": "true"},
                    "item_ids": batch,
                }

                completed = False
                for attempt in range(MAX_RETRIES):
                    try:
                        response = await client.put(
                            url, headers=_get_headers(), json=payload
                        )
                        response.raise_for_status()

                        # Check response body for errors
                        data = response.json()
                        items_status = data.get("drive_items_with_status", [])

                        if items_status:
                            status_code = items_status[0].get("status_code", "200")
                            status_msg = items_status[0].get("status_message", "")

                            if str(status_code) != "200":
                                if attempt < MAX_RETRIES - 1:
                                    delay = RETRY_DELAY * (2 ** attempt)
                                    print(f"  Batch {batch_num}/{total_batches}: {status_code} - retry in {delay}s")
                                    await asyncio.sleep(delay)
                                    continue
                                else:
                                    stats.failed += len(batch)
                                    stats.failed_ids.extend(batch)
                                    print(f"  Batch {batch_num}/{total_batches}: FAILED - {status_msg[:50]}")
                                    return

                        # Success
                        stats.restored += len(batch)
                        async with lock:
                            restored_ids.extend(batch)
                        print(f"  Batch {batch_num}/{total_batches}: OK ({stats.restored} restored)")
                        completed = True
                        break

                    except httpx.HTTPStatusError as e:
                        if e.response.status_code in (401, 403, 421):
                            print(f"\n  Auth expired (HTTP {e.response.status_code})")
                            # Refresh credentials
                            current_creds = await on_auth_expired()
                            # Rebuild client with new cookies
                            cookies = _parse_cookies(current_creds.cookies)
                            client.cookies.clear()
                            client.cookies.update(cookies)
                            url = f"{BASE_URL}/v1/items?{urlencode(_get_params(current_creds))}"
                            continue

                        if attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAY * (2 ** attempt)
                            print(f"  Batch {batch_num}/{total_batches}: HTTP {e.response.status_code} - retry in {delay}s")
                            await asyncio.sleep(delay)
                            continue

                        stats.failed += len(batch)
                        stats.failed_ids.extend(batch)
                        print(f"  Batch {batch_num}/{total_batches}: FAILED - HTTP {e.response.status_code}")
                        return

                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAY * (2 ** attempt)
                            print(f"  Batch {batch_num}/{total_batches}: {type(e).__name__} - retry in {delay}s")
                            await asyncio.sleep(delay)
                            continue

                        stats.failed += len(batch)
                        stats.failed_ids.extend(batch)
                        print(f"  Batch {batch_num}/{total_batches}: FAILED - {e}")
                        return

                if not completed:
                    stats.failed += len(batch)
                    stats.failed_ids.extend(batch)
                    print(f"  Batch {batch_num}/{total_batches}: FAILED - auth refresh exhausted")
                    return

            # Save progress periodically
            save_counter += 1
            if save_counter % 20 == 0:
                async with lock:
                    progress_file.write_text(json.dumps({
                        "restored_ids": restored_ids,
                        "failed_ids": stats.failed_ids,
                    }))

                    elapsed = time.time() - start_time
                    total_done = stats.restored + stats.failed
                    if total_done > 0 and elapsed > 0:
                        rate = total_done / elapsed
                        remaining = len(remaining_ids) - total_done
                        eta_min = (remaining / rate) / 60 if rate > 0 else 0
                        pct = total_done / len(remaining_ids) * 100

                        print(f"\n  === Progress: {pct:.1f}% | "
                              f"Restored: {stats.restored} | "
                              f"Failed: {stats.failed} | "
                              f"ETA: {eta_min:.1f}min ===\n")

    # Run all batches
    tasks = [restore_batch(batch, i + 1) for i, batch in enumerate(batches)]
    await asyncio.gather(*tasks)

    # Final save
    progress_file.write_text(json.dumps({
        "restored_ids": restored_ids,
        "failed_ids": stats.failed_ids,
    }))

    return stats
