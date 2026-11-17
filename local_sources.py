"""Prompt Spider — local-file collectors.

Three surfaces whose prompts sit in local files rather than behind a browser:

  scout        Microsoft Scout desktop app       ~/.scout/copilot/session-store.db
  copilot-cli  GitHub Copilot CLI / ADO          same store, sessions.host_type
  vscode       GitHub Copilot Chat in VS Code    %APPDATA%/Code/User/**/chatSessions

Unlike the M365 surfaces these carry REAL timestamps, so ts_source is populated
and the UI does not have to caveat their dates.

The interesting problem here is signal, not access. In the Scout store 93% of
turns are agent personas, scheduled heartbeats and injected context rather than
things a human typed. We classify instead of discarding: everything is stored
with an `origin`, and the UI defaults to human-only. Filters can be retuned
later; deleted rows cannot.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))

SCOUT_DB = HOME / ".scout" / "copilot" / "session-store.db"
SCOUT_ARCHIVE_DB = HOME / ".copilot" / "session-store.db"
VSCODE_USER = APPDATA / "Code" / "User"


@dataclass
class LocalPrompt:
    source: str
    session_id: str
    turn_index: int
    prompt: str
    response_excerpt: str = ""
    title: str = ""
    ts_source: str | None = None
    origin: str = "human"
    app_context: str = ""
    deep_link: str = ""


# --------------------------------------------------------------------- classify

# Blocks the host appends to a user's message before sending it to the model.
# These are not part of what the person typed, so we strip them rather than let
# them pollute search results and snippets.
_INJECTED = [
    # Host context block. The product has been renamed over time (Clawpilot ->
    # Microsoft Scout), so match the shape rather than the brand name. Titles
    # are stored truncated, so the block may be cut off mid-way with no closing
    # bracket - hence the optional tail.
    re.compile(r"\n?\s*\[[A-Za-z0-9 ._-]{0,40}context:\s*[^\]]*\]?\s*$", re.S | re.I),
    re.compile(r"\n?\s*\[[A-Za-z0-9 ._-]{0,40}context:\s[^\]]*\]\s*", re.S | re.I),
    re.compile(r"\n?\s*<system_reminder>.*?</system_reminder>\s*", re.S | re.I),
    re.compile(r"\n?\s*<current_datetime>.*?</current_datetime>\s*", re.S | re.I),
    re.compile(r"\n?\s*<sql_tables>.*?</sql_tables>\s*", re.S | re.I),
    re.compile(r"\n?\s*<todo_status>.*?</todo_status>\s*", re.S | re.I),
    # Attachment metadata the composer prepends when files/images are attached.
    re.compile(r'^\s*\{"text":"[^"]*","pictures":\[[^\]]*\]\}\s*'),
]


def strip_injected(prompt: str) -> str:
    """Remove host-appended context so the stored prompt is what was typed."""
    out = prompt
    for pat in _INJECTED:
        out = pat.sub("", out)
    return out.strip()


# Ordered: first match wins. Anchored to the start of the prompt wherever
# possible so a human quoting one of these phrases mid-sentence still counts.
_ORIGIN_RULES: list[tuple[str, re.Pattern]] = [
    ("system",    re.compile(r"^\s*<skill-context", re.I)),
    ("system",    re.compile(r"^\s*<system[-_]reminder", re.I)),
    ("system",    re.compile(r"^\s*Conversation:\s*\n", re.I)),
    ("system",    re.compile(r"^\s*Here is the conversation:", re.I)),
    ("system",    re.compile(r"^\s*Summarize the following conversation", re.I)),
    ("system",    re.compile(r"^\s*\[A scheduled automation\b", re.I)),
    ("system",    re.compile(r"^\s*\[Automation\b", re.I)),
    # Internal shell-command safety classifier, not a user prompt.
    ("system",    re.compile(r"^\s*Classify this command:", re.I)),
    # Sub-agent briefs that quote the user; the original turn is stored separately.
    ("system",    re.compile(r"^\s*USER ASKED:", re.I)),
    ("heartbeat", re.compile(r"scheduled heartbeat", re.I)),
    ("automation", re.compile(r"^\s*You are\b", re.I)),
    ("automation", re.compile(r"^\s*Run the .{0,60}(automation|harvest|sync)\b", re.I)),
    ("automation", re.compile(r"^\s*Act as\b", re.I)),
]


def classify(prompt: str) -> str:
    """Label a prompt's origin: human | automation | heartbeat | system."""
    for origin, pat in _ORIGIN_RULES:
        if pat.search(prompt):
            return origin
    return "human"


# ------------------------------------------------------------------- scout / cli

def _snapshot(db_path: Path) -> Path:
    """Copy a live WAL database to a temp file we can read safely.

    The Scout store is written continuously; VACUUM INTO gives a consistent
    point-in-time copy without taking a write lock on the original.
    """
    tmp = Path(tempfile.gettempdir()) / f"ps-snap-{uuid.uuid4().hex}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()
    return tmp


def _iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(
            timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return ts


# Scout and the GitHub Copilot CLI share one store; host_type tells them apart.
_HOST_SOURCE = {"github": "copilot-cli", "ado": "copilot-cli"}


def collect_session_store(db_path: Path) -> list[LocalPrompt]:
    """Read prompts from a Scout / Copilot-CLI session store."""
    if not db_path.exists():
        return []
    snap = _snapshot(db_path)
    out: list[LocalPrompt] = []
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT t.session_id, t.turn_index, t.user_message, t.assistant_response,"
            "       t.timestamp, s.summary, s.repository, s.branch, s.cwd, s.host_type"
            "  FROM turns t JOIN sessions s ON s.id = t.session_id"
            " WHERE t.user_message IS NOT NULL AND t.user_message <> ''")
        for r in rows:
            raw = (r["user_message"] or "").strip()
            prompt = strip_injected(raw)
            if not prompt:
                continue
            ctx = r["repository"] or r["cwd"] or ""
            if r["branch"]:
                ctx = f"{ctx} @{r['branch']}"
            # Scout often stores the first user message as the session summary,
            # which would render the prompt twice on a card. Drop it when it is
            # just an echo of the prompt.
            summary = strip_injected(r["summary"] or "")
            title = "" if summary[:120] == prompt[:120] else summary[:200]
            out.append(LocalPrompt(
                source=_HOST_SOURCE.get(r["host_type"] or "", "scout"),
                session_id=r["session_id"],
                turn_index=r["turn_index"],
                prompt=prompt,
                response_excerpt=(r["assistant_response"] or "")[:2000],
                title=title,
                ts_source=_iso(r["timestamp"]),
                origin=classify(raw),
                app_context=(ctx or "")[:200],
            ))
    finally:
        conn.close()
        snap.unlink(missing_ok=True)
    return out


def collect_scout() -> list[LocalPrompt]:
    """Live store plus the dormant pre-June archive."""
    return collect_session_store(SCOUT_DB) + collect_session_store(SCOUT_ARCHIVE_DB)


# ------------------------------------------------------------------------ vscode

def _vscode_session_files() -> list[Path]:
    files: list[Path] = []
    empty = VSCODE_USER / "globalStorage" / "emptyWindowChatSessions"
    if empty.is_dir():
        files += [p for p in empty.iterdir() if p.suffix in (".json", ".jsonl")]
    ws = VSCODE_USER / "workspaceStorage"
    if ws.is_dir():
        for d in ws.iterdir():
            cs = d / "chatSessions"
            if cs.is_dir():
                files += [p for p in cs.iterdir() if p.suffix in (".json", ".jsonl")]
    profiles = VSCODE_USER / "profiles"
    if profiles.is_dir():
        for prof in profiles.iterdir():
            for sub in ("globalStorage/emptyWindowChatSessions", "chatSessions"):
                d = prof / sub
                if d.is_dir():
                    files += [p for p in d.iterdir() if p.suffix in (".json", ".jsonl")]
    return files


def _replay_jsonl(path: Path) -> tuple[list[dict], str]:
    """Rebuild a chat session from VS Code's append-only journal.

    Newer VS Code writes .jsonl where each line is a patch:
      kind 0 -> full snapshot (has requests[])
      kind 1 -> set value at key path (e.g. customTitle)
      kind 2 -> append to a collection (usually requests)
    Reading only the first line would miss every turn after the first save.
    """
    requests: list[dict] = []
    title = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        kind, key, val = o.get("kind"), o.get("k"), o.get("v")
        if kind == 0 and isinstance(val, dict):
            requests = list(val.get("requests") or [])
            title = val.get("customTitle") or title
        elif kind == 1 and key == ["customTitle"] and isinstance(val, str):
            title = val
        elif kind == 2 and key == ["requests"]:
            requests.extend(val if isinstance(val, list) else [val])
    return requests, title


def collect_vscode() -> list[LocalPrompt]:
    out: list[LocalPrompt] = []
    for path in _vscode_session_files():
        try:
            if path.suffix == ".jsonl":
                requests, title = _replay_jsonl(path)
            else:
                doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                requests = doc.get("requests") or []
                title = doc.get("customTitle") or ""
        except Exception:
            continue

        session_id = path.stem
        for idx, req in enumerate(requests):
            if not isinstance(req, dict):
                continue
            prompt = ((req.get("message") or {}).get("text") or "").strip()
            if not prompt:
                continue
            ts = req.get("timestamp")
            ts_iso = None
            if isinstance(ts, (int, float)):
                ts_iso = datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(
                    timespec="seconds")
            resp = req.get("response")
            if isinstance(resp, list):
                resp = " ".join(
                    p.get("value", "") for p in resp
                    if isinstance(p, dict) and isinstance(p.get("value"), str))
            elif not isinstance(resp, str):
                resp = ""
            agent = (req.get("agent") or {}).get("id") or ""
            out.append(LocalPrompt(
                source="vscode",
                session_id=session_id,
                turn_index=idx,
                prompt=prompt,
                response_excerpt=(resp or "")[:2000],
                title=title[:200],
                ts_source=ts_iso,
                origin=classify(prompt),
                app_context=agent[:200],
            ))
    return out


COLLECTORS = {
    "scout": collect_scout,          # also yields copilot-cli rows via host_type
    "vscode": collect_vscode,
}
