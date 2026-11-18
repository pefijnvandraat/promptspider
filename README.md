# Prompt Spider

Local, searchable archive of the prompts you write across **seven AI surfaces**:

| Source | Where it comes from | Access |
|---|---|---|
| `m365-copilot` | Microsoft 365 Copilot (BizChat) | Authenticated DOM |
| `cowork` | Copilot Cowork tasks | Authenticated DOM |
| `scout` | Microsoft Scout desktop app | Local SQLite |
| `copilot-cli` | GitHub Copilot CLI / ADO | Local SQLite (`host_type`) |
| `copilot-app` | Current Copilot sessions | Local session event logs |
| `github-copilot` | GitHub Copilot web + desktop app | Authenticated DOM |
| `vscode` | GitHub Copilot Chat in VS Code | Local JSON / JSONL |

## Install on Windows

1. Double-click **PromptSpider-Setup.exe** from your approved software source
   and follow the setup wizard. You don't need admin rights or a separate
   Python installation. If your work laptop blocks setup, contact IT.
2. Open **Prompt Spider** from the Start menu. Select **Sign in to Microsoft
   365 Copilot**, sign in with your work account in Microsoft Edge, and close
   the Edge window once you can see your chats.
3. Select **Import Copilot chats**. Keep Edge open while the app imports recent
   chats, then select **Open Search**. Keep Prompt Spider open while you search.

The Windows app also has an **Import history from this laptop** button for
supported local AI history. Imports are on demand and don't run on a schedule.
The Copilot button imports recent Microsoft 365 Copilot web chats only; it
doesn't import Cowork or chats from inside Word, Excel, Outlook, or Teams.

Check your organization's data policy before importing work chats. Prompt Spider
saves imported text and a dedicated Edge sign-in profile on this laptop in
`%LOCALAPPDATA%\PromptSpider`. You can uninstall from **Settings > Apps >
Installed apps**. Uninstall keeps this folder so you don't lose your history;
to remove the history and sign-in profile too, delete the folder after
uninstalling.

### Build the installer

On an x64 Windows build machine with 64-bit Python 3.12 and Inno Setup 6, run:

```powershell
.\windows\build.ps1 -Version 0.1.0 -Compiler 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

The build produces `dist\PromptSpider-Setup.exe` and bundles Python and the
browser driver. Microsoft Edge itself must already be installed on the laptop.
For managed laptops, sign and distribute the installer through your approved
software channel. The developer build is unsigned.

## Why this exists

There is no local file and no accessible API for M365 Copilot prompts. The Graph
endpoint (`interactionHistory`) requires an admin-consented *application*
permission, so under normal delegated access it returns 403. The only route
available without approvals is reading the authenticated DOM.

The GitHub Copilot **desktop app** is the same story for a different reason: it
is a WebView2 shell with cloud session storage, so the only local trace it
leaves is a keystroke autosave buffer. Its internal endpoints
(`/copilot_internal/*`) answer 403 with an explicit anti-scraping notice, so
this reads only `github.com/copilot` pages the signed-in user can open himself.

The local surfaces are far easier to read — their challenge is **signal, not
access** (see Origin classification below).

## Where M365 Copilot data actually comes from

The chat surface is read from the **hydration payload**, not the rendered page.

Every conversation document ships `window.__… = JSON.parse("…")` containing the
full transcript: each message with `text`, `author` (`user` / `bot`), `createdAt`
and `messageId`, plus conversation metadata (`chatName`, `createTimeUtc`,
`updateTimeUtc`) and the sidebar's thread list. None of it is rendered, which is
why an earlier pass concluded — correctly, for the DOM — that no timestamps
existed anywhere.

This matters beyond convenience:

| | Rendered DOM | Hydration payload |
|---|---|---|
| Timestamps | none | per message, UTC + local |
| Assistant replies | paired by DOM order | explicit `author` field |
| Title | scraped link text | `chatName` |
| Scheduled vs human | heuristic | `isScheduledPromptThread` |
| Wait before reading | 6–15s hydration | arrives with the document |
| Breaks when | CSS class hashes rotate | payload shape changes |
| Testable offline | no | **yes** — `tests/test_payload.py` |

`payload.py` is a pure function over a string: no browser, no network, no lock.
That is the point. A UI change now fails a test in 0.2s instead of quietly
writing thousands of empty rows at 03:30.

The DOM extractor is still there and still runs whenever the payload yields
nothing. Both paths feed the same guardrails.

## Running it

```powershell
# Local files: Scout, Copilot CLI, VS Code. Fast, no browser, no lock.
py harvest.py --mode local

# See what's new in M365 Copilot / Cowork and harvest it
py harvest.py --mode incremental

# GitHub Copilot chats and agent sessions (web UI + desktop app)
py harvest.py --mode github

# Walk the whole Copilot sidebar, recording everything as pending (slow, hours)
py harvest.py --mode discover --budget-min 240

# Extract prompt text for pending items (resumable, this does the backfill)
py harvest.py --mode extract --budget-min 120 --limit 500

# Re-visit conversations already stored, to fill in timestamps and responses
py harvest.py --mode backfill --budget-min 120 --limit 500

# Offline tests — no browser, no network, no auth
py -m pytest tests/ -q

# Dry run: how much would re-harvesting existing conversations gain? Read only.
py align_check.py --limit 40
```

Re-running extraction over conversations already stored is **enrichment**:
`prompts.id` is content-addressed on `(source|session|turn_index)`, so a second
pass lands on the same rows and fills in `ts_source`, `response_excerpt`,
`message_id` and `author` without touching `first_seen_at`.

A row is only enriched when the prompt at that turn index still matches. Where
the stored text and the payload disagree the row is skipped and counted as a
conflict — because an index does not always mean the same turn in both. Pass
`--overwrite-conflicts` to let the payload win; it is off by default because it
is the only path here that destroys previously harvested text.

Web UI: <http://127.0.0.1:8420>

## Local usage costs

The **Costs** view shows locally recorded AI-credit usage for supported Copilot
session logs and Scout / Copilot CLI history. Run `py harvest.py --mode local`
to import prompts and usage from those local files. The usage readers open the
source logs and SQLite stores read-only; they do not need billing credentials.
This does not change any scheduled jobs or a running Prompt Spider service.

Amounts use the recorded nano-credit value and the documented conversion of
1 credit = US$0.01. The screen says **“Locally recorded; not an invoice amount”**
because local usage may include discounts and may not include every device or
service. Missing amounts stay unknown, partial values are marked, and usage
without a verified prompt link remains in the unlinked totals. Helpers are
included only when the local event ancestry or saved source turn proves which
prompt owns them.

The current Copilot session-log importer follows event parent links to an
individual user message. It does not use a numeric turn ID by itself. Older
session-store records link only when their session and turn match an existing
Prompt Spider prompt. An unreadable, replaced or quiet source is shown as
partial, replaced or stale rather than presented as current coverage.

Search results can be sorted by known usage or filtered with **Minimum
credits**; prompts with unknown usage do not qualify. JSON, CSV and Markdown
prompt exports include available cost fields. **Download usage-only CSV**
contains usage details but no prompt text, local file paths or call IDs.
Cost totals use compact rounded display values, with keyboard-accessible exact
amounts available by expanding each value. The prompt, day, model and source
tables each have their own filter, sort direction, reset control and paging;
these controls run against all matching records while the existing date, source
and model filters continue to apply.
The supported-prompts table ranks the full filtered result set before paging,
so changing the sort shows the true highest or lowest prompts on the first page.
M365 Copilot, Cowork, VS Code and GitHub Copilot web/desktop activity remains
unsupported or unknown until a local per-prompt source is verified. No billing
feed or invoice allocation is included.

## Origin classification

The raw corpus is overwhelmingly machine-generated: in the Scout store **93% of
turns are agent personas, scheduled heartbeats or injected context** rather than
something a person typed. Every prompt is therefore stored with an `origin`:

| Origin | Meaning |
|---|---|
| `human` | Actually typed by the user — the UI's default view |
| `automation` | Crew/agent personas (`"You are ..."`), scheduled jobs |
| `heartbeat` | Periodic background checks |
| `system` | Injected context, conversation replays, sub-agent briefs, command classifiers |

**We classify rather than discard.** Filters can be retuned later; deleted rows
cannot. Host-appended blocks (`[Microsoft Scout context: ...]`,
`<system_reminder>`) are stripped from the stored text so search matches what
was actually written.

## The three browser modes, and why they're separate

Enumeration and extraction differ in cost by orders of magnitude. A full sidebar
walk took **7.5 hours and found 6,362 conversations without reaching the end**;
extraction runs at ~11s per conversation. Combined in one run, enumeration eats
the entire budget and nothing gets stored — which is exactly what happened on the
first attempt.

| Mode | Does | Cost | When |
|---|---|---|---|
| `discover` | Walks the sidebar, records items as pending. Checkpoints every 10 rounds. | Hours | Weekly |
| `extract` | Reads pending items from the DB and pulls prompts. No enumeration. | Bounded by budget | Nightly |
| `backfill` | Re-visits already-stored conversations that are missing a timestamp or response, and enriches them in place. | ~10s per conversation | Until it drains |
| `incremental` | Shallow walk of the top of the list + extract what's new. | ~minutes | Nightly, once caught up |

`discover` and `extract` are decoupled through `harvest_state`, so a killed run
never loses work.

## Configuration

All paths resolve from the environment, so nothing machine-specific is
committed:

| Variable | Default | Purpose |
|---|---|---|
| `PROMPT_SPIDER_HOME` | `~/.prompt-spider` | Where `prompts.db` and the run lock live |
| `PROMPT_SPIDER_PROFILE` | `$HOME/.prompt-spider/edge-profile` | Persistent Edge profile carrying the M365 session |
| `PROMPT_SPIDER_PYTHON` | `pythonw` on PATH | Interpreter used by pm2 |
| `PROMPT_SPIDER_PORT` | `8420` | Web UI port |

Set `PROMPT_SPIDER_PROFILE` to an Edge profile that is already signed in to
M365 — the harvester never handles credentials, it borrows the session.

## Files

| File | Role |
|---|---|
| `db.py` | Schema, connections, migrations, content-addressed IDs |
| `browser.py` | SSO Edge session, run lock, sign-in and nav readiness |
| `payload.py` | **M365 hydration-payload parser. Pure functions — no browser, no network.** |
| `collectors.py` | Browser enumeration + extraction. **All DOM knowledge lives here.** |
| `local_sources.py` | Scout / CLI / VS Code readers + origin classifier |
| `github_source.py` | GitHub Copilot web/desktop DOM knowledge |
| `harvest.py` | Pipeline, guardrails, run log, CLI |
| `align_check.py` | Read-only dry run: what would re-harvesting gain, and what conflicts |
| `server.py` | FastAPI search API |
| `static/index.html` | Single-page UI |
| `tests/` | Offline tests + a sanitised payload fixture |
| `ecosystem.config.js` | pm2 process definition |

Database: `$PROMPT_SPIDER_HOME/prompts.db` — keep this **outside any synced
folder**, because it contains customer and MIP-labelled content.

## Scheduled

- **Nightly 03:30** — `local` (all local sources), then `extract` backlog, else `incremental`
- **Sunday 02:00** — `discover` full walk

The browser steps open a **visible** Edge window; headless is rejected on the
SSO profile because MSAL/MFA flows need visible UI. `local` needs no browser and
never conflicts.

## Guardrails

The dominant failure mode for a DOM harvester is silent breakage — a UI change
that writes thousands of empty rows, or a page that fails to hydrate and looks
like "nothing to do". So:

- **Zero enumeration is a hard failure**, never a clean run
- **>35% empty extractions aborts** the run
- **Every run is logged** to `harvest_runs` with counts and error text

Watch the `enumerated` column in the Corpus view. A sudden drop means the UI
changed.

## Known limitations

- **In-app Copilot is invisible.** Word, Excel, PowerPoint, Outlook and Teams
  prompts need the Graph route.
- **Cowork has no source timestamps.** Its transcript is read from the DOM of a
  hash-routed SPA, which exposes none. M365 Copilot chat *does* now carry real
  timestamps (see above), as do the local sources.
- **The GitHub Copilot desktop app has no local history.** It is a WebView2
  shell with `cloud_session_storage_enabled`, so `--mode github` reads its
  conversations from the web UI instead. Sessions started with
  `copilot --remote` that were never synced render as empty shells there; those
  prompts arrive via `copilot-cli` and are counted as `no_transcript`, not as
  failed extractions.
- **GitHub timestamps are session-level.** Neither layout exposes per-turn
  times, so every prompt in a thread shares the transcript's earliest timestamp.
- **Origin classification is heuristic.** A human writing "You are..." would be
  misfiled as automation. Nothing is deleted, so misclassification is
  recoverable by retuning the rules and re-running `--mode local`. The M365
  payload exposes `isScheduledPromptThread`, which is authoritative and will
  eventually replace the guess for that surface.
- **The hydration payload is unofficial.** It can change without notice. The DOM
  extractor is retained as a fallback and `tests/test_payload.py` fails loudly
  if the shape moves.
- **Local usage costs are not invoice amounts.** Current Copilot session logs
  and Scout / CLI history provide some recorded usage, but not every prompt or
  device. Historical stores may be old; source freshness and unlinked usage are
  shown separately. No public-rate estimates or billing totals are mixed into
  these prompt figures.
- **Snapshot, not archive** (browser sources). Harvested rows are safe, but
  anything the UI stops showing can't be re-verified.
- **Fragile by nature.** Mitigated by loud failure, not prevented.

See `PLAN-HANDOFF.md` for the full design rationale and the gotchas that cost
the most time.
