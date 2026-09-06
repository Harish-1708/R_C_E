# Refunnel Rights & Payments Sync

Pulls creator content + usage-rights status + payment history out of
Refunnel (app.refunnel.com) and syncs it into a Google Sheet with 6 tabs:
Master Data, Usage Rights - Approved, Usage Rights - Requested,
Usage Rights - Declined, Human Review, Payments.

Runs daily via GitHub Actions, across **4 Refunnel workspaces**
(Duderobe, Swoveralls, Defi Snacks, Kelson) -- each gets its own Google
Sheet. Only **Duderobe** runs on the automatic schedule right now; the
other 3 are built and ready but scheduled runs are switched off for them
(toggle in `config/workspaces.yaml`, see below). Any workspace can always
be run manually regardless of that setting.

## What updates every day

Every tab is fully rewritten from Refunnel's current state each run --
so here's exactly what that means in practice:

- **New content appears automatically.** The row counts you've seen
  (2078, etc.) aren't a ceiling -- that's just how many posts existed
  in Refunnel at the moment that run happened. Whenever a new TikTok/IG
  post tags or mentions the brand, it becomes a new row (keyed by its
  own unique `id`) the next time the sync runs. Nothing caps this.
- **Every field on an existing row refreshes to whatever Refunnel
  currently reports** -- likes, comments, impressions, shares, emv,
  gmv, and critically `rights_status` itself. So when you approve or
  deny a usage-rights request inside Refunnel, that change shows up in
  the Sheet on the next run.
- **Rows move between tabs automatically as rights_status changes.** A
  post that gets approved disappears from "Usage Rights - Requested"
  and appears in "Usage Rights - Approved" on the next run -- you don't
  need to do anything for that to happen.
- **Payments**: same pattern -- new payments appear as new rows, keyed
  by their own id.
- **`creator_email`** stays blank until email scraping is enabled (see
  below) -- once it is, it fills in incrementally, and once found for a
  post, it's never re-scraped again (the sync reads back whatever's
  already in the sheet first).
- **Rows move into Human Review once you mark them "Reviewed"** in
  Master Data (see "How the Human Review tab actually works" below) --
  and out of whichever Usage Rights tab they were in.
- **Manual columns you add yourself** (a "Notes" column, the "Reviewed"
  column itself) are preserved across every run -- see "Notes on design
  choices" below for how that works.

## How the Human Review tab actually works

This changed from the first version, based on your feedback:

1. Add a column literally named **"Reviewed"** to the **Master Data**
   tab yourself, wherever you like -- it's not part of what this script
   writes, so it survives every future sync untouched, the same way any
   manual column does.
2. Mark a row `Yes` (or `y` / `true` / `1`, case-insensitive) in that
   column whenever you've reviewed that post.
3. On the **next** sync, that row is automatically moved OUT of
   whichever tab it was in (Approved / Requested / Declined) and INTO
   the **Human Review** tab instead. It's removed from the other three
   -- not duplicated. It stays in Master Data regardless; only its
   Usage Rights tab placement changes.

Nothing currently in Human Review by default -- it only ever contains
what you've actively marked reviewed. See `apply_human_review_flags()`
in `parse_refunnel.py` for the actual logic, and `read_column_values()`
in `sheets_sync.py` for how your "Reviewed" values get read back each
run.

## What I just added

- **Widened scroll-loading patience** (`scroll_pause_ms` 1200ms ->
  2000ms, `idle_rounds_before_giving_up` 6 -> 12) -- this was an
  initial hypothesis for a stall at 120/2771 that turned out to be
  wrong (see the entry right below): a second run with this wider
  patience stalled at the exact same 120, proving it wasn't a timing
  issue at all. Left in place anyway since it's a harmless general
  safety margin, but it was NOT the fix for that specific problem.

- **The "Last 12 months" undercount fix is reverted -- turned out to be
  a Refunnel-side bug, not ours.** Two real runs stalled at the exact
  same post (@faisalofficial993, ~120 of 2771), even after doubling how
  much patience `scroll_to_load_all` was given -- identical stopping
  point both times ruled out a timing explanation. You then confirmed
  it directly: scrolling that same URL manually in a real browser hits
  the identical wall, and Refunnel's own manual export feature caps out
  at just 20 rows under that filter. Since a genuine human hits the
  same limit, not just our automation, this isn't fixable from our
  side -- worth reporting to Refunnel's own support team. Reverted back
  to no forced date filter (Refunnel's own default), which is confirmed
  reliable at ~2065-2088 rows across many past runs. If Refunnel ever
  fixes this on their end, `REFUNNEL_SOCIAL_LISTENING_URL` in
  `refunnel_auth.py` is the only line that needs to change back.
- **No "views" column** -- reverted. Refunnel's own Analytics panel
  (confirmed from a real screenshot) labels this metric "Impressions",
  not "Views" -- there's no separate "views" concept in the product at
  all. Fully back to the original column name and position.
- **Fixed a side effect of that reverted rename**: renaming a known
  column and then reverting it left stray blank-header cells trailing
  in a real sheet, because the "preserve manual columns" logic (built
  for something like a real "Notes" column) doesn't know the difference
  between a genuine manual column and a leftover renamed one. Now
  ignores any blank-named header when deciding what counts as a manual
  column to carry forward -- a real manual column always has a name.
- **A found email now propagates to every other post by that same
  creator automatically** (`propagate_emails_by_username` in
  `parse_refunnel.py`), both before scraping starts and after each
  restart during it -- so scraping a creator's email once covers all
  their other content instead of re-scraping per post. Confirmed real
  opportunity: 342 of 1360 unique usernames in a real export appear on
  2+ posts. Bonus side effect confirmed from a real run: this also fills
  in emails for Approved/Declined posts by the same creator, even though
  those statuses have no scrapeable button of their own.
- **A crashed browser now recovers AND resumes scraping**, not just
  recovers once to limp to Payments. Confirmed from real runs that a
  single scheduled run could hit the crash repeatedly, each time only
  covering a fraction of what's left -- now it automatically gets a
  fresh session and keeps scraping the remaining (shrinking, thanks to
  propagation) target list, up to a bounded 3 automatic restarts per
  run, before moving on. This should mean far less need to keep
  manually re-triggering runs.

- **Email scraping is implemented for real now** (see `scrape_creator_emails`
  in `refunnel_export.py`), built directly from your screenshots of the
  actual flow: click a post's "Request usage rights" toggle -> its top
  menu item "Request usage-rights" -> the Email tab -> read the
  pre-filled address -> close via the Escape key (never a button click,
  so it can never accidentally hit "Send request"). Still off by
  default (`SCRAPE_EMAILS_ENABLED = False`) -- see the checklist below
  for the one piece of evidence still needed before turning it on.
- **Won't re-scrape an email once it's found.** The sync now reads back
  whatever's already in Master Data's `creator_email` column before
  deciding what still needs scraping -- so a post only ever gets
  scraped once, not every day.
- **The Human Review tab is now flag-driven**, not automatic -- see
  "How the Human Review tab actually works" above. (It used to just
  combine all of Approved+Requested+Declined into one tab regardless of
  whether you'd looked at them -- this was the wrong design, per your
  feedback, and is now fixed.)
- **Consistent row heights, for real this time.** CLIP text wrapping
  alone wasn't enough -- confirmed from a real sheet screenshot that
  the actual cause was captions containing genuine embedded newline
  characters (from the original post's own line breaks), which force
  multi-line rendering regardless of wrap setting. Every cell value now
  has embedded newlines collapsed to a single space before being
  written (`_flatten_cell` in `sheets_sync.py`), so every row is
  uniformly single-line. CLIP + a bolded, frozen header still apply on
  top.
- **Newest content at the top.** Master Data and the media-derived tabs
  now sort by `created_at` descending (Human Review by `updated_at`
  descending) -- safe because those are ISO-format timestamps, which
  sort correctly as plain strings. Payments is left on the default
  id-based sort, since its date field ("Sep 4, 2026") does NOT sort
  correctly as a plain string across month/year boundaries.

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
            real_media_2078.csv  <- a real full-scale export, kept as a regression fixture
    .github/workflows/refunnel-sync.yml
```

## Files

| File | What it does | Tested how |
|---|---|---|
| `parse_refunnel.py` | Turns the 2 CSVs into 6 tabs' worth of rows, keyed by id; Human Review is flag-driven | 26 automated tests against your real sample + full-scale CSVs |
| `sheets_sync.py` | Rewrites each Sheet tab from parsed data, preserving manual columns, formats for consistent row heights, reads back manual columns like "Reviewed" | 13 automated tests against an in-memory fake Sheet |
| `gmail_otp.py` | Fetches a Refunnel login code from Gmail (fallback path only) | 7 automated tests against a fake Gmail client |
| `refunnel_auth.py` | Saved-session reuse + fresh OTP login via Gmail fallback | **Not run against the live site** -- see below |
| `refunnel_export.py` | Workspace switching, scroll-to-load-all, CSV export triggers, email scrape | Switching/scroll/safety-net logic has 16 automated tests against fakes; the actual browser clicks are **not run against the live site** |
| `scripts/determine_workspaces.py` | Decides which workspace(s) a run should sync, from `config/workspaces.yaml` + trigger type | 10 automated tests |
| `run_daily_sync.py` | Orchestrates the above into one workspace's run | Syntax-checked only |
| `build_preview_workbook.py` | One-off: builds a local xlsx to eyeball tab structure before wiring up live Sheets | Run, produced `refunnel_sync_preview.xlsx` |
| `.github/workflows/refunnel-sync.yml` | Daily schedule + matrix over workspaces | Not run |

Run all automated tests (from the repo root): `python -m pytest -v`
Lint everything: `python -m pyflakes *.py scripts/*.py tests/*.py`
(Both were run before delivery -- 72 tests pass, no lint warnings.)

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
Duderobe). The script creates the 6 tabs itself on first run if they
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
- [x] **Email scraping is genuinely working** -- confirmed with a real
  run: 240 real emails found and saved. Built from a real, complete
  HTML trace of the whole flow, for BOTH never-requested and
  already-Requested posts. Confirmed real selectors:
  `.usage-rights-request-card` / `.usage-rights-requested-card` (the
  toggle -- class name differs by status, both matched), the popup's
  top menu item (matched by its subtitle text, which is identical
  regardless of status, since the title text isn't), the `.ur-tab-card`
  Email tab (active by default, confirmed for both statuses), and
  `get_by_label("Creator email address")`. `SCRAPE_EMAILS_ENABLED =
  False` by default regardless -- flip it yourself to test. It will
  never click "Send request" regardless of anything else going wrong --
  enforced by `_safe_click`'s pattern check, independent of whatever
  selector logic runs above it.
  **Scoped to Requested + NONE-status** (never Approved/Declined): a
  real debug screenshot confirmed Approved posts replace the request
  button with a status badge -- "Usage rights approved -- Via direct
  post permission" -- with no button to click at all; every attempt on
  one was a guaranteed failure. NONE-status (never-requested) posts use
  the exact same working request-card UI as Requested ones -- confirmed
  real, since the very first pre-filled-email screenshot in this whole
  project was a never-requested post. Declined is presumed to behave
  like Approved but isn't separately confirmed (0 declined rows existed
  to test against). Widening to NONE-status is a deliberate one-time
  cost (~2065 posts instead of ~732) -- every subsequent run only
  re-attempts posts still missing an email, so this doesn't recur.
  The circuit breaker (`max_consecutive_failures`, default 25 --
  deliberately generous, since a single isolated failure just skips to
  the next post as normal) is still there as a last resort.
- [x] **A scraping crash no longer takes down the whole run.** A real
  run hit `Page crashed` (the browser process itself died, not a
  selector problem) partway through -- traced to one specific unguarded
  call (`_pace()` in the per-item `finally` block) that let the error
  escape the per-item error boundary instead of being caught and
  counted toward the circuit breaker like every other failure. Now
  wrapped in its own try/except. Additionally, `run_daily_sync.py` now
  catches a scraping-level crash entirely and continues to the rest of
  the pipeline rather than aborting the whole run.
- [x] **A crashed browser no longer poisons the rest of the run
  either.** A second real run showed the *next* problem once the above
  was fixed: the circuit breaker correctly stopped scraping after the
  browser crashed, but the following step (navigating to Payments)
  then failed too, since it tried to reuse the same already-dead page.
  `run_daily_sync.py` now checks whether the page survived scraping
  (`page.evaluate("() => 1")`) and, if not, closes out the dead
  browser and gets a fresh logged-in session (reusing the saved
  cookies -- no full re-login needed) before continuing to Payments.
- [ ] **Still open: the browser itself crashes periodically during
  very long scraping sessions** (confirmed twice now, both times deep
  into the widened ~2065-post scan). The fixes above make each crash
  survivable rather than fatal, but don't reduce how often it happens
  -- a long-lived single Chromium instance doing thousands of DOM
  interactions is exactly the kind of load that leads to this. A more
  complete fix would periodically restart the browser *during*
  scraping (not just recover once after), keeping memory bounded
  throughout -- not yet built, since it requires restructuring
  `scrape_creator_emails`'s loop to support swapping out `page`
  mid-scan. Worth building if crashes keep recurring after this fix.
- [x] **Other tabs are now copied from Master Data's actual sheet
  state, not from a fragile in-memory intermediate.** Master Data is
  the only tab any scraping/email logic touches directly (via the
  incremental per-email save). After scraping -- whether it finished
  cleanly or crashed partway -- the script re-reads Master Data's
  current `creator_email` column and re-applies it to every other tab's
  data before writing them. This means a scrape that crashes halfway no
  longer leaves Usage Rights / Human Review stuck with stale, pre-run
  data -- they correctly reflect whatever Master Data actually has,
  including partial progress.
- [x] **Progress saved incrementally**: emails are now written to the
  sheet as each one is found (`update_single_cell`), not only at the
  very end -- so an interruption mid-scrape doesn't lose what was
  already found.
- [x] **Duplicate-content check**: verified against your real 2078-row
  export -- 0 duplicate `original_post_link` values (the 6-row gap
  between total rows and unique links is entirely blank links, from
  Instagram Stories which have none -- not duplicates). `id`-based
  dedup already prevents duplicate rows; `find_duplicate_post_links()`
  adds a second, independent check using the actual post URL, in case
  Refunnel ever assigns two different ids to the same real post. It's
  diagnostic only (logs a warning) -- never auto-removes a row, since a
  shared link could have a legitimate reason.
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
- **Manual columns you add to any tab** (e.g. a "Notes" column, or the
  "Human Review" tab's own review-status column) are preserved across
  runs, matched back to rows by `id`.
- **Master Data and Payments can only ever gain rows or update existing
  ones -- never lose one**, regardless of what a given run's pull looks
  like (`sync_tab(..., never_delete=True)`). If an id from a previous
  run is missing from today's fresh pull, it's carried forward
  unchanged rather than dropped. Usage Rights tabs and Human Review are
  deliberately NOT protected this way, since rows leaving them (a
  status changing, a post marked Reviewed) is correct, intended
  behavior, not data loss.
- **CLIP text wrapping + a frozen, bolded header** on every tab, for
  consistent row heights instead of one long caption blowing out a
  single row's height next to short ones.
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
