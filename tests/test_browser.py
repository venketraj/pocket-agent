"""Drives the real local form in headless Edge (no model involved)."""
import pytest

import pocket_agent as pa


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr(pa, "HEADLESS", True)
    yield
    pa.close_browser()


def test_form_fill_and_submit(headless):
    page_info = pa.open_page("feedback_form.html")
    assert "name: Your name" in page_info and "'Pocket-Sized Brains'" in page_info
    after = pa.fill_and_submit({"name": "Venket", "session": "Pocket-Sized Brains", "rating": 5,
                                "try_next": "Gemma locally", "improve": "more demos"})
    assert "Thank you, Venket" in after
    assert pa._page().evaluate("window.__submitted") == {
        "name": "Venket", "session": "Pocket-Sized Brains", "rating": "5",
        "try_next": "Gemma locally", "improve": "more demos"}


def test_unknown_field_is_an_error(headless):
    pa.open_page("feedback_form.html")
    with pytest.raises(ValueError):
        pa.fill_and_submit({"email": "x@y.z"})
