"""
Req2QA - Run Control Center: one job registry for live-execution batches
(2026-09-25). See claude/council-review-run-control-center-2026-09-25.md for
the full design and claude/council-warroom-aws-independent-workstream-2026-
09-25.md for why this was prioritized independently of the AWS RPM outcome.

Before this, execution state was scattered across three ad-hoc places:
_active_executions_by_code (main.py, an in-memory dict just for "is a job
already running for this code"), exec_status.py's per-execution JSON files
(the client-facing progress source of truth), and a hardcoded timing
sentence. Cancel, queue-depth visibility, and honest timing all kept coming
out as separate patches because they each had to reach into a different one
of those. This module is the single abstraction they all now come from.

SCOPE NOTE (deliberately smaller than the original design doc's full
proposal): the original design called for cancellation state to be written
through to disk so a process restart could "rehydrate" a job's cancelled
status. On review, that specific piece was dropped: a restart already kills
the background asyncio task running the batch (Playwright + the whole
worker thread go with it, exactly like today's existing crash path), so
there's nothing left running that a rehydrated cancel flag would need to
stop. What DOES still need to survive a request/response cycle - the
client-facing status page reading whether a job is cancelled - already goes
through exec_status.py's on-disk file, which _run_execution_batch_impl
updates once a cancellation is observed. So this registry is deliberately
in-memory-only, matching _active_executions_by_code's existing pattern (a
restart clears it; that's a no-op since nothing survives the restart to
resume anyway) - see the informational-only change log for the full note.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# Terminal states (a job in one of these no longer occupies capacity and is
# excluded from queue-depth/position calculations).
_TERMINAL = {"DONE", "CANCELLED", "ERROR"}


@dataclass
class ExecutionJob:
    exec_id: str
    access_code: str
    total: int
    registered_at: float = field(default_factory=time.time)
    state: str = "QUEUED"          # QUEUED -> RUNNING -> DONE/CANCELLED/ERROR
    completed: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Real observed step-completion timestamps, most recent last. This is
    # what makes the live ETA honest instead of a hardcoded sentence - see
    # observed_seconds_per_step below.
    step_timestamps: list = field(default_factory=list)

    def record_step(self) -> None:
        self.step_timestamps.append(time.time())
        # Bounded so a very long-running batch can't grow this unboundedly;
        # only recent steps matter for a "how fast is this actually going"
        # estimate anyway.
        if len(self.step_timestamps) > 200:
            self.step_timestamps = self.step_timestamps[-200:]

    def observed_seconds_per_step(self) -> Optional[float]:
        """None means "no data yet" (cold start, e.g. before the first step
        of the first test case completes) - callers must handle this
        explicitly rather than dividing by zero or showing a bogus 0s
        estimate. Uses the median of recent per-step deltas rather than the
        mean, so one unusually slow step (a page that took longer to settle)
        doesn't skew the estimate as much as it would with an average."""
        ts = self.step_timestamps
        if len(ts) < 2:
            return None
        deltas = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
        if not deltas:
            return None
        mid = len(deltas) // 2
        if len(deltas) % 2:
            return deltas[mid]
        return (deltas[mid - 1] + deltas[mid]) / 2.0


class JobRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, ExecutionJob] = {}

    def register(self, exec_id: str, access_code: str, total: int) -> ExecutionJob:
        with self._lock:
            job = ExecutionJob(exec_id=exec_id, access_code=access_code, total=total)
            self._jobs[exec_id] = job
            return job

    def get(self, exec_id: str) -> Optional[ExecutionJob]:
        with self._lock:
            return self._jobs.get(exec_id)

    def mark_running(self, exec_id: str) -> None:
        with self._lock:
            job = self._jobs.get(exec_id)
            if job and job.state == "QUEUED":
                job.state = "RUNNING"

    def mark_terminal(self, exec_id: str, state: str) -> None:
        assert state in _TERMINAL, state
        with self._lock:
            job = self._jobs.get(exec_id)
            if job:
                job.state = state

    def set_completed(self, exec_id: str, completed: int) -> None:
        with self._lock:
            job = self._jobs.get(exec_id)
            if job:
                job.completed = completed

    def cancel(self, exec_id: str) -> bool:
        """Returns True if a job was found and its cancel flag set (whether
        or not it was already finishing - see the race note below), False if
        no such job is registered at all (e.g. already cleaned up, or a
        bogus exec_id). Cancelling a run that finishes naturally in the same
        instant is a harmless no-op by construction: the Event being set
        after the run already completed changes nothing, since the step
        loop only checks it before starting further work."""
        with self._lock:
            job = self._jobs.get(exec_id)
            if job is None:
                return False
            job.cancel_event.set()
            return True

    def is_cancelled(self, exec_id: str) -> bool:
        job = self.get(exec_id)
        return bool(job and job.cancel_event.is_set())

    def get_event(self, exec_id: str) -> Optional[threading.Event]:
        job = self.get(exec_id)
        return job.cancel_event if job else None

    def record_step(self, exec_id: str) -> None:
        job = self.get(exec_id)
        if job:
            job.record_step()

    def eta_seconds(self, exec_id: str, remaining_steps_estimate: int) -> Optional[float]:
        job = self.get(exec_id)
        if job is None or remaining_steps_estimate <= 0:
            return 0.0 if job else None
        per_step = job.observed_seconds_per_step()
        if per_step is None:
            return None  # cold start - caller must show "estimating..." not "0s"
        return per_step * remaining_steps_estimate

    def cleanup(self, exec_id: str) -> None:
        """Drop a finished job's bookkeeping once its result page is served -
        called from the same place _active_executions_by_code is cleared, so
        this never grows unboundedly across a long-running process."""
        with self._lock:
            self._jobs.pop(exec_id, None)

    def snapshot(self) -> list[dict]:
        """Every non-terminal job right now, oldest first. Deliberately
        returns ONLY aggregate/queue-relevant fields - access_code, total,
        completed, state, registered_at - never application name, module, or
        test-case titles, so this can safely power a cross-client view
        (queue-position messaging, the admin "what's running now" page)
        without leaking one client's run details to another. See the
        Run Control Center design doc's synthetic-test checklist for why
        this boundary matters."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.state not in _TERMINAL]
        jobs.sort(key=lambda j: j.registered_at)
        return [
            {
                "exec_id": j.exec_id,
                "access_code": j.access_code,
                "total": j.total,
                "completed": j.completed,
                "state": j.state,
                "registered_at": j.registered_at,
            }
            for j in jobs
        ]

    def queue_position(self, exec_id: str) -> Optional[dict]:
        """For the client-facing 'N other runs ahead of yours' message. Ahead
        = registered earlier and still not done, EXCLUDING other jobs from
        the same access_code (a client's own second run shouldn't be counted
        as competition against themselves in this message - the existing
        _active_executions_by_code warning already handles that case
        separately, before a second batch is even started)."""
        job = self.get(exec_id)
        if job is None:
            return None
        snap = self.snapshot()
        ahead = [j for j in snap if j["registered_at"] < job.registered_at and j["access_code"] != job.access_code]
        return {"ahead_count": len(ahead), "total_active": len(snap)}


REGISTRY = JobRegistry()
