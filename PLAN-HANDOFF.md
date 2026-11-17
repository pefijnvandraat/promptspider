# Prompt Spider — Build Plan & Handoff

**Purpose:** Harvest the prompts a user writes in **Microsoft 365 Copilot (BizChat)** and **Copilot Cowork** into a local, searchable store, using their existing authenticated Edge session.

**Status:** Working. Harvester + web UI built and running. Backfill in progress.

**Author's note for whoever picks this up:** every "Verified ground truth" item below was established by live DOM probing against the real M365 Copilot UI, not from documentation. Several contradict what you'd reasonably assume, and two of them cost most of a day. Read that section before writing any code.

---

## 1. Context — why this design

The original goal was to gather prompts across four surfaces: an AI agent desktop app, M365 Copilot, M365 Copilot Cowork, and GitHub Copilot.

Investigation split them into two tiers:

**Tier 1 — local files, trivially accessible (not this project).** Volumes below are from one machine, as an indication of scale:
| Surface | Source | Volume |
|---|---|---|
| Agent desktop app | `~\.<agent>\copilot\session-store.db` (SQLite) | 1,220 sessions / 16,429 turns |
| Agent archive | `~\.copilot\session-store.db` (same schema) | 692 sessions / 1,422 turns |
| GitHub Copilot CLI | same store, `sessions.host_type` = `github`/`ado` | 3 sessions |
| GitHub Copilot (VS Code) | `%APPDATA%\Code\User\globalStorage\emptyWindowChatSessions\*.json` | 13 session files |

Both agent stores share one schema: `turns(session_id, turn_index, user_message, assistant_response, timestamp)` joined to `sessions(cwd, repository, branch, summary, host_type)` — `user_message` is the prompt log. VS Code's JSON exposes `requests[].message.text` with `timestamp`, `modelId`, and `agent`.

**Tier 2 — M365 Copilot + Cowork (this project).** No local files exist. Three routes were evaluated:

- **Route B — Graph `interactionHistory`:** `/beta/copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions`. **Verified 403** under delegated access. Requires the *application* permission `AiEnterpriseInteraction.Read.All` + admin consent. There is no delegated equivalent. This is the only route that also captures in-app Copilot (Word/Excel/PowerPoint/Outlook/Teams).
- **Route C — Purview `CopilotInteraction` audit records:** same breadth as B, needs Audit Reader role, capped by audit retention.
- **Route A — authenticated DOM harvest:** ← **chosen.** Zero approvals, works today.

**Ruled out along the way:**
- *Mailbox route:* enumerated all hidden mail folders; only `News Feed` and `Social Activity Notifications` are hidden. Copilot data lives in the non-IPM subtree, which Graph `mailFolders` never exposes.
- *Interceptable REST API:* the Copilot app's only visible Graph calls are `informationProtection/sensitivityLabels` and `drive/special/copilotuploads`. Conversation content streams over a **Trouter WebSocket** via the Sydney client; substrate calls send no `Timing-Allow-Origin`, so resource timing reveals nothing. There is no clean JSON endpoint to borrow.

---

## 2. Verified ground truth

Established by live probing and by running the harvester against the real UI.

| Finding | Value |
|---|---|
| Chat conversations | **6,362+** — a 7.5-hour walk hit a round cap without reaching the end |
| Agent-scoped share | ~19% (1,226 of 6,362) |
| Cowork tasks | **219+** (not walked to exhaustion) |
| Chat entry point | `https://m365.cloud.microsoft/chat` |
| Chat sidebar loading | Appends in pages of ~18; slows badly as the DOM grows (3s/round early, 60s/round late) |
| Timestamps | **None in the rendered DOM** — no `<time>`, no `datetime`, no relative labels. But present in the hydration payload; see gotcha #10 |
| Copilot Chats search | Returns **0** even for a term that is literally a chat title — unusable |
| Extraction rate | ~11s per conversation |
| Conversation hydration | 6–15s, highly variable — must wait on content, not a fixed sleep |

> ⚠️ **An earlier draft of this document said the corpus was 74 conversations and that the list virtualises. Both were wrong.** They are recorded here because the *way* they were wrong is the most useful thing in this document — see gotchas #4 and #5.

### Nine gotchas that will cost you a day each if missed

1. **Two conversation URL shapes.** Roughly a fifth are agent-scoped. A harvester assuming one shape silently loses every declarative-agent conversation.
   ```
   /chat/conversation/{guid}                     ← plain BizChat
   /chat/agent/{agentId}/conversation/{guid}     ← declarative agent (e.g. Prompt Coach)
   ```
   Regex: `/chat/(?:agent/(?<agent>[^/]+)/)?conversation/(?<guid>[0-9a-f-]{36})`

2. **`/cowork` direct navigation renders a completely blank page.** You must load `/chat` and click the `Cowork` nav item — it performs an SSR transition. A scraper that jumps straight to the URL gets nothing and looks like a permissions failure.

3. **Nav collapse state persists across navigations and silently zeroes enumeration.** When the sidebar collapsed mid-spike, conversation links dropped to 0 with no error. Always assert the nav is expanded before enumerating.

4. **JS `scrollTop` stalls the loader after ~2 pages — and that stall looks exactly like the end of the list.** This is the single most expensive mistake available here. Setting `scrollTop = scrollHeight` from JS loads about two pages, then the intersection observer never re-fires. The count sits still, a stability check concludes "done", and you confidently record a corpus of 74.

   **The fix is to drive scrolling from Playwright with real mouse-wheel events**, not from JS:
   ```python
   box = page.evaluate(NAV_BOX)          # re-query each round; geometry changes
   page.mouse.move(box["x"], box["y"])
   for _ in range(6):
       page.mouse.wheel(0, 700)
       page.wait_for_timeout(140)
   page.wait_for_timeout(2200)
   ```
   Trusted events keep the loader running indefinitely. Same code, same session: JS scrolling gave 74; wheel events gave 6,362 and had not finished.

5. **Stalls are pauses, not endings.** Even with wheel events the loader goes quiet for several rounds at a time. Back off and re-drive to re-arm it:
   ```python
   if stall and stall % 5 == 0:
       page.mouse.wheel(0, -1500); page.wait_for_timeout(600)
   ```
   Identical code with different patience returned 37, 91, and 181 items on three consecutive runs. Use a generous stall limit (25 rounds) and treat any low count as suspect.

   **Corollary for incremental mode:** if you early-exit when a round adds nothing new, you must not count *stalled* rounds toward that streak. Conflating "loader paused" with "reached already-known history" is the same bug wearing a different hat.

   *(An earlier draft blamed virtualisation for the count dips. It was wrong — `dom == accum` throughout, so the list appends rather than recycles. The dips were the loader resetting. Accumulating into a persistent Set is still correct, just for a different reason.)*

6. **Headless is rejected on the default SSO profile.** MSAL/MFA flows require visible UI, so the harvester always opens a visible Edge window. It cannot run invisibly. If the persistent Edge profile is shared with other automation, treat it as a **singleton** and take an exclusive lock before use.

7. **Conversations hydrate at 6–15s, not on a fixed schedule.** A 6-second sleep before extraction produced a **65% empty-read rate**. Wait on a content condition (`/You said/` present) with a generous timeout instead. Verified: 7 of 8 previously-"empty" conversations extracted fine once this was in place.

8. **M365 bounces through `login.microsoftonline.com` mid-navigation and comes back.** A naive "is the URL a login page?" check fires on this transient redirect and aborts a perfectly healthy run. Only fail if you are *still* on a sign-in page after settling ~12s.

9. **The nav drawer sometimes never hydrates**, and a missing drawer yields zero links — indistinguishable from "no data". Wait for `.fui-NavDrawerBody` to exist with non-zero height, retry the navigation, and treat **zero enumeration as a hard failure**, never a clean run.

10. **"The DOM exposes no timestamps" was true, and completely misleading.** Gotcha #0, discovered late and worth more than the other nine combined: the conversation document ships a hydration payload — `window.__… = JSON.parse("…")` inside a `<script nonce>` block — carrying the entire transcript with `author`, `text`, `createdAt`, `messageId`, plus `chatName`, `createTimeUtc` and the sidebar thread list. **None of it is rendered.** Probing the hydrated page and concluding "no chronology exists" was measuring the wrong artefact for months.

    Three consequences:

    - The payload arrives **with the document**, so the 6–15s hydration wait is unnecessary when it parses.
    - It is anchored on semantic keys, not the generated CSS class hashes that gotcha #4 warns rotate every deploy — so it is *more* stable than the DOM path, not less.
    - It is a pure string → dict transform, so it is **unit-testable offline**. `tests/test_payload.py` runs in 0.2s with no browser, no network, no auth and no lock. That is the first thing in this project that makes UI breakage *fail* rather than silently write empty rows overnight.

    Two traps inside the payload itself, both of which silently drop data:

    - Timestamp formats are **mixed**. Messages carry ISO strings (`createdAt: "2026-03-03T09:52:14.5473247+00:00"`); thread and conversation metadata carry **epoch milliseconds** (`createTimeUtc: 1772531534547`). Handling only ISO loses every conversation-level date. `expiryTimeUtc: 0` means "unset", not 1970.
    - Filtering captured responses on `"/chat/conversation/" in url` **misses every agent-scoped conversation**, which live at `/chat/agent/{agent}/conversation/{guid}` — that is gotcha #1 resurfacing in a new place, and it cost 100% of agent conversations in the first dry run. Use `CONV_RE`.

11. **The stored corpus is not a clean baseline to reconcile against.** A 30-conversation dry run (`align_check.py`) found the DOM extractor had, on ~23% of conversations: stored an attachment JSON blob as the prompt text, truncated a 1,284-character prompt to a single character, prefixed a prompt with stray words, and missed turns in the middle of a conversation. In every case inspected the payload was correct and the stored row was wrong.

    This matters for backfill: `prompts.id` is `sha256(source|session|turn_index)`, so a turn index that no longer means the same turn would attach the right timestamp to the wrong prompt. `store()` therefore only enriches a row when the prompt at that index still matches, and reports the rest as conflicts. `--overwrite-conflicts` lets the payload win, off by default.

### Cowork identifiers
Composite, deep-linkable:
```
https://m365.cloud.microsoft/cowork#/task/{tenantId}:{userId}:{taskId}
```

### Prompt extraction
Chat prompts render as the anchor text `You said:` followed by the prompt. Selector strategy, most→least stable:
1. Conversation GUID from element `id` (structural, very stable)
2. `You said:` anchor text, then walk to the sibling text node
3. `aria-label` / `role` attributes
4. **Never** depend on generated CSS class hashes — they rotate every deploy

---

## 3. Architecture

```
scheduler (nightly 06:30)
        │
   run-lock ──► launch SSO Edge (visible, persistent profile)
        │
   ┌────┴────┐
 chat      cowork
 collector collector
   └────┬────┘
    normaliser
        │
   prompts.db (SQLite + FTS5)
        │
   FastAPI web UI ──► 127.0.0.1:8420
```

Single process, sequential collectors. One tab, one navigation at a time.

### Locations
| What | Where | Why |
|---|---|---|
| Code | `<workspace>\Prompt Spider\` | Normal workspace |
| **Database** | `%USERPROFILE%\prompt-spider\prompts.db` (or any non-synced local dir) | **Deliberately NOT in a synced folder** — contains customer + MIP-labelled content, must not sync |
| SSO profile | Existing persistent Edge profile directory | Reused so no credentials are handled |

---

## 4. Data model

```sql
CREATE TABLE prompts (
  id                TEXT PRIMARY KEY,   -- sha256(source|source_id|turn_index)[:32]
  source            TEXT NOT NULL,      -- 'm365-copilot' | 'cowork'
  source_session_id TEXT NOT NULL,      -- conversation GUID | composite task id
  agent_id          TEXT,               -- non-null for agent-scoped chats
  turn_index        INTEGER,
  ts_observed       TEXT NOT NULL,      -- harvest time (real)
  ts_source         TEXT,               -- real timestamp: local sources, and
                                        -- M365 chat via the hydration payload
  recency_rank      INTEGER,            -- sidebar position at enumeration
  prompt            TEXT NOT NULL,
  response_excerpt  TEXT,
  title             TEXT,
  app_context       TEXT,               -- 'chat' | 'task' | 'automation'
  deep_link         TEXT,
  content_hash      TEXT,
  provenance        TEXT,               -- 'backfill' | 'incremental'
  first_seen_at     TEXT,
  last_seen_at      TEXT
);

CREATE TABLE harvest_state (            -- per-item watermark, drives incremental
  source, source_session_id, title, agent_id, recency_rank,
  content_hash, discovered_at, last_harvested_at,
  PRIMARY KEY (source, source_session_id)
);

CREATE TABLE harvest_runs (             -- run log, powers health view
  run_id, started_at, ended_at, status, mode,
  enumerated, visited, items_new, empty_ratio, error
);

CREATE VIRTUAL TABLE prompts_fts USING fts5(
  prompt, title, content='prompts', content_rowid='rowid'
);
```

**Two schema decisions worth preserving:**

- `id` is **content-addressed** (`sha256(source|session|turn)`), which is what makes re-runs idempotent with no coordination.
- `ts_observed` and `ts_source` are **deliberately separate**. Keeping them apart stopped the UI from ever implying a precision we did not have — and left the door open when it turned out we could have it after all. `ts_source` is now populated for the local sources and for M365 chat (hydration payload); it stays NULL for Cowork and for rows harvested before that change until a backfill pass reaches them. Because `id` is content-addressed, that backfill is an in-place enrichment rather than a re-import.

---

## 5. Harvest algorithm

**Phase 1 — discover (hours, weekly)**
1. Launch browser → `/chat`, wait for the nav drawer to actually exist.
2. Wheel-scroll to exhaustion with stall recovery (gotchas #4, #5).
3. Capture **both URL shapes** (gotcha #1) and titles in the same pass.
4. **Checkpoint every 10 rounds** into `harvest_state`. Non-negotiable: the first full walk ran 7.5 hours and lost all 6,362 items because nothing was persisted until the end.
5. Cowork: click the nav item, never navigate directly (gotcha #2).

**Phase 2 — extract (budgeted, nightly)**
Read pending items from `harvest_state` — **no enumeration at all**. Navigate, wait for hydration (gotcha #7), extract, hash, upsert, mark harvested. Per-item commits mean any interruption resumes cleanly.

**Phase 3 — incremental (cheap, nightly once caught up)**
Shallow walk of the top ~20 rounds only. The list is recency-ordered, so new work is always at the top. Early-exit once several *non-stalled* rounds add nothing unknown.

**Phase 4 — close**
Close browser, release lock, write `harvest_runs` row. Wrapped in `try/finally`.

### Chunked backfill
The corpus is 6,362+ conversations at ~11s each — roughly **20 hours of extraction**. It runs as a nightly budgeted `extract` job reading pending items from the DB, so it converges over ~10 nights without any single long run. `discover` and `extract` are fully decoupled through `harvest_state`.

### Guardrails — fail loudly, never silently
| Guard | Rule |
|---|---|
| Zero enumeration | **Hard failure.** Means the nav never rendered; must never look like "nothing to do". |
| Empty extraction | A page that loads but yields **zero** prompts is an **error**. Abort if >35% of visited items. |
| Run log | Every run writes counts + error text to `harvest_runs`; the UI surfaces the `enumerated` trend so a collapse is visible. |

Rationale: the dominant failure mode for DOM scrapers is a UI change that quietly writes thousands of empty rows. Every guard exists to turn silence into a loud failure.

---

## 6. Web UI

Local **FastAPI + SQLite FTS5**, `http://127.0.0.1:8420`, single page.

- **Search:** full-text over prompt + title, snippet highlighting
- **Filters:** source, app context, agent, date range on `ts_observed`
- **Sort:** relevance · recency rank · first seen
- **Results:** prompt, title, source badge, deep link back to the live conversation/task
- **Backfilled rows are visibly marked "date approximate"** — non-negotiable given no source timestamps
- **Corpus view:** counts per source, run history, last successful run, **enumeration-count trend** (makes silent breakage visible at a glance)
- **Export:** filtered set → JSON / CSV / Markdown

Read-only. Binds `127.0.0.1` only.

---

## 7. Operational

**Schedule:** any scheduler (cron/Task Scheduler/agent automation), nightly **06:30**. Visible window ~2–3 min warm, ~5 min during backfill.

**Port note:** 8420 is arbitrary — any free local port works. Bind to `127.0.0.1` only.

**Failure handling**
| Mode | Handling |
|---|---|
| Profile lock held | Exit 2, retry next window |
| Session expired / MFA | Detect login redirect, surface "sign-in needed", abort |
| Crash mid-run | Per-item commits; resume from `harvest_state` |
| Browser left open | `try/finally` close + lock release |

**Safety:** navigation and scroll **only**. Never send, delete, rename, or re-run a task. No writes to Microsoft services.

---

## 8. Build status

| Phase | Work | Status |
|---|---|---|
| 0 | Spikes — enumeration limits, timestamps | ✅ Done (and later corrected) |
| 1 | Schema, lock, chat enumeration + extraction | ✅ Done |
| 2 | Cowork enumeration + extraction | ✅ Done |
| 3 | discover/extract split, checkpointing, guardrails | ✅ Done |
| 4 | Web UI (search, filters, export, corpus health) | ✅ Done |
| 5 | Process supervision + scheduled runs | ✅ Done |
| — | Full backfill | 🔄 In progress (~10 nights) |

Verified working end-to-end: 40 conversations → 73 prompts at a 2.5% empty rate.

---

## 9. Accepted limitations

- ~~**No source chronology.**~~ **Resolved.** M365 Copilot chat carries real
  per-message timestamps in the hydration payload — see gotcha #10. Cowork still
  has none (hash-routed SPA, no equivalent document payload), and rows harvested
  before this change keep `ts_source = NULL` until a backfill pass reaches them.
- **In-app Copilot invisible.** Word, Excel, PowerPoint, Outlook, Teams need Route B or C. Route A covers BizChat + Cowork only. **This is now the main argument for Route B**; the chronology argument no longer applies to chat.
- **Visible browser window nightly.** Unavoidable on the SSO profile.
- **Payload shape is unofficial.** It can change without notice. Mitigated by keeping the DOM extractor as a fallback and by `tests/test_payload.py`, which fails on the fixture the moment the shape moves.
- **UI-change fragility.** Mitigated by loud failure, and now partly *prevented* for the payload path.
- **Snapshot, not archive.** The corpus is what the UI shows *today*. If Microsoft trims the list, already-harvested rows are safe in the DB, but vanished items can't be re-verified. **This argues for running the backfill early.**
- **Scale.** 6,362+ conversations means ~20 hours of extraction. Budget accordingly; do not attempt it in one run.

---

## 10. Recommended next step for the wider goal

Build Route A (this project) **and open the Route B admin conversation in parallel**. Frame the ask as needing an *app registration*, not new data collection — the data already exists in the tenant.

The scale finding strengthens this considerably: a 6,000+ conversation corpus takes ~20 hours of brittle DOM scraping to extract, arrives with no timestamps, and still misses in-app Copilot entirely. The Graph route would return the same data in minutes, with real chronology and full surface coverage. Route A is the pragmatic answer; Route B is the correct one.
