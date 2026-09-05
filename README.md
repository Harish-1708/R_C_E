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

**3. A Gmail App Password** (for the OTP-login fallback only, shared
across all workspaces since it's the same Refunnel account):
   - Turn on 2-Step Verification on the Gmail account, if it isn't
     already (App Passwords require it).
   - Go to https://myaccount.google.com/apppasswords, create one for
     "Mail" -- Google hands you a 16-character password immediately.
     No Cloud Console project, no OAuth consent screen, no refresh token
     to manage.
   - Store the Gmail address as `GMAIL_ADDRESS` and that password as
     `GMAIL_APP_PASSWORD`.

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
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | OTP fallback (IMAP) | 1 each (shared) |
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

## Debugging a failed run

On any failure, `run_daily_sync.py` now saves a screenshot
(`failure_screenshot.png`) and the raw page HTML (`failure_page.html`)
into `downloads/<workspace>/debug/` -- and that whole `downloads/`
folder is already uploaded as a workflow artifact (Actions tab -> the
failed run -> Artifacts section -> `refunnel-csv-exports-<workspace>`),
so you don't need to add anything to see it.

This matters because "nothing rendered in time" can mean several
different things that look identical from the error message alone:
still on a login page, a wrong URL, or -- worth specifically checking
for, since this site uses Cloudflare (`__cf_bm` cookie seen in your
session export) -- a bot-detection challenge page instead of the real
dashboard. Headless browsers running from datacenter IPs (like GitHub
Actions runners) get challenged by Cloudflare far more often than a
real browser on a home connection does, independent of whether the
session/cookies are valid. If the screenshot shows a "Checking your
browser..." / CAPTCHA-style page instead of Refunnel's actual UI, that
confirms it, and the fix is different from a selector problem -- send
me that screenshot if so.

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
- [x] **Media export button**: was located by screen position
  (`:left-of()`), which turned out wrong -- a real run clicked "Sort by"
  instead (also technically "left of" Create collection, just on a
  different row). Fixed using a real class name confirmed from an HTML
  dump: `.upload-content-activator` -- confirmed genuinely unique (one
  match in the page). The menu item text ('Export Content CSV') was
  already confirmed separately and is unchanged.
- [x] **Sticky header hides on scroll**: a follow-up real run then found
  that same button correctly, but reported it as "hidden" -- confirmed
  from two HTML dumps (before/after scrolling) that the header
  containing it toggles `content-header-visible` -> `-hidden` once
  scrolled down, i.e. exactly what `scroll_to_load_all()` does right
  before this runs. Fixed: `export_media_csv` now scrolls the container
  back to the top first.
- [x] **Missing export-confirmation modal**: clicking 'Export Content
  CSV' doesn't download immediately -- a real screenshot showed it opens
  an "Export data in CSV format" modal with two choices, "Download to
  device" (immediate, what we want) vs "Export to email" (async, emails
  a link later). Fixed: `export_media_csv` now clicks "Download to
  device" before expecting the download. Bonus confirmation from that
  same screenshot: it showed "2078 of 2078 media" loaded, proving
  `scroll_to_load_all()` is working correctly end-to-end.
- [x] **The whole media export now works end-to-end**: a real run
  produced an actual downloaded CSV (2078 rows) for the first time.
  Running it through `parse_refunnel.py` surfaced one real bug: the
  actual `rights_status` enum value is `DENIED`, not `DECLINED` as
  originally guessed -- fixed in `RIGHTS_STATUS_MAP`, confirmed against
  the real 2 denied rows in that export, and that file is now a
  permanent test fixture (`tests/fixtures/real_media_2078.csv`).
- [x] **Payments page URL**: confirmed wrong by a real run, then fixed
  with the real URL you sent from your address bar --
  `/dashboard/payments/history` (the guess was missing `/history`).
  Payment export itself has no confirmation modal (unlike media), so it
  should just work now that navigation lands in the right place --
  still worth confirming on a real run.
- [x] **Sidebar collapsed on load**: confirmed root cause of the actual
  failure, from a real screenshot + HTML dump -- the sidebar can load
  collapsed (`class="left-side-navbar collapsed"`), and while collapsed
  the workspace name isn't in the DOM at all, not just hidden. Fixed:
  `select_workspace` now clicks the sidebar's own `<img alt="Toggle
  menu">` expand control if no workspace name appears within 3s, then
  waits again with the full time budget. Still worth confirming on a
  real run that this actually resolves it end-to-end (the toggle click
  itself hasn't been exercised against the live site).
- [x] **Social Listening page URL**: was guessed (`/social-listening`),
  actually confirmed wrong by a real run -- the real path is
  `/dashboard/content/social-listening`, found in analytics data embedded
  in a session export you shared. Fixed in `refunnel_auth.py`
  (`REFUNNEL_SOCIAL_LISTENING_URL`).
- [x] **Scroll-to-load-all mechanism**: a real run only loaded 20/2078
  items before giving up. Root cause: `page.mouse.wheel()` scrolls
  wherever the mouse happens to be, which was never necessarily over the
  actual content area. Fixed to directly scroll the real container
  instead, confirmed from an HTML dump: `#scrollableDiv` (a
  react-infinite-scroll-component, separate from the virtuoso grid used
  just for rendering). The "80 of 2078 media" counter text pattern
  itself was already confirmed correct and is unchanged -- still worth a
  fresh real run to confirm the full 2078 now loads.
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

Set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in your shell first, then:
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
