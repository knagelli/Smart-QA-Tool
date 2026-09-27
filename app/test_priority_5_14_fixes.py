"""
Unit tests for the 2026-09-27 pending-deploy-checklist items:
- priority 6 (fail-fast tuning): MAX_AGENT_STEPS made env-configurable
- priority 14 (TC-006/TC-015): popup-window lookup detection/diagnostic

These import the REAL app/execute_engine.py module - not a reimplementation
- so a passing test actually exercises the shipped code. Three third-party
packages (`playwright`, `anthropic`, `boto3`) are not installable in this
offline sandbox (no PyPI network access), so they are stubbed with the
minimal surface execute_engine.py/ai_client.py touch at import time, before
the real module is imported. This mirrors the same stubbing approach used
earlier this session for the rapidfuzz-based wait_for_text fuzzy fallback,
when rapidfuzz itself could not be installed here either.

Run with: python3 -m pytest app/test_priority_5_14_fixes.py -v
(or, with no pytest available: python3 app/test_priority_5_14_fixes.py)
"""
import os
import sys
import types
import unittest


def _stub_missing_third_party_modules():
    if "playwright" not in sys.modules:
        playwright_pkg = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")

        class _FakeTimeoutError(Exception):
            pass

        def _fake_sync_playwright():
            raise RuntimeError("stubbed - not used by these tests")

        sync_api.sync_playwright = _fake_sync_playwright
        sync_api.TimeoutError = _FakeTimeoutError
        playwright_pkg.sync_api = sync_api
        sys.modules["playwright"] = playwright_pkg
        sys.modules["playwright.sync_api"] = sync_api

    if "anthropic" not in sys.modules:
        anthropic_mod = types.ModuleType("anthropic")

        class _FakeRateLimitError(Exception):
            pass

        class _FakePermissionDeniedError(Exception):
            pass

        class _FakeAnthropic:
            def __init__(self, *a, **k):
                pass

        class _FakeAnthropicBedrock:
            def __init__(self, *a, **k):
                pass

        anthropic_mod.RateLimitError = _FakeRateLimitError
        anthropic_mod.PermissionDeniedError = _FakePermissionDeniedError
        anthropic_mod.Anthropic = _FakeAnthropic
        anthropic_mod.AnthropicBedrock = _FakeAnthropicBedrock
        sys.modules["anthropic"] = anthropic_mod

    if "boto3" not in sys.modules:
        sys.modules["boto3"] = types.ModuleType("boto3")


_stub_missing_third_party_modules()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import execute_engine as ee  # noqa: E402


class TestMaxAgentStepsEnvConfigurable(unittest.TestCase):
    """Priority 6: MAX_AGENT_STEPS must be overridable via
    REQ2QA_MAX_AGENT_STEPS without changing the default for anyone who
    doesn't set it (the checklist's explicit "default unchanged" bar)."""

    def test_env_int_helper_parses_a_valid_override(self):
        os.environ["REQ2QA_TEST_ONLY_STEPS"] = "45"
        try:
            self.assertEqual(ee._env_int("REQ2QA_TEST_ONLY_STEPS", 60), 45)
        finally:
            del os.environ["REQ2QA_TEST_ONLY_STEPS"]

    def test_env_int_helper_falls_back_on_missing_var(self):
        os.environ.pop("REQ2QA_TEST_ONLY_STEPS_MISSING", None)
        self.assertEqual(ee._env_int("REQ2QA_TEST_ONLY_STEPS_MISSING", 60), 60)

    def test_env_int_helper_falls_back_on_garbage_value(self):
        os.environ["REQ2QA_TEST_ONLY_STEPS"] = "not-a-number"
        try:
            self.assertEqual(ee._env_int("REQ2QA_TEST_ONLY_STEPS", 60), 60)
        finally:
            del os.environ["REQ2QA_TEST_ONLY_STEPS"]

    def test_default_max_agent_steps_is_unchanged_when_env_unset(self):
        # Guards the explicit "default unchanged" requirement: importing the
        # module with no REQ2QA_MAX_AGENT_STEPS set must still yield 60.
        os.environ.pop("REQ2QA_MAX_AGENT_STEPS", None)
        self.assertEqual(ee.MAX_AGENT_STEPS, 60)


class TestPopupNoticeRecording(unittest.TestCase):
    """Priority 14 (TC-006/TC-015): a popup/lookup window's url/title is
    recorded and the popup is closed, without ever letting an internal
    error escape and interrupt the run in progress."""

    def test_records_url_and_title_and_closes_the_popup(self):
        notices = []
        closed = []

        class FakePopupPage:
            url = "https://example.service-now.com/lookup.do"
            def title(self):
                return "Choose a value"
            def wait_for_load_state(self, *a, **k):
                pass
            def close(self):
                closed.append(True)

        ee._record_popup_notice(FakePopupPage(), notices)

        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["url"], "https://example.service-now.com/lookup.do")
        self.assertEqual(notices[0]["title"], "Choose a value")
        self.assertEqual(closed, [True])

    def test_fails_open_with_unknown_values_if_title_raises(self):
        notices = []

        class FlakyPopupPage:
            url = "https://example.com/popup"
            def title(self):
                raise RuntimeError("page already navigated away")
            def wait_for_load_state(self, *a, **k):
                pass
            def close(self):
                pass

        # Must not raise - a bug in this diagnostic must never take down
        # test execution.
        ee._record_popup_notice(FlakyPopupPage(), notices)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0], {"url": "(unknown)", "title": "(unknown)"})

    def test_fails_open_and_appends_nothing_worse_if_close_itself_raises(self):
        notices = []

        class PopupThatWontClose:
            url = "https://example.com/popup"
            def title(self):
                return "A lookup"
            def wait_for_load_state(self, *a, **k):
                pass
            def close(self):
                raise RuntimeError("already closed")

        # Must not raise even though close() fails.
        ee._record_popup_notice(PopupThatWontClose(), notices)
        self.assertEqual(notices, [{"url": "https://example.com/popup", "title": "A lookup"}])

    def test_multiple_popups_keep_every_notice_in_order(self):
        notices = []

        def make(url, title):
            class P:
                def title(self_inner):
                    return title
                def wait_for_load_state(self_inner, *a, **k):
                    pass
                def close(self_inner):
                    pass
            p = P()
            p.url = url
            return p

        ee._record_popup_notice(make("https://a", "First"), notices)
        ee._record_popup_notice(make("https://b", "Second"), notices)
        self.assertEqual([n["title"] for n in notices], ["First", "Second"])
        # take_snapshot's payload uses popup_notices[-1] - the most recent
        # one - confirm that's the second, not the first.
        self.assertEqual(notices[-1]["title"], "Second")

    def test_waits_for_url_to_leave_about_blank_before_recording(self):
        # Council-required fix: a plain domcontentloaded wait can be
        # satisfied by about:blank itself. wait_for_url must be given a
        # chance to let the real destination settle before .url is read.
        notices = []
        state = {"url": "about:blank"}

        class SettlingPopupPage:
            def title(self):
                return "Choose a value"

            def wait_for_load_state(self, *a, **k):
                pass

            def wait_for_url(self, predicate, timeout=0):
                # Simulate the popup navigating to its real destination by
                # the time wait_for_url is asked to check it.
                state["url"] = "https://example.service-now.com/lookup.do"

            def close(self):
                pass

            @property
            def url(self):
                return state["url"]

        ee._record_popup_notice(SettlingPopupPage(), notices)
        self.assertEqual(notices[0]["url"], "https://example.service-now.com/lookup.do")

    def test_records_whatever_is_true_if_still_about_blank_after_timeout(self):
        # Must degrade honestly (record the real, if uninformative, current
        # value) rather than raising when the popup never leaves about:blank
        # in time.
        notices = []

        class NeverSettlesPopupPage:
            url = "about:blank"

            def title(self):
                return ""

            def wait_for_load_state(self, *a, **k):
                pass

            def wait_for_url(self, predicate, timeout=0):
                raise TimeoutError("still about:blank")

            def close(self):
                pass

        ee._record_popup_notice(NeverSettlesPopupPage(), notices)
        self.assertEqual(notices, [{"url": "about:blank", "title": ""}])


class TestPopupNoticeIsSingleShot(unittest.TestCase):
    """Priority 14, council-required fix: the correctness reviewer found a
    real false-BLOCKED bug in the first version - popup_notices was never
    cleared, so a popup resolved early in the run kept echoing forward onto
    every later, unrelated snapshot for the rest of the test case. The
    shipped fix is "surface once via popup_notices[-1], then .clear()" at
    the exact point take_snapshot() builds its payload. This test exercises
    that exact consume-then-clear pattern standalone (the real logic lives
    inline in take_snapshot's closure, which needs a live Playwright page to
    exercise end-to-end) to confirm the pattern itself is correct: it must
    surface on the very next read after a popup fires, and be silent
    (absent from the payload) on every read after that until another popup
    fires."""

    @staticmethod
    def _consume_popup_notice(popup_notices: list) -> dict:
        """The exact pattern shipped in take_snapshot(): surface the most
        recent notice if any, then clear the list so it cannot echo
        forward onto a later, unrelated snapshot."""
        payload = {}
        if popup_notices:
            payload["unsupported_popup_opened"] = popup_notices[-1]
            popup_notices.clear()
        return payload

    def test_absent_when_no_popup_has_fired(self):
        notices = []
        self.assertNotIn("unsupported_popup_opened", self._consume_popup_notice(notices))

    def test_present_on_the_read_immediately_after_a_popup_fires(self):
        notices = []
        notices.append({"url": "https://x", "title": "Lookup"})
        payload = self._consume_popup_notice(notices)
        self.assertEqual(payload["unsupported_popup_opened"], {"url": "https://x", "title": "Lookup"})

    def test_absent_on_every_subsequent_read_after_being_surfaced_once(self):
        notices = []
        notices.append({"url": "https://x", "title": "Lookup"})
        first_read = self._consume_popup_notice(notices)
        second_read = self._consume_popup_notice(notices)
        third_read = self._consume_popup_notice(notices)
        self.assertIn("unsupported_popup_opened", first_read)
        self.assertNotIn("unsupported_popup_opened", second_read)
        self.assertNotIn("unsupported_popup_opened", third_read)

    def test_a_second_later_popup_is_surfaced_again_after_the_first_cleared(self):
        notices = []
        notices.append({"url": "https://x", "title": "First lookup"})
        self._consume_popup_notice(notices)  # surfaced + cleared
        notices.append({"url": "https://y", "title": "Second lookup"})
        second = self._consume_popup_notice(notices)
        self.assertEqual(second["unsupported_popup_opened"]["title"], "Second lookup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
