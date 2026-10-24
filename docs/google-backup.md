# Google Sheets history backup

The optional `google_backup` worker configuration mirrors confirmed game history
into an existing Google spreadsheet, on the tab `История игр`. It polls committed
SQLite history every 10 seconds and writes only when the content changes.
The first successful run uploads all existing games. Two votes are still required;
drafts and the first vote alone never enter the history copy.

Each row preserves the game ID, Moscow timestamp, photo ID and ordinal, players
and their IDs, scores, winner, full-precision Elo before/after/deltas, active or
cancelled status, original Unix timestamp and source Telegram message link.
Cancelled games remain visible as `Отменена`. Late confirmations and corrections
refresh chronology and recalculated ratings. This is a game-history export,
not a complete replacement for the SQLite backups (jobs, votes, raw OCR and
credentials are not exported). Edits made in Google do not change bot results;
the history tab is managed by the bot and may be overwritten on the next sync.

## Setup

1. Create/import the initial history workbook in the owner's Google Drive.
2. Give the backup service account editor access to this spreadsheet only.
   Use its `client_email` from the private credential file. No public link sharing
   is required. The Sheets API must be enabled for the service account project.
3. Deploy the code, then enable the integration from an authorized workstation:

```bash
.venv/bin/python scripts/install_sheets_backup.py \
  --spreadsheet-id SPREADSHEET_ID \
  --credentials /path/to/private/service-account.json
```

The installer checks read access locally and from the VM, preserves the existing
worker configuration, encrypts the Google key with systemd-creds and adds a
separate `google-backup.json` runtime credential. It then restarts the worker.
The key never goes into Git, code workspaces or command arguments.
The worker discovers the tab by the configured numeric ID and also verifies its
expected title, rather than silently creating another tab if it disappears.

## Delivery and recovery

The snapshot is read within one SQLite transaction. One atomic Sheets
`batchUpdate` replaces the owned cells and extends the grid when needed. There
is no separate destructive clear step and no append that could duplicate rows
after a lost HTTP response. Values are typed explicitly, so player names cannot
become formulas. Number formats preserve full underlying precision.

After success the worker atomically saves `google-backup-state.json` beside the
database with target, fingerprint, exported game IDs, count and last-success
timestamp. Failures leave that checkpoint unchanged and retry with backoff up to
five minutes; a later attempt includes any newer confirmed games. Restarting the
worker resumes this process. Missing previously exported game IDs stop an
overwrite, protecting the sheet if a stale or empty database is restored.
Review the history before manually resetting that checkpoint.

The background thread does not block Telegram or send routine chat messages.
Inspect `journalctl -u ttar-worker` for `Google backup synchronized` or
`Google backup failed`. Google HTTP status codes are logged without response
bodies or credentials. Check first synchronization and the remote row count
before calling setup complete. To disable future synchronization, remove
`google_backup` from `/etc/ttar/config.json` and restart the worker; the remote
spreadsheet remains intact.

Google API references:
[atomic batch updates](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate),
[UpdateCellsRequest](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#UpdateCellsRequest),
[service account authentication](https://developers.google.com/identity/protocols/oauth2/service-account).
