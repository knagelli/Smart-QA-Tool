"""
Unit tests for the 2026-09-27 job_registry.py council fixes (priority 5):
1. register() raises on exec_id collision against a non-terminal job,
   instead of silently overwriting it.
2. register() still allows re-registering an exec_id that belongs to an
   already-terminal (DONE/CANCELLED/ERROR) job that hasn't been cleanup()'d
   yet, matching the registry's existing "terminal jobs don't occupy
   capacity" convention.
3. cleanup()'s docstring correction and the per-process docstring note are
   documentation-only and are not separately unit-testable; they are
   covered by manual review, not exercised here.

No third-party stubbing needed - job_registry.py only imports threading,
time, dataclasses, typing (all stdlib).

Run with: python3 -m pytest app/test_job_registry_fixes.py -v
(or, with no pytest available: python3 app/test_job_registry_fixes.py)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.job_registry import JobRegistry  # noqa: E402


class TestExecIdCollisionGuard(unittest.TestCase):
    def test_register_succeeds_for_a_fresh_exec_id(self):
        reg = JobRegistry()
        job = reg.register("abc123", "CODE1", 10)
        self.assertEqual(job.exec_id, "abc123")
        self.assertEqual(job.state, "QUEUED")

    def test_register_raises_on_collision_with_queued_job(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        with self.assertRaises(ValueError):
            reg.register("abc123", "CODE2", 5)

    def test_register_raises_on_collision_with_running_job(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        reg.mark_running("abc123")
        with self.assertRaises(ValueError):
            reg.register("abc123", "CODE2", 5)

    def test_original_job_is_untouched_after_a_failed_collision_register(self):
        # The bug being fixed: silent overwrite. Confirm a rejected
        # collision leaves the original job fully intact.
        reg = JobRegistry()
        original = reg.register("abc123", "CODE1", 10)
        original.completed = 3
        try:
            reg.register("abc123", "CODE2", 5)
        except ValueError:
            pass
        still_there = reg.get("abc123")
        self.assertIs(still_there, original)
        self.assertEqual(still_there.access_code, "CODE1")
        self.assertEqual(still_there.completed, 3)

    def test_register_allowed_after_job_reaches_done(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        reg.mark_terminal("abc123", "DONE")
        # Should NOT raise - a terminal, not-yet-cleaned-up job is fine to
        # replace, matching snapshot()/queue_position()'s existing
        # "terminal jobs don't occupy capacity" convention.
        new_job = reg.register("abc123", "CODE2", 7)
        self.assertEqual(new_job.access_code, "CODE2")
        self.assertEqual(new_job.state, "QUEUED")

    def test_register_allowed_after_job_reaches_cancelled(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        reg.mark_terminal("abc123", "CANCELLED")
        new_job = reg.register("abc123", "CODE2", 7)
        self.assertEqual(new_job.state, "QUEUED")

    def test_register_allowed_after_job_reaches_error(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        reg.mark_terminal("abc123", "ERROR")
        new_job = reg.register("abc123", "CODE2", 7)
        self.assertEqual(new_job.state, "QUEUED")

    def test_register_allowed_after_explicit_cleanup(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        reg.mark_terminal("abc123", "DONE")
        reg.cleanup("abc123")
        new_job = reg.register("abc123", "CODE2", 7)
        self.assertEqual(new_job.access_code, "CODE2")

    def test_distinct_exec_ids_never_collide(self):
        reg = JobRegistry()
        reg.register("abc123", "CODE1", 10)
        job2 = reg.register("def456", "CODE2", 5)
        self.assertEqual(job2.exec_id, "def456")
        self.assertEqual(len(reg.snapshot()), 2)


class TestExistingBehaviorUnaffected(unittest.TestCase):
    """Guard against a regression: the collision guard must not change
    behavior for the normal, non-colliding path exercised by every other
    method."""

    def test_cancel_get_and_terminal_flow_still_work(self):
        reg = JobRegistry()
        reg.register("x1", "CODEX", 4)
        reg.mark_running("x1")
        self.assertEqual(reg.get("x1").state, "RUNNING")
        self.assertTrue(reg.cancel("x1"))
        self.assertTrue(reg.is_cancelled("x1"))
        reg.mark_terminal("x1", "CANCELLED")
        self.assertEqual(reg.get("x1").state, "CANCELLED")
        reg.cleanup("x1")
        self.assertIsNone(reg.get("x1"))

    def test_cancel_returns_false_for_unknown_exec_id(self):
        reg = JobRegistry()
        self.assertFalse(reg.cancel("does-not-exist"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
