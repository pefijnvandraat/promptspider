"""Prompt Spider — re-harvest alignment check. READ ONLY.

Before backfilling timestamps onto conversations we already hold, we need to
know that the payload produces the SAME number of turns the DOM extractor did.

Why this matters more than it sounds: `prompts.id` is
sha256(source|session|turn_index). If the payload yields a different turn count
or ordering, every turn past the divergence point gets a fresh id, so a
"backfill" would INSERT duplicates instead of enriching rows — silently, and
the corpus has almost no cross-source duplication for that to show up against.

This opens each conversation, parses the payload, compares counts, and writes
nothing at all. Run it, read the number, then decide.

    py align_check.py --limit 40
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter

import db
from browser import SignInRequired, browser_page, goto_chat, run_lock
from collectors import BASE, CONV_RE
from payload import parse_hydration

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BAR = "=" * 74


def sample(conn, limit: int, source: str = "m365-copilot") -> list[tuple]:
    """Already-harvested conversations, newest discovery first."""
    return conn.execute(
        "SELECT hs.source_session_id, hs.agent_id, hs.title,"
        "       COUNT(p.id)                                        AS stored_rows,"
        "       SUM(CASE WHEN p.ts_source IS NULL THEN 1 ELSE 0 END) AS missing_ts,"
        "       SUM(CASE WHEN COALESCE(p.response_excerpt,'')='' THEN 1 ELSE 0 END) AS missing_resp"
        "  FROM harvest_state hs"
        "  JOIN prompts p ON p.source = hs.source"
        "                AND p.source_session_id = hs.source_session_id"
        " WHERE hs.source = ? AND hs.last_harvested_at IS NOT NULL"
        " GROUP BY hs.source_session_id"
        " ORDER BY hs.discovered_at DESC"
        " LIMIT ?", (source, limit)).fetchall()


def stored_turns(conn, sid: str, source: str = "m365-copilot") -> list[tuple[int, str]]:
    return [(r["turn_index"], r["prompt"]) for r in conn.execute(
        "SELECT turn_index, prompt FROM prompts"
        " WHERE source=? AND source_session_id=? ORDER BY turn_index", (source, sid))]


def norm(text: str) -> str:
    """Compare on shape, not bytes.

    The DOM extractor stripped a 'You said:' prefix and attachment metadata that
    the payload still carries, so exact equality would report false divergence.
    """
    return " ".join((text or "").split()).lower()[:120]


def mask(text: str, width: int = 70) -> str:
    """Structural view for diagnosis: punctuation kept, words shown as length.

    Enough to recognise a leading attachment blob or a role prefix without
    printing the content of a prompt to a terminal or a log. Forced to ASCII:
    these run on cp1252 consoles where a stray en-dash kills the process.
    """
    import re as _re
    masked = _re.sub(r"[A-Za-z0-9_]+", lambda m: f"~{len(m.group(0))}",
                     " ".join((text or "").split()))
    return masked.encode("ascii", "replace").decode()[:width]


def first_divergence(stored: list[tuple[int, str]], turns: list[dict]):
    for idx, prompt in stored:
        if idx >= len(turns):
            return idx, prompt, None
        if norm(prompt) != norm(turns[idx]["prompt"]):
            return idx, prompt, turns[idx]["prompt"]
    return None


def classify(stored: list[tuple[int, str]], turns: list[dict]) -> str:
    """Would a re-harvest enrich these rows, or duplicate them?

    Counts alone are not enough. What actually matters is whether the prompt at
    turn_index i is the SAME prompt in both, because that index is baked into
    the row id. Extra turns AFTER every stored row are harmless (they append);
    a mismatch at any shared index shifts every later id.
    """
    if not turns:
        return "no-payload"
    for idx, prompt in stored:
        if idx >= len(turns):
            return "divergent"
        if norm(prompt) != norm(turns[idx]["prompt"]):
            return "divergent"
    if len(turns) > len(stored):
        return "extra-trailing"
    return "aligned"


def deep_link(sid: str, agent: str | None) -> str:
    path = (f"/chat/agent/{agent}/conversation/{sid}" if agent
            else f"/chat/conversation/{sid}")
    return f"{BASE}{path}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry-run re-harvest alignment check (read only)")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--budget-min", type=float, default=20.0)
    args = ap.parse_args()

    # Read-only URI. as_uri() is required on Windows: a raw "file:C:\..." path
    # keeps backslashes, which SQLite will not open.
    conn = sqlite3.connect(f"{db.DB_PATH.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = sample(conn, args.limit)
    print(f"  sampling {len(rows)} already-harvested conversations")
    if not rows:
        print("  nothing to check"); return 0

    verdicts: Counter[str] = Counter()
    detail: list[tuple] = []
    diverged: list[tuple] = []
    gain_ts = gain_resp = 0
    deadline = time.time() + args.budget_min * 60

    with run_lock():
        print("  run_lock acquired (read only — nothing is written)")
        with browser_page() as page:
            goto_chat(page)
            page.wait_for_timeout(2000)

            for i, r in enumerate(rows, 1):
                if time.time() > deadline:
                    print(f"  time budget reached after {i - 1}/{len(rows)}")
                    break
                sid = r["source_session_id"]
                captured: list[str] = []

                def on_response(resp, bucket=captured):
                    try:
                        # Agent-scoped conversations live at
                        # /chat/agent/{agent}/conversation/{guid}, so a literal
                        # "/chat/conversation/" test misses ~19% of the corpus.
                        if CONV_RE.search(resp.url):
                            bucket.append(resp.text())
                    except Exception:
                        pass

                page.on("response", on_response)
                try:
                    page.goto(deep_link(sid, r["agent_id"]), wait_until="domcontentloaded")
                    # Poll rather than sleep a fixed interval: the document
                    # response can lag domcontentloaded, and a short fixed wait
                    # reports "no payload" for conversations that were merely slow.
                    turns = []
                    for _ in range(16):
                        for html in captured:
                            parsed = parse_hydration(html)
                            if parsed["turns"]:
                                turns = parsed["turns"]
                                break
                        if turns:
                            break
                        page.wait_for_timeout(500)
                except SignInRequired:
                    raise
                except Exception as exc:                      # noqa: BLE001
                    verdicts["error"] += 1
                    detail.append((sid, r["stored_rows"], "-", f"error {type(exc).__name__}"))
                    continue
                finally:
                    try:
                        page.remove_listener("response", on_response)
                    except Exception:
                        pass

                stored = stored_turns(conn, sid)
                verdict = classify(stored, turns)
                verdicts[verdict] += 1
                detail.append((sid, len(stored), len(turns), verdict))
                if verdict == "divergent":
                    diverged.append((sid, r["agent_id"], first_divergence(stored, turns)))

                if verdict in ("aligned", "extra-trailing"):
                    gain_ts += r["missing_ts"] or 0
                    gain_resp += r["missing_resp"] or 0
                if i % 10 == 0:
                    print(f"    …{i}/{len(rows)}  {dict(verdicts)}")

    print(f"\n{BAR}\nALIGNMENT\n{BAR}")
    checked = sum(verdicts.values())
    for k, n in verdicts.most_common():
        pct = f"{100 * n // checked}%" if checked else "-"
        print(f"  {k:<20} {n:>5}  {pct:>5}")

    print(f"\n{BAR}\nMISMATCHES\n{BAR}")
    bad = [d for d in detail if d[3] not in ("aligned", "extra-trailing")]
    if not bad:
        print("  none")
    for sid, stored, found, verdict in bad[:25]:
        print(f"  {sid[:8]}  stored={stored:<4} payload={found:<4} {verdict}")

    trailing = [d for d in detail if d[3] == "extra-trailing"]
    if trailing:
        print(f"\n{BAR}\nEXTRA TRAILING TURNS (safe — every stored index matches)\n{BAR}")
        for sid, stored, found, _ in trailing[:15]:
            print(f"  {sid[:8]}  stored={stored:<4} payload={found:<4} "
                  f"(+{found - stored} previously missed)")

    if diverged:
        print(f"\n{BAR}\nDIVERGENCE DETAIL (words masked as ·length)\n{BAR}")
        for sid, agent, div in diverged[:10]:
            if not div:
                continue
            idx, stored_text, payload_text = div
            print(f"\n  {sid[:8]}  agent={'yes' if agent else 'no':<4} first differs at turn {idx}")
            print(f"    stored  len={len(stored_text or ''):<6} {mask(stored_text)}")
            if payload_text is None:
                print("    payload  <no turn at this index>")
            else:
                print(f"    payload len={len(payload_text):<6} {mask(payload_text)}")

    no_payload = [d for d in detail if d[3] == "no-payload"]
    if no_payload:
        agents = sum(1 for r in rows
                     if r["source_session_id"] in {d[0] for d in no_payload} and r["agent_id"])
        print(f"\n{BAR}\nNO-PAYLOAD BREAKDOWN\n{BAR}")
        print(f"  total: {len(no_payload)}   agent-scoped: {agents}")

    print(f"\n{BAR}\nWHAT A BACKFILL WOULD GAIN (safe conversations only)\n{BAR}")
    print(f"  rows gaining ts_source        : {gain_ts}")
    print(f"  rows gaining response_excerpt : {gain_resp}")

    safe = verdicts["aligned"] + verdicts["extra-trailing"]
    divergent = verdicts["divergent"]
    print(f"\n{BAR}\nVERDICT\n{BAR}")
    print(f"  safe to enrich   : {safe}/{checked}")
    print(f"  will conflict    : {divergent}  (skipped by store(), never overwritten)")
    print(f"  no payload       : {verdicts['no-payload']}  (falls back to the DOM path)")
    print()
    print("  store() only enriches a row when the prompt at that turn_index still")
    print("  matches, so a backfill cannot write the wrong timestamp onto the wrong")
    print("  prompt. Conflicts are reported and skipped, not merged. This check")
    print("  therefore sizes the GAIN; it is not a gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
