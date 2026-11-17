"""Prompt Spider — GitHub Copilot (web / desktop app) collector.

The GitHub Copilot desktop app keeps nothing locally: it is a WebView2 shell
with cloud_session_storage_enabled, so its only local trace is a keystroke
autosave buffer. Its conversations do, however, surface in the web UI at
github.com/copilot, which is the same authenticated-DOM pattern we already use
for M365 Copilot.

We deliberately do NOT touch api.enterprise.githubcopilot.com or
/copilot_internal/*: those return 403 with an explicit anti-scraping notice.
This reads only pages a signed-in user can open in their own browser.

Two page types, two layouts:

  /copilot/c/{uuid}                 chat threads
                                    user turns: ChatMessage-module__user
  /copilot/tasks/{uuid}             agent / CLI sessions
  /{org}/{repo}/tasks/{uuid}        repo-scoped agent sessions
                                    user turns: UserMessage-module__bubble

Class names are CSS modules with a rotating hash suffix
(UserMessage-module__bubble__qfya7), so every selector matches on the stable
prefix only.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from browser import SignInRequired

GH_BASE = "https://github.com"
GH_COPILOT = f"{GH_BASE}/copilot"

CHAT_RE = re.compile(r"^/copilot/c/([0-9a-f-]{36})$", re.I)
TASK_RE = re.compile(r"^(?:/copilot|/[^/]+/[^/]+)/tasks/([0-9a-f-]{36})$", re.I)

# Sidebar links to every reachable conversation and agent session.
_LINKS = """() => [...document.querySelectorAll('a[href]')]
    .map(a => [a.getAttribute('href').split('?')[0],
               (a.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120)])
    .filter(([h]) => /\\/copilot\\/c\\/|\\/tasks\\//.test(h))"""

# Prompt extraction. Tries the task layout, then the chat layout, then a plain
# "You said:" text split for anything that changes shape underneath us.
_EXTRACT = """
() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const strip = s => clean(s)
      .replace(/^Previous page/i, '')
      .replace(/^You said:\\s*/i, '')
      .trim();
  // The accessible header duplicates the bubble text ("You said: X" + "X"),
  // which lands as an exact doubling once whitespace is collapsed.
  const undouble = t => {
    if (t.length % 2 === 0) {
      const h = t.length / 2;
      if (t.slice(0, h) === t.slice(h)) return t.slice(0, h);
    }
    return t;
  };
  const pick = sel => {
    const all = [...document.querySelectorAll(sel)];
    // Keep only leaf-most matches: these containers nest.
    return all.filter(e => !all.some(o => o !== e && e.contains(o)));
  };

  for (const sel of ['[class*="UserMessage-module__bubble"]',
                     '[class*="ChatMessage-module__user"]']) {
    const nodes = pick(sel);
    if (nodes.length) {
      const out = nodes.map(e => ({ prompt: undouble(strip(e.textContent)), response: '' }))
                       .filter(x => x.prompt);
      if (out.length) return out;
    }
  }

  const main = document.querySelector('main') || document.body;
  const parts = clean(main.textContent).split(/You said:/i).slice(1);
  return parts.map(p => {
    const seg = p.split(/Copilot said:/i);
    return { prompt: strip(seg[0]), response: clean(seg[1] || '').slice(0, 2000) };
  }).filter(x => x.prompt);
}
"""

# Session-level timestamps. GitHub does not expose per-turn times on either
# layout, so the earliest timestamp inside the transcript is the best real
# anchor we have — still far better than the harvest time we fall back to for
# M365 Copilot.
_TIMES = """() => {
  const main = document.querySelector('main') || document.body;
  return [...main.querySelectorAll('relative-time,[datetime]')]
      .map(e => e.getAttribute('datetime')).filter(Boolean);
}"""

# The conversation's own header title. The sidebar anchor text is polluted with
# badge copy ("CLI session (read only)", "Loading"), so prefer this.
_TITLE = """() => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const el = document.querySelector('[class*="TaskChatHeader-module__headerTitle"]')
          || document.querySelector('h1');
  const fromEl = clean(el && el.textContent);
  if (fromEl) return fromEl;
  return clean(document.title).replace(/\\s*[·|]\\s*GitHub Copilot$/i, '');
}"""

# A `copilot --remote` session that was never synced renders as a placeholder
# shell: connection banner, no transcript. Those prompts live in the local CLI
# session store we already read, so an empty result here is correct rather than
# a broken selector — we must not let it count against the empty-ratio guardrail.
_IS_REMOTE_SHELL = """() => {
  const main = document.querySelector('main') || document.body;
  const t = (main.innerText || '');
  return document.querySelector('[class*="CLIChatInputHeader-module__"]') !== null
      || /copilot --remote --resume/i.test(t);
}"""


@dataclass
class GhItem:
    session_id: str
    kind: str          # 'chat' | 'task'
    title: str
    url: str


def _norm(href: str) -> str:
    return href if href.startswith("http") else f"{GH_BASE}{href}"


# Sidebar anchors and page headers append status badges to the conversation
# name, sometimes mid-string ("<name> CLI session (read only) … · owner/repo"),
# so everything from the badge onward is dropped. The titles below carry no
# information at all.
_BADGE_RE = re.compile(r"(CLI session \(read only\)|New remote session).*$", re.I)
_PLACEHOLDER_TITLES = {"", "new conversation", "sso to see this thread", "loading",
                       "copilot chat", "github copilot", "copilot"}


def clean_github_title(raw: str) -> str:
    """Strip GitHub's status badges and generic page titles off a title.

    Public because `harvest --mode repair` rewrites titles captured before this
    normalisation existed, for sessions the collector will never revisit.
    """
    t = _BADGE_RE.sub("", (raw or "").strip())
    t = re.sub(r"^Loading\s+", "", t, flags=re.I)
    t = re.sub(r"\s*[·|]\s*(GitHub Copilot|[\w.-]+/[\w.-]+)\s*$", "", t, flags=re.I)
    t = re.sub(r"[\ufffd\u2026]+$", "", t).strip(" -–—·|").strip()
    return "" if t.lower() in _PLACEHOLDER_TITLES else t


def _best_title(sidebar: str, page_title: str, first_prompt: str) -> str:
    for cand in (clean_github_title(page_title), clean_github_title(sidebar)):
        if cand:
            return cand
    return (first_prompt or "").strip()[:120]


def _assert_gh_signed_in(page, settle_ms: int = 12_000) -> None:
    """Raise only on a persistent GitHub sign-in redirect.

    browser.assert_signed_in watches for the M365 login host, which never
    appears here; GitHub bounces to /login or /session instead.
    """
    def on_login() -> bool:
        u = (page.url or "").lower()
        return "github.com/login" in u or "github.com/session" in u

    if not on_login():
        return
    deadline = time.time() + settle_ms / 1000
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if not on_login():
            return
    raise SignInRequired("GitHub sign-in required — open github.com/copilot and log in")


def enumerate_github(page, *, settle_ms: int = 9000) -> list[GhItem]:
    """List every chat thread and agent session visible in the sidebar."""
    page.goto(GH_COPILOT, wait_until="domcontentloaded")
    page.wait_for_timeout(settle_ms)
    _assert_gh_signed_in(page)

    items: dict[str, GhItem] = {}
    for href, title in page.evaluate(_LINKS):
        path = href.split("#")[0]
        m = CHAT_RE.match(path)
        if m:
            items.setdefault(m.group(1), GhItem(m.group(1), "chat", title, _norm(path)))
            continue
        m = TASK_RE.match(path)
        if m:
            items.setdefault(m.group(1), GhItem(m.group(1), "task", title, _norm(path)))
    return list(items.values())


def extract_github(page, item: GhItem, *,
                   settle_ms: int = 9000) -> tuple[list[dict], str | None, bool]:
    """Open one conversation.

    Returns (prompt blocks, session timestamp, has_transcript). A False
    has_transcript means the page is an unsynced `copilot --remote` shell that
    legitimately holds no prompts, not a failed extraction.
    """
    page.goto(item.url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    _assert_gh_signed_in(page)

    deadline = time.time() + 25
    blocks: list[dict] = []
    while time.time() < deadline:
        blocks = page.evaluate(_EXTRACT) or []
        if blocks:
            break
        page.wait_for_timeout(1200)
    else:
        page.wait_for_timeout(settle_ms)
        blocks = page.evaluate(_EXTRACT) or []

    has_transcript = bool(blocks) or not page.evaluate(_IS_REMOTE_SHELL)
    times = sorted(t for t in (page.evaluate(_TIMES) or []) if t)
    if blocks:
        item.title = _best_title(item.title, page.evaluate(_TITLE) or "",
                                 blocks[0].get("prompt", ""))
    return blocks, (times[0] if times else None), has_transcript
