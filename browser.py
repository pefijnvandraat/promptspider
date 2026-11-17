"""Prompt Spider — browser session + run lock.

The SSO Edge profile is a singleton shared with other Scout skills (ESXP, PjMXP,
Virtuoso, CompassOne). We take an exclusive lock and fail fast if it is held,
rather than fighting over the profile.

Headless is deliberately NOT used: MSAL/MFA flows on the default SSO profile
require visible UI.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_DIR = os.environ.get("PROMPT_SPIDER_PROFILE") or str(Path.home() / ".prompt-spider" / "edge-profile")
LOCK_PATH = Path(os.environ.get("PROMPT_SPIDER_HOME") or (Path.home() / ".prompt-spider")) / "harvest.lock"
LOCK_STALE_SECONDS = 45 * 60

CHAT_URL = "https://m365.cloud.microsoft/chat"


class LockHeld(Exception):
    """Another harvest run (or skill) holds the profile."""


class SignInRequired(Exception):
    """Session expired — a human must complete sign-in."""


class NavNotReady(Exception):
    """The chat nav never rendered; enumeration would silently return nothing."""


@contextmanager
def run_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < LOCK_STALE_SECONDS:
            raise LockHeld(f"harvest.lock held ({int(age)}s old)")
        LOCK_PATH.unlink(missing_ok=True)  # stale: previous run died
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        LOCK_PATH.unlink(missing_ok=True)


@contextmanager
def browser_page(headless: bool = False):
    """Persistent-context Edge using the shared SSO profile.

    Always closed via finally so a crash never leaves the profile locked.
    """
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel="msedge",
            headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(45_000)
            yield page
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def assert_signed_in(page, settle_ms: int = 12_000) -> None:
    """Fail only on a persistent sign-in redirect.

    M365 routinely bounces through login.microsoftonline.com mid-navigation and
    lands back on the app a moment later, so a single URL check produces false
    failures. We only raise if we are still on a sign-in page after settling.
    """
    def on_login() -> bool:
        u = (page.url or "").lower()
        return "login.microsoftonline.com" in u or "/common/oauth2" in u

    if not on_login():
        return
    deadline = time.time() + settle_ms / 1000
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        if not on_login():
            return
    raise SignInRequired("Persistent sign-in redirect — interactive auth needed")


def goto_chat(page, attempts: int = 3) -> None:
    """Land on the chat surface with the nav drawer present and expanded.

    Hydration timing varies a lot, and a collapsed nav renders no conversation
    links at all — which looks identical to "no data". So we wait for the drawer
    to actually exist and retry rather than trusting a fixed sleep.
    """
    for attempt in range(attempts):
        if not page.url.startswith(CHAT_URL):
            page.goto(CHAT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        assert_signed_in(page)
        ensure_nav_expanded(page)
        if wait_for_nav(page):
            return
        page.goto(CHAT_URL, wait_until="domcontentloaded")
    raise NavNotReady("Nav drawer never rendered — page did not hydrate")


def wait_for_nav(page, timeout_ms: int = 25_000) -> bool:
    """True once the nav drawer exists and has non-zero height."""
    try:
        page.wait_for_function(
            """() => { const e = document.querySelector('.fui-NavDrawerBody');
                       return !!e && e.clientHeight > 100; }""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def ensure_nav_expanded(page) -> bool:
    """Expand the left nav if collapsed. Returns True if a toggle was clicked."""
    clicked = page.evaluate(
        """() => {
            const drawer = document.querySelector('.fui-NavDrawerBody');
            if (drawer && drawer.clientHeight > 100) return false;
            const btn = document.querySelector('[data-testid="nav-toggle-button"]')
                || [...document.querySelectorAll('button')].find(
                    e => /expand|collapse/i.test(e.getAttribute('aria-label') || ''));
            if (btn) { btn.click(); return true; }
            return false;
        }"""
    )
    if clicked:
        page.wait_for_timeout(5000)
    return bool(clicked)
