"""持久化：JSON 原子写，重启后仍能按 (pid, start_time) 复核历代实例身份。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from .events import EventLog
from .models import Job, Round


class Store:
    """内存状态 + 单个 JSON 文件的原子持久化。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.rounds: dict[str, Round] = {}          # round_id -> Round
        self.job_rounds: dict[str, list[str]] = {}  # job_id -> [round_id]
        self.events = EventLog()
        self._loaded = False

    # ------------------------------------------------------------ 装载/保存

    def load(self) -> None:
        with self.lock:
            if not os.path.exists(self.path):
                self._loaded = True
                return
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.jobs = {j["job_id"]: Job.from_dict(j) for j in raw.get("jobs", [])}
            self.rounds = {r["round_id"]: Round.from_dict(r) for r in raw.get("rounds", [])}
            self.job_rounds = {k: list(v) for k, v in raw.get("job_rounds", {}).items()}
            self.events.load(raw.get("events", {}))
            self._loaded = True

    def save(self) -> None:
        """原子写：先写临时文件再 rename，避免半写状态。"""
        with self.lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            raw = {
                "version": 1,
                "jobs": [j.to_dict() for j in self.jobs.values()],
                "rounds": [r.to_dict() for r in self.rounds.values()],
                "job_rounds": self.job_rounds,
                "events": self.events.snapshot(),
            }
            fd, tmp = tempfile.mkstemp(
                prefix=".opshost-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(self.path))
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(raw, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ------------------------------------------------------------ 作业

    def add_job(self, job: Job) -> None:
        with self.lock:
            self.jobs[job.job_id] = job
            self.job_rounds.setdefault(job.job_id, [])
            self.save()

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    # ------------------------------------------------------------ 轮次

    def add_round(self, rnd: Round) -> None:
        with self.lock:
            self.rounds[rnd.round_id] = rnd
            self.job_rounds.setdefault(rnd.job_id, []).append(rnd.round_id)
            self.save()

    def get_round(self, round_id: str) -> Optional[Round]:
        return self.rounds.get(round_id)

    def rounds_of_job(self, job_id: str) -> list[Round]:
        ids = self.job_rounds.get(job_id, [])
        return [self.rounds[i] for i in ids if i in self.rounds]

    def mark_round_ended(self, round_id: str, status: str, summary: Optional[dict] = None) -> None:
        with self.lock:
            rnd = self.rounds[round_id]
            rnd.status = status
            rnd.ended_at = rnd.ended_at or _now()
            if summary is not None:
                rnd.stop_summary = summary
            self.save()


def _now() -> float:
    import time

    return time.time()
