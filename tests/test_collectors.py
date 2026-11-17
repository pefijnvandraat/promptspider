"""Offline tests for browser navigation selection."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import collectors  # noqa: E402


class FakeControl:
    def __init__(self, tag: str, *, visible: bool, enabled: bool = True):
        self.tag = tag
        self.text = "Cowork"
        self.visible = visible
        self.enabled = enabled
        self.clicked = False

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click(self, *, timeout):
        assert timeout == 5_000
        self.clicked = True


class FakeLocator:
    def __init__(self, controls):
        self.controls = controls
        self.text_pattern = None

    def filter(self, *, has_text):
        self.text_pattern = has_text
        self.controls = [
            control for control in self.controls
            if has_text.search(control.text)
        ]
        return self

    def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]


class FakePage:
    def __init__(self, elements):
        self.elements = elements
        self.selector = None
        self.waits = []

    def locator(self, selector):
        self.selector = selector
        actionable_tags = {"a", "button"}
        controls = [element for element in self.elements
                    if element.tag in actionable_tags]
        return FakeLocator(controls)

    def wait_for_timeout(self, timeout):
        self.waits.append(timeout)


@pytest.fixture(autouse=True)
def skip_browser_actions(monkeypatch):
    monkeypatch.setattr(collectors, "goto_chat", lambda page: None)
    monkeypatch.setattr(collectors, "assert_signed_in", lambda page: None)
    monkeypatch.setattr(collectors, "ensure_nav_expanded", lambda page: None)


def test_open_cowork_ignores_hidden_duplicate_text():
    hidden_duplicate = FakeControl("span", visible=False)
    visible_nav_item = FakeControl("button", visible=True)
    page = FakePage([hidden_duplicate, visible_nav_item])

    collectors.open_cowork(page)

    assert hidden_duplicate.clicked is False
    assert visible_nav_item.clicked is True
    assert "button" in page.selector
    assert page.waits == [9_000]


def test_open_cowork_fails_fast_without_visible_actionable_control():
    page = FakePage([FakeControl("span", visible=False)])

    with pytest.raises(collectors.CoworkNavigationError, match="found 0"):
        collectors.open_cowork(page)

    assert page.waits == []


def test_open_cowork_reports_bounded_click_timeout(monkeypatch):
    class TimedOutControl(FakeControl):
        def click(self, *, timeout):
            assert timeout == 5_000
            raise collectors.PlaywrightTimeoutError("timeout")

    page = FakePage([TimedOutControl("button", visible=True)])

    with pytest.raises(collectors.CoworkNavigationError, match="within 5 seconds"):
        collectors.open_cowork(page)
