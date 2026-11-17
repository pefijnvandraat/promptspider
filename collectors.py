"""Prompt Spider — enumeration + extraction for M365 Copilot chat and Cowork.

Everything here encodes findings from live DOM probing. See PLAN-HANDOFF.md.

The single most important lesson: enumeration MUST be driven from Python with
real mouse-wheel events. Setting scrollTop from JS loads about two pages and
then stalls forever; that stall was originally misread as "the list ends at 74".
Wheel-driven scrolling reached 4,393+ conversations and was still going.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import unquote

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from browser import assert_signed_in, ensure_nav_expanded, goto_chat
from payload import parse_hydration

CONV_RE = re.compile(
    r"/chat/(?:agent/(?P<agent>[^/]+)/)?conversation/(?P<guid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
TASK_RE = re.compile(r"/task/([^?#]+)")

BASE = "https://m365.cloud.microsoft"
_COWORK_NAV_SELECTOR = (
    'a[href], button, [role="button"], [role="link"], '
    '[role="menuitem"], [role="tab"]'
)

# Anchor rect for wheel targeting. Re-queried each round: the element survives,
# but its geometry changes as the list grows.
_NAV_BOX = """() => {
  const e = document.querySelector('.fui-NavDrawerBody');
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
}"""

# Collect href + title together. Titles must be read in the same pass as hrefs;
# a second pass costs another full DOM walk, which gets very slow once the list
# is thousands of nodes deep.
_ENTRIES = """(sel) => [...document.querySelectorAll(sel)].map(a => [
    (a.getAttribute('href') || '').split('?')[0],
    (a.innerText || a.getAttribute('aria-label') || '')
        .replace(/Press Tab[\\s\\S]*$/i, '').replace(/\\s+/g, ' ').trim()
])"""

# True once the transcript has rendered at least one user turn. Conversations
# hydrate at wildly different speeds (observed 6s-15s), so we wait on this
# condition instead of a fixed sleep — a fixed 6s wait produced 65% empty reads.
# Uses textContent because Cowork's user containers are CSS-hidden.
_HAS_CONTENT = """() => {
  if (document.querySelector('.fai-UserMessage')) return true;
  const main = document.querySelector('main') || document.body;
  return /You said/i.test(main.textContent || '');
}"""

# Prompt extraction.
#
# Primary path keys off `.fai-UserMessage`, the semantic container Copilot uses
# for user turns on BOTH chat and Cowork. Two details matter:
#
#  * Read `textContent`, NOT `innerText`. On Cowork these containers are
#    CSS-hidden, so innerText returns "" and any innerText-based walk finds
#    nothing — which is why Cowork silently extracted zero prompts.
#  * Responses are paired by walking the chat container in DOM order: anything
#    after a user turn and before the next one belongs to that turn.
#
# The old "You said:" text-walk is kept as a fallback for layouts that predate
# or diverge from this class.
_EXTRACT = """
() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const strip = s => clean(s)
      .replace(/^You said:?\\s*/i, '')
      .replace(/^\\{"text":"[^"]*","pictures":\\[[^\\]]*\\]\\}\\s*/, '');   // attachment metadata
  const users = [...document.querySelectorAll('.fai-UserMessage')];
  if (users.length) {
    // Find the common chat container so we can pair responses in DOM order.
    //
    // closest() FIRST, deliberately. document.querySelector returns the first
    // .fai-CopilotChat in the document, which on chat can be an outer shell
    // that does not contain the turns — then idxOf() returns -1 for every
    // message, the m.i >= 0 guard fails, and every response comes back empty.
    // That is exactly the 100%-prompts / 0%-responses split seen in the store.
    // An ancestor of a user message cannot have that problem.
    let chat = users[0].closest('.fai-CopilotChat')
            || document.querySelector('.fai-CopilotChat')
            || document.querySelector('main') || document.body;
    if (!chat.contains(users[0])) chat = document.querySelector('main') || document.body;
    const rows = [...chat.children];
    const idxOf = u => rows.findIndex(r => r === u || r.contains(u));

    const out = [];
    const marks = users.map(u => ({ u, i: idxOf(u) }));
    marks.forEach((m, k) => {
      const prompt = strip(m.u.textContent);
      if (!prompt) return;
      let resp = '';
      if (m.i >= 0) {
        const end = k + 1 < marks.length && marks[k + 1].i >= 0 ? marks[k + 1].i : rows.length;
        resp = clean(rows.slice(m.i + 1, end).map(r => r.textContent).join(' '))
                 .replace(/^Copilot said:?\\s*/i, '');
      }
      out.push({ prompt, response: resp.slice(0, 2000) });
    });
    if (out.length) return out;
  }

  // Fallback: split the transcript on the role labels.
  const main = document.querySelector('main') || document.body;
  const parts = clean(main.textContent).split(/You said:/i).slice(1);
  const out = [];
  for (const p of parts) {
    const seg = p.split(/Copilot said:/i);
    const prompt = strip(seg[0]);
    if (prompt) out.push({ prompt, response: clean(seg[1] || '').slice(0, 2000) });
  }
  return out;
}
"""


@dataclass
class Item:
    """One enumerated conversation or task, before extraction."""
    source: str
    session_id: str
    title: str = ""
    agent_id: str | None = None
    recency_rank: int = 0
    deep_link: str = ""
    app_context: str = "chat"


@dataclass
class Enumeration:
    items: list[Item] = field(default_factory=list)
    growth: list[int] = field(default_factory=list)
    exhausted: bool = False
    stopped_early: bool = False
    elapsed_s: float = 0.0

    @property
    def agent_count(self) -> int:
        return sum(1 for i in self.items if i.agent_id)


class CoworkNavigationError(RuntimeError):
    """The Cowork navigation control is missing or could not be activated."""


def _scroll_collect(
    page,
    selector: str,
    *,
    max_rounds: int,
    stall_limit: int,
    known: set[str] | None = None,
    known_streak_stop: int = 0,
    time_budget_s: float | None = None,
    progress: Callable[[int, int], None] | None = None,
    on_batch: Callable[[list[tuple[str, str]]], None] | None = None,
    batch_every: int = 10,
) -> tuple[dict[str, str], list[int], bool, bool, float]:
    """Wheel-scroll the nav, accumulating href -> title until exhausted.

    known / known_streak_stop implement the incremental early-exit: the list is
    recency-ordered, so once several consecutive rounds add nothing we have not
    already harvested, everything further down is older and already stored.

    on_batch is called every `batch_every` rounds with newly-seen entries so a
    long walk can be checkpointed. A full enumeration takes hours; without this
    an interrupted run would lose all of it.

    Returns (entries, growth, exhausted, stopped_early, elapsed).
    """
    entries: dict[str, str] = {}
    unflushed: list[tuple[str, str]] = []
    growth: list[int] = []
    stall = 0
    known_streak = 0
    exhausted = False
    stopped_early = False
    t0 = time.time()

    for rnd in range(max_rounds):
        box = page.evaluate(_NAV_BOX)
        if not box:
            break
        page.mouse.move(box["x"], box["y"])

        # Every few stalled rounds, back off and re-drive: the loader pauses
        # intermittently and this re-arms it. Without this the walk terminates
        # early and silently returns a fraction of the corpus.
        if stall and stall % 5 == 0:
            page.mouse.wheel(0, -1500)
            page.wait_for_timeout(600)

        for _ in range(6):
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(140)
        page.wait_for_timeout(2200)

        before = len(entries)
        new_unknown = 0
        for href, title in page.evaluate(_ENTRIES, selector):
            if not href:
                continue
            if href not in entries:
                if known is None or href not in known:
                    new_unknown += 1
                entries[href] = title
                unflushed.append((href, title))
            elif not entries[href] and title:
                entries[href] = title

        added = len(entries) - before
        stall = stall + 1 if added == 0 else 0
        if added:
            growth.append(len(entries))
        if progress:
            progress(rnd, len(entries))

        if on_batch and unflushed and rnd % batch_every == 0:
            on_batch(unflushed)
            unflushed = []

        if stall >= stall_limit:
            exhausted = True
            break

        # Incremental early-exit. Only counts rounds that actually LOADED items
        # and found them all already-known. A stalled round (added == 0) must
        # not count: conflating "loader paused" with "reached known history" is
        # exactly the bug that made the corpus look like 74 conversations.
        if known_streak_stop:
            if added > 0:
                known_streak = known_streak + 1 if new_unknown == 0 else 0
            if known_streak >= known_streak_stop:
                stopped_early = True
                break

        if time_budget_s and (time.time() - t0) > time_budget_s:
            break

    if on_batch and unflushed:
        on_batch(unflushed)
    return entries, growth, exhausted, stopped_early, time.time() - t0


def _to_chat_items(entries: Iterable[tuple[str, str]]) -> list[Item]:
    items: list[Item] = []
    for rank, (href, title) in enumerate(entries):
        m = CONV_RE.search(href)
        if not m:
            continue
        items.append(Item(
            source="m365-copilot",
            session_id=m.group("guid"),
            title=title or "",
            agent_id=m.group("agent"),
            recency_rank=rank,
            deep_link=f"{BASE}{href}",
            app_context="chat",
        ))
    return items


def _to_cowork_items(entries: Iterable[tuple[str, str]]) -> list[Item]:
    items: list[Item] = []
    for rank, (href, title) in enumerate(entries):
        m = TASK_RE.search(href)
        if not m:
            continue
        task_id = unquote(m.group(1))
        items.append(Item(
            source="cowork",
            session_id=task_id,
            title=title or "",
            recency_rank=rank,
            deep_link=f"{BASE}/cowork#/task/{task_id}",
            app_context="task",
        ))
    return items


def enumerate_chat(
    page,
    *,
    max_rounds: int = 600,
    stall_limit: int = 25,
    known: set[str] | None = None,
    known_streak_stop: int = 0,
    time_budget_s: float | None = None,
    progress: Callable[[int, int], None] | None = None,
    on_batch: Callable[[list[tuple[str, str]]], None] | None = None,
) -> Enumeration:
    """Enumerate chat conversations, including agent-scoped ones."""
    goto_chat(page)
    entries, growth, exhausted, early, elapsed = _scroll_collect(
        page, 'a[href*="/conversation/"]',
        max_rounds=max_rounds, stall_limit=stall_limit, known=known,
        known_streak_stop=known_streak_stop, time_budget_s=time_budget_s,
        progress=progress, on_batch=on_batch,
    )
    return Enumeration(_to_chat_items(entries.items()), growth, exhausted, early, elapsed)


def open_cowork(page) -> None:
    """Reach the Cowork surface.

    Direct navigation to /cowork renders a blank page; the nav item must be
    clicked from /chat to trigger the SSR transition.
    """
    goto_chat(page)
    controls = page.locator(_COWORK_NAV_SELECTOR).filter(
        has_text=re.compile(r"^\s*Cowork\s*$", re.IGNORECASE)
    )
    visible = [
        controls.nth(i)
        for i in range(controls.count())
        if controls.nth(i).is_visible() and controls.nth(i).is_enabled()
    ]
    if len(visible) != 1:
        raise CoworkNavigationError(
            "Expected one visible, enabled Cowork navigation control; "
            f"found {len(visible)}"
        )
    try:
        visible[0].click(timeout=5_000)
    except PlaywrightTimeoutError as exc:
        raise CoworkNavigationError(
            "Cowork navigation control could not be activated within 5 seconds"
        ) from exc
    page.wait_for_timeout(9000)
    assert_signed_in(page)
    ensure_nav_expanded(page)


def enumerate_cowork(
    page,
    *,
    max_rounds: int = 600,
    stall_limit: int = 25,
    known: set[str] | None = None,
    known_streak_stop: int = 0,
    time_budget_s: float | None = None,
    progress: Callable[[int, int], None] | None = None,
    on_batch: Callable[[list[tuple[str, str]]], None] | None = None,
) -> Enumeration:
    """Enumerate Cowork tasks."""
    open_cowork(page)
    entries, growth, exhausted, early, elapsed = _scroll_collect(
        page, 'a[href*="/task/"]',
        max_rounds=max_rounds, stall_limit=stall_limit, known=known,
        known_streak_stop=known_streak_stop, time_budget_s=time_budget_s,
        progress=progress, on_batch=on_batch,
    )
    return Enumeration(_to_cowork_items(entries.items()), growth, exhausted, early, elapsed)


def _await_content(page, timeout_ms: int = 25_000) -> bool:
    """Wait for the transcript to hydrate. False if it never does."""
    try:
        page.wait_for_function(_HAS_CONTENT, timeout=timeout_ms)
        page.wait_for_timeout(1200)      # let the last blocks settle
        return True
    except Exception:
        return False


def extract_chat(page, item: Item) -> list[dict]:
    """Open a conversation and pull its prompt/response pairs.

    Two paths, in order of preference:

      1. The hydration payload that ships with the document. It carries the
         full transcript plus per-message `createdAt`, and it arrives BEFORE
         client-side hydration — so when it parses we skip the 6-15s content
         wait entirely. It is also anchored on semantic keys rather than the
         generated CSS class hashes that rotate every deploy (gotcha #4).

      2. The rendered DOM, unchanged. Kept because the payload shape is
         unofficial and could disappear without notice.

    Falling through to (2) is expected and fine; getting nothing from either is
    what the empty-ratio guardrail is for.
    """
    captured: list[str] = []

    def on_response(resp):
        try:
            # CONV_RE, not a literal "/chat/conversation/" test: roughly a fifth
            # of conversations are agent-scoped and live at
            # /chat/agent/{agent}/conversation/{guid}. A substring check on the
            # unscoped shape silently skips every one of them (gotcha #1).
            if CONV_RE.search(resp.url):
                captured.append(resp.text())
        except Exception:
            pass                      # a body we cannot read is not a failure

    page.on("response", on_response)
    try:
        page.goto(item.deep_link, wait_until="domcontentloaded")
        assert_signed_in(page)

        # Poll rather than sleep a fixed interval. The document response can lag
        # domcontentloaded, and a short fixed wait makes slow-but-healthy
        # conversations look like "no payload" and fall back unnecessarily.
        for _ in range(12):
            for html in captured:
                parsed = parse_hydration(html)
                if parsed["turns"]:
                    return _blocks_from_payload(parsed)
            page.wait_for_timeout(500)

        if not _await_content(page):
            return []
        return page.evaluate(_EXTRACT) or []
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


def _blocks_from_payload(parsed: dict) -> list[dict]:
    """Payload turns -> the block shape `harvest.store` already consumes."""
    conv = parsed.get("conversation") or {}
    blocks = []
    for turn in parsed["turns"]:
        blocks.append({
            "prompt": turn["prompt"],
            "response": turn["response"],
            "ts_source": turn["ts_source"],
            "author": turn["author"],
            "message_id": turn["message_id"],
            "title": conv.get("title"),
            "created_utc": conv.get("created_utc"),
            "updated_utc": conv.get("updated_utc"),
        })
    return blocks


def _first_prompt_sig(page) -> str:
    """Cheap fingerprint of the currently-rendered transcript."""
    try:
        return page.evaluate(
            """() => { const u = document.querySelector('.fai-UserMessage');
                       return u ? (u.textContent || '').slice(0, 120) : ''; }""") or ""
    except Exception:
        return ""


def extract_cowork(page, item: Item) -> list[dict]:
    """Open a Cowork task via its hash route and pull its prompt blocks.

    The hash route only works once the Cowork SPA is loaded, so callers must
    have visited Cowork first (open_cowork).

    Hash navigation swaps content in place, so the previous task stays on screen
    for a moment. Waiting only for "content exists" would read the PREVIOUS
    task, so we wait for the transcript fingerprint to actually change.
    """
    if "/cowork" not in page.url:
        open_cowork(page)
    before = _first_prompt_sig(page)
    page.evaluate("(h) => { window.location.hash = h; }", item.deep_link.split("#", 1)[1])
    page.wait_for_timeout(2500)
    assert_signed_in(page)

    deadline = time.time() + 25
    while time.time() < deadline:
        sig = _first_prompt_sig(page)
        if sig and sig != before:
            page.wait_for_timeout(1200)      # let remaining turns settle
            return page.evaluate(_EXTRACT) or []
        page.wait_for_timeout(800)

    # Fingerprint never changed: either a single-turn task identical to the last,
    # or it genuinely failed to load. Fall through and let the caller judge.
    return page.evaluate(_EXTRACT) or []
