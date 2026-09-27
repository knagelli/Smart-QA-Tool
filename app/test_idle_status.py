"""
Unit tests for the 2026-09-27 auto-stop idle-tracking feature: the
_track_last_request middleware, GET /internal/idle-status, and
client_quotas.count_paying_clients() (app/main.py, app/client_quotas.py).

See claude/on-demand-ec2-auto-stop-implemented-2026-09-27.md for the full
design. Kalyan's policy under test: safe_to_stop is true only when
paying_client_count < 3 AND active_jobs == 0 AND the process has seen no
non-exempt HTTP request for at least REQ2QA_IDLE_QUIET_SECONDS.

Stubs playwright/anthropic/boto3 exactly like test_pii_masking.py and
test_priority_5_14_fixes.py (no PyPI network access in this sandbox), then
imports the REAL app.main module and exercises it through FastAPI's
TestClient (httpx is already a pinned, installed dependency here) - not a
reimplementation.

Run with: python3 -m pytest app/test_idle_status.py -v
(or, with no pytest available: python3 app/test_idle_status.py)
"""
import importlib
import json
import os
import sys
import tempfile
import types
import unittest


def _stub_missing_third_party_modules():
    if "playwright" not in sys.modules:
        playwright_pkg = types.ModuleType("playwright")
        sync_api = types.ModuleType("playwright.sync_api")

        class _FakeTimeoutError(Exception):
            pass

        sync_api.sync_playwright = lambda: (_ for _ in ()).throw(RuntimeError("stubbed"))
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

        anthropic_mod.RateLimitError = _FakeRateLimitError
        anthropic_mod.PermissionDeniedError = _FakePermissionDeniedError
        anthropic_mod.Anthropic = lambda *a, **k: None
        anthropic_mod.AnthropicBedrock = lambda *a, **k: None
        sys.modules["anthropic"] = anthropic_mod

    if "boto3" not in sys.modules:
        sys.modules["boto3"] = types.ModuleType("boto3")


_stub_missing_third_party_modules()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="req2qa_idle_status_test_")
os.environ["DATA_DIR"] = _TMP_DATA_DIR
os.environ["REQ2QA_INTERNAL_TOKEN"] = "test-token-123"
os.environ["REQ2QA_IDLE_QUIET_SECONDS"] = "0.05"  # near-instant for the test, not production

from app import main  # noqa: E402
from app import client_quotas  # noqa: E402
from app import job_registry  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)


def _set_paying_client_count(n: int) -> None:
    """Writes n synthetic client_quotas.json records directly - bypasses
    set_quota()'s validation since these tests only care about count, not
    field contents."""
    data = {f"CODE_{i}": {"client_name": f"Client {i}", "service_type": "both"} for i in range(n)}
    client_quotas._save(data)


class TestCountPayingClients(unittest.TestCase):
    def test_zero_when_store_is_empty(self):
        _set_paying_client_count(0)
        self.assertEqual(client_quotas.count_paying_clients(), 0)

    def test_counts_every_record_regardless_of_fields(self):
        _set_paying_client_count(5)
        self.assertEqual(client_quotas.count_paying_clients(), 5)


class TestIdleStatusAuth(unittest.TestCase):
    def test_missing_token_header_is_401(self):
        resp = client.get("/internal/idle-status")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_is_401(self):
        resp = client.get("/internal/idle-status", headers={"X-Internal-Token": "wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_correct_token_is_200(self):
        resp = client.get("/internal/idle-status", headers={"X-Internal-Token": "test-token-123"})
        self.assertEqual(resp.status_code, 200)

    def test_endpoint_disabled_entirely_if_token_env_var_unset(self):
        original = os.environ.pop("REQ2QA_INTERNAL_TOKEN", None)
        try:
            importlib.reload(main)
            reloaded_client = TestClient(main.app)
            resp = reloaded_client.get("/internal/idle-status", headers={"X-Internal-Token": "anything"})
            self.assertEqual(resp.status_code, 503)
        finally:
            if original is not None:
                os.environ["REQ2QA_INTERNAL_TOKEN"] = original
            importlib.reload(main)


class TestSafeToStopPolicy(unittest.TestCase):
    def setUp(self):
        self.headers = {"X-Internal-Token": "test-token-123"}
        # Every job_registry mutation this test file makes is cleaned up in
        # tearDown - REGISTRY is a module-level singleton shared across
        # tests/requests in this process.
        self._registered_exec_ids = []

    def tearDown(self):
        for exec_id in self._registered_exec_ids:
            job_registry.REGISTRY.cleanup(exec_id)

    def _register_job(self, exec_id, state=None):
        job_registry.REGISTRY.register(exec_id, "CODE_TEST", 1)
        if state:
            job_registry.REGISTRY.mark_terminal(exec_id, state) if state in ("DONE", "CANCELLED", "ERROR") \
                else job_registry.REGISTRY.mark_running(exec_id)
        self._registered_exec_ids.append(exec_id)

    def test_safe_to_stop_true_when_all_conditions_met(self):
        _set_paying_client_count(0)  # < 3
        # No active jobs registered in this test.
        import time
        time.sleep(0.1)  # exceed the 0.05s test threshold
        resp = client.get("/internal/idle-status", headers=self.headers)
        body = resp.json()
        self.assertTrue(body["safe_to_stop"])
        self.assertEqual(body["active_jobs"], 0)
        self.assertEqual(body["paying_client_count"], 0)

    def test_not_safe_when_3_or_more_paying_clients(self):
        _set_paying_client_count(3)
        import time
        time.sleep(0.1)
        resp = client.get("/internal/idle-status", headers=self.headers)
        self.assertFalse(resp.json()["safe_to_stop"])
        self.assertEqual(resp.json()["paying_client_count"], 3)

    def test_not_safe_with_2_paying_clients_is_still_stoppable(self):
        # Boundary check: policy is "< 3", so exactly 2 must still allow
        # stopping (given the other conditions hold) - confirms no off-by-
        # one against Kalyan's stated "minimum of three" wording.
        _set_paying_client_count(2)
        import time
        time.sleep(0.1)
        resp = client.get("/internal/idle-status", headers=self.headers)
        self.assertTrue(resp.json()["safe_to_stop"])

    def test_not_safe_when_a_job_is_running(self):
        _set_paying_client_count(0)
        self._register_job("idle-test-exec-running", state="RUNNING")
        import time
        time.sleep(0.1)
        resp = client.get("/internal/idle-status", headers=self.headers)
        body = resp.json()
        self.assertFalse(body["safe_to_stop"])
        self.assertEqual(body["active_jobs"], 1)

    def test_not_safe_when_a_job_is_only_queued(self):
        # Council-confirmed requirement: QUEUED must still count as active,
        # not just RUNNING.
        _set_paying_client_count(0)
        self._register_job("idle-test-exec-queued")  # stays QUEUED
        import time
        time.sleep(0.1)
        resp = client.get("/internal/idle-status", headers=self.headers)
        self.assertFalse(resp.json()["safe_to_stop"])

    def test_terminal_jobs_do_not_block_idle(self):
        _set_paying_client_count(0)
        self._register_job("idle-test-exec-done", state="DONE")
        import time
        time.sleep(0.1)
        resp = client.get("/internal/idle-status", headers=self.headers)
        self.assertTrue(resp.json()["safe_to_stop"])
        self.assertEqual(resp.json()["active_jobs"], 0)

    def test_not_safe_immediately_after_a_recent_request(self):
        _set_paying_client_count(0)
        # Hit a normal (non-exempt) endpoint right before checking - this
        # should reset the quiet timer.
        client.get("/")
        resp = client.get("/internal/idle-status", headers=self.headers)
        self.assertFalse(resp.json()["safe_to_stop"])
        self.assertLess(resp.json()["last_request_age_seconds"], 0.05)


class TestIdleStatusPathIsExempt(unittest.TestCase):
    """Polling /internal/idle-status itself must never look like real
    traffic, or the checker would keep the instance perpetually 'busy' just
    by polling it."""

    def test_polling_idle_status_does_not_reset_the_quiet_timer(self):
        _set_paying_client_count(0)
        import time
        time.sleep(0.1)
        headers = {"X-Internal-Token": "test-token-123"}
        first = client.get("/internal/idle-status", headers=headers).json()
        self.assertTrue(first["safe_to_stop"])
        # Poll it again immediately - if idle-status polling counted as
        # activity, this second call's own age would have just been reset
        # to ~0 by the first call, making safe_to_stop flip to False.
        second = client.get("/internal/idle-status", headers=headers).json()
        self.assertTrue(second["safe_to_stop"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
