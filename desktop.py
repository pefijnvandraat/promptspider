"""Windows launcher for on-demand imports and the local search page."""
from __future__ import annotations

import ctypes
import os
import secrets
import socket
import sys
import tempfile
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from tkinter import messagebox, ttk

import uvicorn
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

MUTEX_NAME = "Local\\PromptSpiderDesktop-8540AA4A"


def configure_data() -> None:
    home = os.environ.get("PROMPT_SPIDER_HOME") or str(
        Path(os.environ["LOCALAPPDATA"]) / "PromptSpider"
    )
    os.environ["PROMPT_SPIDER_HOME"] = home
    if not os.environ.get("PROMPT_SPIDER_PROFILE"):
        os.environ["PROMPT_SPIDER_PROFILE"] = str(Path(home) / "edge-profile")


class PrivateSearch:
    def __init__(self, app, address: str):
        self.app = app
        self.address = address
        self.key = secrets.token_urlsafe(32)
        self.cookie = "prompt_spider_" + secrets.token_hex(8)
        self.launch_url = f"http://{address}/_open/{self.key}"

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        if request.headers.get("host") != self.address:
            response = PlainTextResponse("Invalid search address.", status_code=400)
        elif request.url.path == f"/_open/{self.key}":
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(self.cookie, self.key, httponly=True, samesite="strict")
        elif request.cookies.get(self.cookie) == self.key:
            async def no_cache(message):
                if message["type"] == "http.response.start":
                    message["headers"].extend([
                        (b"cache-control", b"no-store"),
                        (b"referrer-policy", b"no-referrer"),
                    ])
                await send(message)

            return await self.app(scope, receive, no_cache)
        else:
            response = PlainTextResponse("Open Search from Prompt Spider.", status_code=403)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        await response(scope, receive, send)


class SearchWindow:
    def __init__(self):
        from db import init_db
        from server import app

        init_db()
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", 0))
            self.search = PrivateSearch(app, f"127.0.0.1:{listener.getsockname()[1]}")
            self.server = uvicorn.Server(uvicorn.Config(
                self.search, http="h11", loop="asyncio", ws="none", lifespan="off",
                proxy_headers=False, access_log=False, log_level="warning",
                log_config=None,
            ))
            self.thread = Thread(target=self.server.run, kwargs={"sockets": [listener]},
                                 daemon=True)
            self.thread.start()
            deadline = time.monotonic() + 5
            while not self.server.started:
                if not self.thread.is_alive() or time.monotonic() >= deadline:
                    self.close()
                    raise RuntimeError("Search could not start.")
                time.sleep(0.02)
        except BaseException:
            listener.close()
            raise

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=3)


def sign_in() -> str:
    from browser import CHAT_URL, browser_page, run_lock

    with run_lock(), browser_page() as page:
        page.goto(CHAT_URL, wait_until="commit")
        page.context.wait_for_event("close", timeout=0)
    return "If you reached Copilot in Edge, select Import Copilot chats."


def import_history(local: bool) -> str:
    from harvest import harvest, harvest_local

    result = (harvest_local(verbose=False) if local else
              harvest("incremental", budget_s=10 * 60, limit=50, cowork=False, verbose=False))
    if result["status"] == "signin_required":
        return "Sign in to Microsoft 365 Copilot in Edge, then try the import again."
    if result["status"] == "lock_held":
        return "The import profile is in use. Close its Edge window and try again later."
    if result["status"] != "ok":
        return "The import did not finish. Open Search > Corpus for details."
    count = result["new"]
    if not count:
        return "Import finished. No new prompts were found."
    return f"Import finished. {count:,} new prompt{'s' if count != 1 else ''} added."


def run_window(root: tk.Tk) -> None:
    root.title("Prompt Spider")
    root.resizable(False, False)
    panel = ttk.Frame(root, padding=24)
    panel.grid(sticky="nsew")
    ttk.Label(panel, text="Prompt Spider", font=("Segoe UI", 18, "bold")).grid(sticky="w")
    ttk.Label(panel, text="Import and search your AI prompts on this laptop.",
              wraplength=410).grid(sticky="w", pady=(4, 16))
    status = tk.StringVar(value="For Microsoft 365 Copilot, sign in before your first import.")
    results = SimpleQueue()
    buttons = []
    busy = False
    search = None

    def run(action, progress, failure):
        nonlocal busy
        busy = True
        status.set(progress)
        for button in buttons:
            button.state(["disabled"])

        def worker():
            try:
                results.put(action())
            except Exception:
                results.put(failure)

        def update():
            nonlocal busy
            try:
                status.set(results.get_nowait())
            except Empty:
                root.after(100, update)
                return
            busy = False
            for button in buttons:
                button.state(["!disabled"])

        Thread(target=worker, daemon=True).start()
        root.after(100, update)

    def open_search():
        nonlocal search
        try:
            if search is None:
                search = SearchWindow()
            if not webbrowser.open(search.search.launch_url):
                raise RuntimeError("No default browser opened.")
        except (OSError, RuntimeError):
            status.set("Search could not open. Check that you have a default browser and try again.")

    def close():
        if busy:
            messagebox.showinfo("Prompt Spider", "Finish signing in or wait for the import to finish.",
                                parent=root)
        else:
            root.destroy()

    actions = [
        ("1. Sign in to Microsoft 365 Copilot", lambda: run(
            sign_in, "Sign in using the Edge window, then close that window.",
            "Could not finish sign-in. Check that Microsoft Edge is installed and try again.")),
        ("2. Import Copilot chats", lambda: run(
            lambda: import_history(False), "Importing recent Copilot chats. Leave Edge open.",
            "Could not import Copilot chats. Sign in again and retry.")),
        ("3. Open Search", open_search),
        ("Import history from this laptop", lambda: run(
            lambda: import_history(True), "Checking for supported AI history on this laptop.",
            "Could not import local history. Check that Prompt Spider can access its data folder.")),
    ]
    for label, action in actions:
        button = ttk.Button(panel, text=label, command=action)
        button.grid(sticky="ew", pady=3)
        buttons.append(button)
    buttons[0].focus_set()
    ttk.Label(panel, textvariable=status, wraplength=410, width=58).grid(sticky="w", pady=(14, 0))
    root.protocol("WM_DELETE_WINDOW", close)
    try:
        root.mainloop()
    finally:
        if search is not None:
            search.close()


def main() -> None:
    configure_data()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    mutex = kernel.CreateMutexW(None, False, MUTEX_NAME)
    already_open = ctypes.get_last_error() == 183
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if already_open:
            messagebox.showinfo("Prompt Spider", "Prompt Spider is already open.")
        else:
            run_window(tk.Tk())
    finally:
        kernel.CloseHandle(mutex)


def check_bundle() -> None:
    """Exercise the packaged GUI, browser driver and search without signing in."""
    import gc
    from http.cookiejar import CookieJar
    from urllib.error import HTTPError
    from urllib.request import HTTPCookieProcessor, Request as OpenRequest, build_opener

    with tempfile.TemporaryDirectory() as temp:
        os.environ["PROMPT_SPIDER_HOME"] = temp
        os.environ["PROMPT_SPIDER_PROFILE"] = str(Path(temp) / "edge-profile")
        from playwright.sync_api import sync_playwright
        import harvest  # Ensure the packaged importer loads too.

        assert callable(harvest.harvest)
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        with sync_playwright():
            pass
        search = SearchWindow()
        try:
            health = f"http://{search.search.address}/api/health"
            for request, status in (
                (health, 403),
                (OpenRequest(health, headers={"Host": "unexpected.invalid"}), 400),
            ):
                try:
                    build_opener().open(request, timeout=5).close()
                except HTTPError as exc:
                    assert exc.code == status
                else:
                    raise AssertionError("Search accepted an unauthorized request.")
            opener = build_opener(HTTPCookieProcessor(CookieJar()))
            with opener.open(search.search.launch_url, timeout=5) as response:
                assert b"Prompt Spider" in response.read()
            with opener.open(health, timeout=5) as response:
                assert b'"ok":true' in response.read()
        finally:
            search.close()
            gc.collect()


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        try:
            check_bundle()
        except Exception:
            if sys.stderr is not None:
                raise
            sys.exit(1)
    else:
        main()
