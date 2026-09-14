"""
Req2QA - Live Execution Progress Tracking (Phase B)

A small per-execution JSON file, written incrementally while a batch of
test cases runs in the background, so a status page can poll it and show
"N of M done" plus which test case is currently running - instead of the
client staring at a blank, possibly request-timed-out, browser tab.

Deliberately file-based, matching the rest of this app's persistence
pattern (runs/, executions/, history) - no new datastore, no new
dependency. Written with a write-to-temp-then-rename so a concurrent poll
never sees a half-written file.
"""
import json
import os
from pathlib import Path
from typing import Optional

STATUS_FILENAME = "status.json"


def _path(exec_dir: Path) -> Path:
    return exec_dir / STATUS_FILENAME


def write_status(exec_dir: Path, data: dict) -> None:
    path = _path(exec_dir)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data))
    os.replace(tmp_path, path)


def read_status(exec_dir: Path) -> Optional[dict]:
    path = _path(exec_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
