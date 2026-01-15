# iCloud Restore

**Restore deleted files from iCloud Drive when the web UI fails.**

```bash
uvx icloud-restore
```

> *iCloud.com's "Recently Deleted" page freezes, crashes, or shows a spinning wheel forever? This tool fixes that.*

## The Problem

iCloud.com's "Restore Files" page crashes or hangs when you have a large number of deleted files (10k+). Symptoms:

- Page shows spinning wheel forever
- Browser tab crashes or becomes unresponsive
- "Restore All" button never appears
- Page loads partially then freezes
- Safari/Chrome runs out of memory

This happens because the web UI tries to render all deleted files at once before letting you restore them. Apple hasn't fixed this bug for years.

## The Solution

This tool bypasses the broken web UI by using Apple's API directly. It:

1. Opens your Chrome browser to iCloud's recovery page
2. You log in normally (with Keychain autofill, 2FA, etc.)
3. Fetches the list of deleted files via API
4. Restores them in batches
5. Auto-refreshes credentials when they expire (long restores can take hours)

## Installation

```bash
# Using uv (recommended)
uvx icloud-restore

# Using pipx
pipx run icloud-restore

# Or install globally
pip install icloud-restore
```

## Usage

Just run the command:

```bash
icloud-restore
```

Your Chrome browser will open to iCloud's recovery page. Log in with your Apple ID (Keychain autofill works!), then the tool will:

1. Detect your login
2. Fetch the list of deleted files
3. Ask for confirmation
4. Restore all files

### Progress & Resume

The tool saves progress to local files. If interrupted (Ctrl+C, crash, etc.), just run it again to resume where you left off.

Progress files:
- `icloud_restore_checkpoint.json` - Tracks file list fetching
- `icloud_restore_progress.json` - Tracks restore progress

### Long Restores

For large restores (100k+ files), the process can take several hours. The tool will:

- Keep the browser open throughout
- Auto-refresh credentials when they expire (~every hour)
- Save progress periodically

You can leave it running unattended.

## How It Works

1. **Browser Login**: Launches Chrome with a fresh profile (macOS Keychain still works for autofill)
2. **Credential Capture**: Watches network requests to capture your session credentials
3. **API Calls**: Uses the same API endpoints that the web UI would use
4. **Batch Processing**: Restores files in batches with retries and rate limiting
5. **Auto-Refresh**: When credentials expire, reloads the browser page to get fresh ones

## Security

- **Local only**: Your credentials never leave your machine
- **No storage**: Cookies are not saved to disk (only held in memory during the session)
- **Keychain works**: Fresh Chrome profile still has access to macOS Keychain for password autofill
- **Open source**: Review the code yourself

## Requirements

- Python 3.10+
- Google Chrome installed

## Troubleshooting

### "Login not detected"

Make sure you complete the full login flow including any 2FA prompts. Wait a few seconds after logging in for the tool to detect it.

### "Auth expired" errors

The tool should handle this automatically by refreshing credentials. If it keeps failing, try closing the tool and running it again.

### Some files failed to restore

This can happen if Apple's servers are overloaded. Run the tool again - it will only retry the failed files.

## License

MIT
