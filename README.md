# Refunnel Rights & Payments Sync

Pulls creator content + usage-rights status + payment history out of
Refunnel (app.refunnel.com) and syncs it into a Google Sheet with 5 tabs:
Master Data, Usage Rights - Approved, Usage Rights - Requested,
Usage Rights - Declined, Payments.

Runs daily via GitHub Actions, across **4 Refunnel workspaces**
(Duderobe, Swoveralls, Defi Snacks, Kelson) -- each gets its own Google
Sheet. Only **Duderobe** runs on the automatic schedule right now; the
other 3 are built and ready but scheduled runs are switched off for them
(toggle in `config/workspaces.yaml`, see below). Any workspace can always
be run manually regardless of that setting.

## Layout

```
refunnel-sync/
    parse_refunnel.py
    sheets_sync.py
    gmail_otp.py
    refunnel_auth.py
    refunnel_export.py
    run_daily_sync.py
    build_preview_workbook.py
    conftest.py              <- lets tests/ import the source files above
    requirements.txt
    README.md
    config/
        workspaces.yaml      <- the on/off switch for scheduled runs, per workspace
    scripts/
        determine_workspaces.py   <- picks which workspace(s) a run should sync
    tests/
        test_parse_refunnel.py
        test_sheets_sync.py
        test_gmail_otp.py
        test_refunnel_export.py
        test_determine_workspaces.py
        fixtures/
            sample_media.csv
            sample_payments.csv
    .github/workflows/refunnel-sync.yml
```

## Files

| File | What it does | Tested how |
|---|---|---|
| `parse_refunnel.py` | Turns the 2 CSVs into 5 tabs' worth of rows, keyed by id | 18 automated tests against your real sample CSVs |
| `sheets_sync.py` | Rewrites each Sheet tab from parsed data, preserving manual columns | 8 automated tests against an in-memory fake Sheet |
| `gmail_otp.py` | Fetches a Refunnel login code from Gmail (fallback path only) | 7 automated tests against a fake Gmail client |
| `refunnel_auth.py` | Saved-session reuse + fresh OTP login via Gmail fallback | **Not run against the live site** -- see below |
| `refunnel_export.py` | Workspace switching, scroll-to-load-all, CSV export triggers, (optional) email scrape | Switching/scroll logic has 11 automated tests against fakes; the actual browser clicks are **not run against the live site** |
| `scripts/determine_workspaces.py` | Decides which workspace(s) a run should sync, from `config/workspaces.yaml` + trigger type | 10 automated tests |
| `run_daily_sync.py` | Orchestrates the above into one workspace's run | Syntax-checked only |
| `build_preview_workbook.py` | One-off: builds a local xlsx to eyeball tab structure before wiring up live Sheets | Run, produced `refunnel_sync_preview.xlsx` |
| `.github/workflows/refunnel-sync.yml` | Daily schedule + matrix over workspaces | Not run |

Run all automated tests (from the repo root): `python -m pytest -v`
Lint everything: `python -m pyflakes *.py scripts/*.py tests/*.py`
(Both were run before delivery -- 54 tests pass, no lint warnings.)

## Multi-workspace setup: `config/workspaces.yaml`

This file is the single place that controls which of your 4 Refunnel
workspaces get scheduled runs:

```yaml
workspaces:
  - name: Duderobe
    refunnel_workspace_name: Duderobe
    spreadsheet_id_secret: SPREADSHEET_ID_DUDEROBE
    schedule_enabled: true       # <- only this one runs automatically
  - name: Swoveralls
    ...
    schedule_enabled: false
```

To turn scheduled runs on for another brand, flip its `schedule_enabled`
to `true` and commit -- no code changes needed. Any workspace can still
be run **manually** any time regardless of this flag, from the Actions
tab: Actions -> Refunnel Sync -> Run workflow -> type the workspace name
(or `all` to run every workspace once, ignoring the flags).

Each workspace needs its own Google Sheet and its own
`spreadsheet_id_secret` (see setup checklist below) -- they're kept
completely separate, nothing is shared between brands' sheets.

## Setup checklist -- what I need from you

Everything here is a one-time setup step. Once done, the daily schedule
just runs.

**1. A Google Sheet per workspace you want to sync** (at minimum,
Duderobe). The script creates the 5 tabs itself on first run if they
don't exist -- you just need each Sheet's ID (the long string in its
URL).

**2. A Google service account** (one, shared across all workspaces) for
writing to Sheets:
   - Google Cloud Console -> create a service account -> enable the
     Sheets API for that project -> download the service account's JSON
     key.
   - Share **each** workspace's Google Sheet with that service account's
     email address (Editor access) -- it needs to be added to every
     sheet individually.
   - The JSON key's full file content becomes the
     `GOOGLE_SERVICE_ACCOUNT_JSON` secret.

**3. Gmail OAuth credentials** (for the OTP-login fallback only, shared
across all workspaces since it's the same Refunnel account):
   - Enable the Gmail API on a Google Cloud project (can be the same
     project as the service account, or a separate one).
   - Create OAuth 2.0 credentials (Desktop app type) and run the
     standard one-time consent flow to get a refresh token with
     `gmail.readonly` scope.
   - Store as `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`,
     `GMAIL_REFRESH_TOKEN`.

**4. An initial Refunnel login session** -- run this once, locally, on a
machine with a display:
   ```
   python -c "import refunnel_auth; refunnel_auth.capture_session_interactive()"
   ```
   A real browser opens. Log in yourself (enter your email, get the code
   from your own inbox, paste it, get to your dashboard). Press Enter in
   the terminal once you're in -- this writes `refunnel_session.json`.

   Base64-encode it for the bootstrap secret:
   ```
   base64 -w0 refunnel_session.json
   ```
   That becomes the `INITIAL_REFUNNEL_SESSION_B64` secret -- only needed
   once; after the first successful run, the workflow's own cache takes
   over automatically. **Never commit `refunnel_session.json` to the
   repo** -- it's a login credential.

**5. All GitHub secrets to set** (repo Settings -> Secrets and variables
-> Actions):

| Secret | Used for | How many |
|---|---|---|
| `REFUNNEL_EMAIL` | your Refunnel login email | 1 (shared) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | writing to all Sheets | 1 (shared) |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` | OTP fallback | 1 each (shared) |
| `INITIAL_REFUNNEL_SESSION_B64` | bootstrapping the first run | 1 (shared, one-time) |
| `SPREADSHEET_ID_DUDEROBE` | Duderobe's target Sheet | 1 |
| `SPREADSHEET_ID_SWOVERALLS` | Swoveralls' target Sheet | 1 (only needed once you enable/run that workspace) |
| `SPREADSHEET_ID_DEFI_SNACKS` | Defi Snacks' target Sheet | 1 (same) |
| `SPREADSHEET_ID_KELSON` | Kelson's target Sheet | 1 (same) |
| `SLACK_WEBHOOK_URL` | optional failure alerts | 1 (optional) |

You don't need the other 3 `SPREADSHEET_ID_*` secrets set up right away
-- only Duderobe's is required to get the schedule running. Add the
others whenever you're ready to bring that workspace online (manually or
by flipping its `schedule_enabled`).

**No Refunnel API key is needed anywhere** -- there isn't one; everything
goes through the browser-automation + CSV export path described below.

## What's genuinely unverified -- please test before trusting the schedule

I don't have network access to app.refunnel.com or Google's APIs from my
sandbox, so none of the following has actually been run against the real
site. Please work through this checklist once real credentials exist:

- [ ] **Login selectors** (`refunnel_auth.py` `SELECTORS` dict): guessed
  at the email input / send-code button / code input / submit button
  based on your description of the flow, not the real page. This mostly
  matters for the **automated** fallback path (`_perform_login`) -- test
  it by deliberately deleting `refunnel_session.json` and running
  `run_daily_sync.py` once to confirm the fallback actually logs in.
- [x] **Media export button**: confirmed from a real screenshot -- the
  '...' menu next to 'Create collection' on the Social Listening page,
  then 'Export Content CSV'. The menu item text is solid; the '...'
  button itself is still located by screen position (`:left-of()`) since
  it has no visible label, so it's worth a quick manual check.
- [ ] **Workspace switcher** (`select_workspace` in `refunnel_export.py`):
  built from your screenshot of the *opened* dropdown (search box +
  workspace list), but I've never seen the collapsed trigger's actual
  markup -- it's located by clicking whichever known workspace name is
  currently showing at the top of the page. Test by running against a
  non-default workspace (e.g. Swoveralls) and confirming it actually
  switches before the export runs.
- [ ] **Scroll-to-load-all counter text** (`count_text_pattern` in
  `scroll_to_load_all`): based on the "80 of 2078 media" / "20 of 2078
  media" text visible in your screenshots. Confirm the pattern still
  matches on the live page.
- [ ] **Email scraping** (`scrape_creator_emails` in `refunnel_export.py`):
  left as a stub on purpose -- I have no visibility into how you get from
  the content grid to a specific post's "Request usage rights" modal.
  `SCRAPE_EMAILS_ENABLED = False` by default; don't turn it on until
  you've filled in real selectors and manually confirmed it only ever
  reads the email field and closes via X, never touching "Send request".
  Use `playwright codegen https://app.refunnel.com` to record the real
  clicks and copy the selectors it captures.
- [ ] **A full end-to-end run for Duderobe**: once the above are fixed,
  trigger the workflow manually once (Actions tab -> Run workflow,
  workspace = Duderobe) and check the Sheet updates correctly before
  letting the schedule run unattended.
- [ ] **One manual run for a second workspace** (e.g. Swoveralls), to
  confirm the workspace switcher and per-workspace Sheet routing both
  work correctly, before ever flipping its `schedule_enabled`.

## Testing the Gmail OTP fallback manually

```
python -c "
import time, gmail_otp
print(gmail_otp.fetch_latest_code(requested_after_ts=time.time()-60, max_wait_seconds=90))
"
```
Trigger a real Refunnel login code to your inbox first, then run this
within a couple minutes -- it should print the code it found.

## Notes on design choices

- **Dedup key**: `id` (e.g. `tk_7681495537484369165`) -- confirmed unique
  per post even when creators repeat across multiple videos.
- **Usage Rights tabs are rewritten wholesale each run**, not
  incrementally patched -- simpler and self-correcting (a status change
  just means the row isn't in that tab's target set anymore), at the cost
  of not being able to see intermediate history in the Sheet itself.
- **Manual columns you add to any tab** (e.g. a "Notes" column) are
  preserved across runs, matched back to rows by `id`.
- **Email scraping is scoped to only the handful of posts with a
  non-NONE rights_status**, not all ~2000+ media rows, and is designed to
  never click "Send request" -- see `_DANGEROUS_BUTTON_PATTERN` in
  `refunnel_export.py`.
- **One shared login session across all 4 workspaces**: Refunnel workspace
  switching happens inside the app after logging in once, so there's a
  single `refunnel_session.json` / cache entry rather than one per brand.
- **Scheduled vs. manual runs** are decided by `scripts/determine_workspaces.py`
  reading `config/workspaces.yaml` -- a scheduled trigger only ever runs
  `schedule_enabled: true` workspaces; a manual trigger can target any
  workspace by name regardless of that flag.
