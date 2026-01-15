"""Command-line interface for iCloud Drive file restore."""

import asyncio
import sys
from pathlib import Path

from .browser import ICloudBrowser
from .api import fetch_deleted_files, restore_files, AuthExpiredError


async def async_main() -> int:
    """Main async entry point."""
    print("=" * 50)
    print("  iCloud Drive File Restore")
    print("=" * 50)
    print()
    print("This tool restores deleted files from iCloud Drive.")
    print("It bypasses the web UI which crashes with large file counts.")
    print()

    browser = ICloudBrowser()

    try:
        # Connect to Chrome (or launch it if not running)
        print("Opening iCloud recovery page...")
        if not await browser.connect():
            print()
            print("Could not launch Chrome.")
            print("Please ensure Google Chrome is installed.")
            print()
            return 1

        # Wait for user to log in
        print("-" * 50)
        print("A browser window has opened to iCloud.")
        print("Please log in with your Apple ID.")
        print("-" * 50)

        try:
            creds = await browser.wait_for_login(timeout=300)
        except TimeoutError:
            print("\nLogin timed out after 5 minutes.")
            print("Please run the tool again and complete login.")
            return 1

        print()

        # Fetch deleted files
        print("-" * 50)
        print("Fetching deleted files list...")
        print("-" * 50)

        checkpoint_file = Path("icloud_restore_checkpoint.json")

        try:
            item_ids = await fetch_deleted_files(creds, checkpoint_file)
        except AuthExpiredError:
            # Try refreshing credentials once
            print("Session expired during fetch, refreshing...")
            creds = await browser.refresh_credentials()
            item_ids = await fetch_deleted_files(creds, checkpoint_file)

        if not item_ids:
            print("\nNo deleted files found!")
            print("Your iCloud Drive trash is empty.")
            return 0

        print(f"\nFound {len(item_ids):,} deleted files.")
        print()

        # Confirm restore
        print("-" * 50)
        print(f"Ready to restore {len(item_ids):,} files.")
        print("-" * 50)
        print()
        print("Press Enter to start restore, or Ctrl+C to cancel...")

        try:
            input()
        except KeyboardInterrupt:
            print("\nCancelled.")
            return 0

        # Restore files
        print("-" * 50)
        print("Restoring files...")
        print("-" * 50)

        progress_file = Path("icloud_restore_progress.json")

        async def on_auth_expired():
            return await browser.refresh_credentials()

        stats = await restore_files(
            creds,
            item_ids,
            on_auth_expired=on_auth_expired,
            progress_file=progress_file,
        )

        # Summary
        print()
        print("=" * 50)
        print("  Complete!")
        print("=" * 50)
        print(f"  Restored: {stats.restored:,}")
        print(f"  Failed:   {stats.failed:,}")
        print()

        if stats.failed > 0:
            print(f"{stats.failed} files failed to restore.")
            print("Run the tool again to retry failed files.")

        # Clean up checkpoint on success
        if stats.failed == 0 and checkpoint_file.exists():
            checkpoint_file.unlink()
            print("Checkpoint file cleaned up.")

        return 0 if stats.failed == 0 else 1

    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        print("Progress has been saved. Run again to resume.")
        return 130

    except Exception as e:
        print(f"\nError: {e}")
        return 1

    finally:
        await browser.close()


def main() -> None:
    """Entry point for the CLI."""
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
