"""In-memory tracker for long-running backup/restore jobs so the UI can show
a progress bar with step name, percentage and elapsed time. Jobs are transient
by design (a page reload always re-syncs from the persisted BackupRecord once
a job finishes), so no DB persistence is needed here.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

_lock = threading.Lock()
_jobs: dict[str, "Job"] = {}
# (timestamp, cumulative bytes) samples per job, trimmed to last ~10s.
_byte_samples: dict[str, deque] = {}
_upload_byte_samples: dict[str, deque] = {}
# Per-job log lines: (iso_timestamp, message) tuples, capped at 500 entries.
_job_logs: dict[str, deque] = {}

# Finished jobs older than this are dropped from list_jobs() and cleaned up.
FINISHED_JOB_TTL = 10 * 60  # 10 minutes


@dataclass
class Job:
    id: str
    kind: str  # "backup" | "restore"
    label: str
    total_steps: int = 1
    current_step: int = 0
    step_name: str = "starting"
    status: str = "running"  # running | success | failed | cancelling | cancelled
    error: Optional[str] = None
    result_backup_id: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    cancel_requested: bool = False
    bytes_done: int = 0
    upload_bytes_done: int = 0

    def to_dict(self):
        elapsed = (self.finished_at or time.time()) - self.started_at
        pct = int((self.current_step / self.total_steps) * 100) if self.total_steps else 0
        pct = max(0, min(100, pct))
        eta_seconds = None
        if self.status == "running" and self.current_step > 0:
            per_step = elapsed / self.current_step
            remaining_steps = max(self.total_steps - self.current_step, 0)
            eta_seconds = round(per_step * remaining_steps, 1)

        def _speed(samples_dict) -> Optional[int]:
            samples = samples_dict.get(self.id)
            if not samples or len(samples) < 2:
                return None
            t0, b0 = samples[0]
            t1, b1 = samples[-1]
            dt = t1 - t0
            return round((b1 - b0) / dt) if dt > 0.2 else None

        speed_bytes_per_sec = _speed(_byte_samples) if self.status == "running" else None
        upload_speed_bytes_per_sec = _speed(_upload_byte_samples) if self.status == "running" else None

        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "step_name": self.step_name,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "percent": pct,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": eta_seconds,
            "error": self.error,
            "result_backup_id": self.result_backup_id,
            "cancellable": self.status == "running" and not self.cancel_requested,
            "bytes_done": self.bytes_done,
            "speed_bytes_per_sec": speed_bytes_per_sec,
            "upload_speed_bytes_per_sec": upload_speed_bytes_per_sec,
        }


def create_job(kind: str, label: str, total_steps: int = 1) -> Job:
    job = Job(id=str(uuid.uuid4()), kind=kind, label=label, total_steps=max(total_steps, 1))
    with _lock:
        _jobs[job.id] = job
    return job


def update_progress(job_id: str, current_step: int, step_name: str, total_steps: Optional[int] = None):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.current_step = current_step
        job.step_name = step_name
        if total_steps is not None:
            job.total_steps = max(total_steps, 1)


def _record_sample(samples_dict: dict, job_id: str, cumulative: int) -> None:
    now = time.time()
    samples = samples_dict.setdefault(job_id, deque(maxlen=2000))
    samples.append((now, cumulative))
    cutoff = now - 10
    while len(samples) > 2 and samples[0][0] < cutoff:
        samples.popleft()


def update_bytes(job_id: str, delta: int):
    """Adds delta to the job's cumulative bytes read and records a timestamped
    sample for live read-speed (Leserate) computation."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.bytes_done += delta
        _record_sample(_byte_samples, job_id, job.bytes_done)


def add_log(job_id: str, message: str) -> None:
    """Appends a timestamped log line to the job's in-memory log buffer."""
    import datetime
    import pytz
    from app.config import TZ_NAME
    from app.clock import utcnow
    try:
        tz = pytz.timezone(TZ_NAME)
        ts = datetime.datetime.now(tz).strftime("%H:%M:%S")
    except Exception:
        ts = utcnow().strftime("%H:%M:%S")
    with _lock:
        if job_id not in _jobs:
            return
        buf = _job_logs.setdefault(job_id, deque(maxlen=500))
        buf.append((ts, message))


def get_logs(job_id: str) -> list[dict]:
    with _lock:
        buf = _job_logs.get(job_id)
        return [{"ts": ts, "msg": msg} for ts, msg in buf] if buf else []


def update_upload_bytes(job_id: str, delta: int):
    """Adds delta to the job's cumulative upload bytes and records a timestamped
    sample for live upload-speed (Übertragungsrate) computation."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.upload_bytes_done += delta
        _record_sample(_upload_byte_samples, job_id, job.upload_bytes_done)


def finish_job(job_id: str, ok: bool, error: Optional[str] = None, result_backup_id: Optional[int] = None):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.status = "success" if ok else "failed"
        job.error = error
        job.result_backup_id = result_backup_id
        job.finished_at = time.time()
        job.current_step = job.total_steps if ok else job.current_step


def cancel_job(job_id: str, message: str = "Vom Nutzer abgebrochen"):
    """Marks a job as cancelled once the running operation has actually
    stopped (called by the job runner after catching a BackupCancelled)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.status = "cancelled"
        job.error = message
        job.finished_at = time.time()


def request_cancel(job_id: str) -> bool:
    """Signals a running job to stop at its next checkpoint (cooperative -
    the job itself has to poll is_cancel_requested()). Returns False if the
    job doesn't exist or isn't running."""
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.status != "running":
            return False
        job.cancel_requested = True
        job.status = "cancelling"
        return True


def is_cancel_requested(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        return bool(job and job.cancel_requested)


def get_job(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


def list_jobs(active_only: bool = False) -> list[Job]:
    cutoff = time.time() - FINISHED_JOB_TTL
    with _lock:
        jobs = list(_jobs.values())
    terminal = {"success", "failed", "cancelled"}
    jobs = [j for j in jobs if j.status not in terminal or (j.finished_at or 0) >= cutoff]
    if active_only:
        jobs = [j for j in jobs if j.status == "running"]
    jobs.sort(key=lambda j: j.started_at, reverse=True)
    return jobs


def prune_old_jobs(max_finished: int = 50):
    cutoff = time.time() - FINISHED_JOB_TTL
    with _lock:
        finished = sorted(
            (j for j in _jobs.values() if j.status != "running"),
            key=lambda j: j.finished_at or 0, reverse=True,
        )
        for i, j in enumerate(finished):
            if (j.finished_at or 0) < cutoff or i >= max_finished:
                _jobs.pop(j.id, None)
                _byte_samples.pop(j.id, None)
                _upload_byte_samples.pop(j.id, None)
                _job_logs.pop(j.id, None)
