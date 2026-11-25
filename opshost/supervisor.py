"""守护编排：轮次生命周期管理 + 后台轮询线程。

轮询线程周期性 settle + observe，让「父先退出、子被 subreaper 收养」这类
异步事件及时反映到轮次记录里。所有停止动作仍然走 Tracker 的身份复核路径。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

from .events import EventLog
from .models import Job, Round
from .store import Store
from .tracker import Tracker

POLL_INTERVAL = 0.25


class Supervisor:
    def __init__(self, store: Store, backend, daemon_pid: int) -> None:
        self.store = store
        self.backend = backend
        self.events = store.events
        self.tracker = Tracker(backend, self.events, daemon_pid)
        self.daemon_pid = daemon_pid
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._thread = threading.Thread(target=self._poll_loop, name="opshost-poller", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def wakeup(self) -> None:
        self._wake.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()

    def poll_once(self) -> None:
        with self._lock:
            self.backend.settle()
            for rnd in self.store.rounds.values():
                if rnd.status == "running":
                    self.tracker.observe(rnd)
                    self._check_natural_finish(rnd)
            self.store.save()

    def _check_natural_finish(self, rnd: Round) -> None:
        if rnd.root is None:
            return
        root_inst = rnd.instances.get(rnd.root.key())
        if root_inst and not root_inst.alive and all(
            not i.alive for i in rnd.instances.values()
            if not i.identity.key().startswith("foreign:")
        ):
            rnd.status = "finished"
            rnd.ended_at = time.time()
            self.events.emit(
                rnd.round_id, "round_finish",
                "轮次根及全部成员已自然退出，轮次结束",
            )

    # ------------------------------------------------------------ 作业/轮次

    def create_job(self, name: str, command: str, cwd: str = "", env: Optional[dict] = None) -> Job:
        job = Job(
            job_id=uuid.uuid4().hex[:12],
            name=name,
            command=command,
            cwd=cwd,
            env=env or {},
        )
        self.store.add_job(job)
        return job

    def start_round(self, job_id: str) -> Round:
        with self._lock:
            job = self.store.get_job(job_id)
            if job is None:
                raise KeyError(f"作业不存在: {job_id}")
            rnd = Round(
                round_id=uuid.uuid4().hex[:12],
                job_id=job_id,
                backend=self.backend.name,
            )
            self.store.add_round(rnd)
            # 继承同作业往轮的 PID 记忆：跨轮 PID 复用必须能识别
            for prev in self.store.rounds_of_job(job_id):
                rnd.observed_pids.update(prev.observed_pids)
            info = self.backend.start_round(job, rnd.round_id)
            self.tracker.register_root(rnd, info)
            self.store.save()
            self.wakeup()
            return rnd

    def stop_round(self, round_id: str, *, force: bool = False) -> dict:
        with self._lock:
            rnd = self.store.get_round(round_id)
            if rnd is None:
                raise KeyError(f"轮次不存在: {round_id}")
            if rnd.status != "running":
                return {"error": f"轮次当前状态为 {rnd.status}，无需停止"}
            summary = self.tracker.stop_round(rnd, force=force)
            self.store.mark_round_ended(round_id, "stopped", summary)
            self.store.save()
            self.wakeup()
            return summary

    def tree(self, round_id: str) -> dict:
        with self._lock:
            rnd = self.store.get_round(round_id)
            if rnd is None:
                raise KeyError(f"轮次不存在: {round_id}")
            return self.tracker.build_tree(rnd)
