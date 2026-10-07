from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from scraper import InstagramScraper, ScraperConfig, ScraperError
from scraper.exporter import export_json

logger = logging.getLogger(__name__)

Runner = Callable[[str], dict[str, Any]]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    username: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    filename: str | None = None
    result: dict[str, Any] | None = None

    @property
    def finished(self) -> bool:
        return self.status in (JobStatus.DONE, JobStatus.FAILED)

    def to_dict(self, include_result: bool = False) -> dict[str, Any]:
        data = {
            "id": self.id,
            "username": self.username,
            "status": self.status.value,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "error": self.error,
            "filename": self.filename,
        }
        if include_result and self.result is not None:
            data["result"] = self.result
        return data


def default_runner(config: ScraperConfig) -> Runner:
    def run(username: str) -> dict[str, Any]:
        return asyncio.run(InstagramScraper(config).scrape(username)).to_dict()

    return run


class JobManager:
    def __init__(
        self,
        runner: Runner,
        output_dir: Path,
        max_workers: int = 2,
        retention_seconds: float = 3600,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._runner = runner
        self._retention = retention_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scrape")

    def submit(self, username: str) -> Job:
        with self._lock:
            self._prune()
            for job in self._jobs.values():
                if not job.finished and job.username.lower() == username.lower():
                    return replace(job)
            job = Job(id=uuid.uuid4().hex, username=username)
            self._jobs[job.id] = job
            snapshot = replace(job)
        self._executor.submit(self._run, job.id)
        logger.info("Queued job %s for @%s", job.id, username)
        return snapshot

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return replace(job) if job else None

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _run(self, job_id: str) -> None:
        job = self._update(job_id, status=JobStatus.RUNNING)
        try:
            result = self._runner(job.username)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{job.username}_{stamp}.json"
            export_json(result, self.output_dir / filename)
        except ScraperError as exc:
            logger.warning("Job %s for @%s failed: %s", job_id, job.username, exc)
            self._update(job_id, status=JobStatus.FAILED, error=str(exc))
        except Exception:
            logger.exception("Job %s for @%s crashed", job_id, job.username)
            self._update(
                job_id,
                status=JobStatus.FAILED,
                error="Unexpected error while scraping. Check the server logs.",
            )
        else:
            self._update(job_id, status=JobStatus.DONE, filename=filename, result=result)

    def _update(self, job_id: str, **changes: Any) -> Job:
        with self._lock:
            job = self._jobs[job_id]
            for name, value in changes.items():
                setattr(job, name, value)
            if job.finished and job.finished_at is None:
                job.finished_at = time.time()
            return replace(job)

    def _prune(self) -> None:
        cutoff = time.time() - self._retention
        for job_id in [
            job.id
            for job in self._jobs.values()
            if job.finished and job.finished_at is not None and job.finished_at < cutoff
        ]:
            del self._jobs[job_id]
