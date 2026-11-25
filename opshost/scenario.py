"""演练脚本：在模拟后端上编排题目要求的确定性场景。

场景时间线（跨两个轮次，刻意制造 PID 复用）：

第 1 轮 round-1
  root(100)
   ├─ worker(101)
   └─ parent(102)
        └─ child(103)        # parent 先退出 -> child 被 subreaper 收养，断链=疑似
  动作「父进程先退出」：102 退出并被收割，103 reparent 到守护进程
  停止 round-1：100/101/103 全部按身份带走；收割后 PID 100..103 回到空闲池

第 2 轮 round-2
  root 复用 PID 100，但 start_time 不同 -> 新身份
  动作「同 PID 新进程」：在 100 号 PID 上再放一个与轮次无关的占位进程
  停止 round-2：身份复核发现 100 上是陌生实例，拒绝发信号并保留它
"""

from __future__ import annotations

import signal

from .backends.sim import SimBackend
from .events import EventLog
from .models import Job
from .store import Store
from .supervisor import Supervisor

SCENARIO_JOB_ID = "sim-scenario"
SCENARIO_JOB = Job(
    job_id=SCENARIO_JOB_ID,
    name="演练作业（模拟）",
    command="demo-job.sh",
    cwd="",
    env={},
)


class ScenarioController:
    """把模拟原语封装成页面上的单步演练动作。"""

    def __init__(self, backend: SimBackend, supervisor: Supervisor) -> None:
        self.sim = backend
        self.sup = supervisor
        self.tracker = supervisor.tracker

    # ------------------------------------------------------------ 场景动作

    def spawn_worker(self, round_id: str, comm: str = "worker") -> dict:
        rnd = self._require_running(round_id)
        info = self.sim.spawn(ppid=rnd.root.pid, comm=comm, cmdline=f"{comm} --round {round_id}")
        self.sup.wakeup()
        self.sup.poll_once()
        return {"spawned": info.identity.to_dict()}

    def spawn_parent_child(self, round_id: str) -> dict:
        """父进程派生子进程；稍后父先退出，制造被收养的孤儿。"""
        rnd = self._require_running(round_id)
        parent = self.sim.spawn(ppid=rnd.root.pid, comm="parent", cmdline="parent.sh")
        child = self.sim.spawn(ppid=parent.pid, comm="child", cmdline="child.sh")
        self.sup.poll_once()
        return {
            "parent": parent.identity.to_dict(),
            "child": child.identity.to_dict(),
        }

    def parent_exits_first(self, round_id: str, parent_pid: int) -> dict:
        """父进程先退出：子进程 reparent 到守护进程，启动链断裂。"""
        rnd = self._require_running(round_id)
        # 退出前先找到它的存活子进程（即即将成为孤儿的实例）
        orphan_pid = next(
            (p.pid for p in self.sim.procs.values()
             if p.alive and p.ppid == parent_pid),
            None,
        )
        self.sim.exit_proc(parent_pid, 0)
        self.sim.settle()
        self.sup.poll_once()
        orphan = None
        if orphan_pid is not None:
            orphan = next(
                (i for i in rnd.instances.values() if i.identity.pid == orphan_pid),
                None,
            )
        return {
            "exited_pid": parent_pid,
            "orphan": orphan.identity.to_dict() if orphan else None,
            "orphan_confidence": orphan.confidence if orphan else None,
        }

    def occupy_pid(self, round_id: str, pid: int) -> dict:
        """让一个与轮次无关的新进程占用指定 PID（要求该 PID 已在空闲池）。"""
        rnd = self._require_running(round_id)
        if pid not in self.sim.free_pids:
            raise ValueError(f"PID {pid} 当前不可复用（仍被占用或从未分配）")
        info = self.sim.spawn(
            ppid=self.sim.daemon_pid, comm="unrelated",
            cmdline=f"unrelated-occupant pid={pid}", pid_hint=pid,
        )
        # 外来进程属于另一个会话，确保不会被误登记为轮次成员
        self.sim.procs[info.pid].sid = self.sim.daemon_pid
        self.sup.poll_once()
        return {"occupant": info.identity.to_dict()}

    # ------------------------------------------------------------ 工具

    def _require_running(self, round_id: str):
        rnd = self.sup.store.get_round(round_id)
        if rnd is None:
            raise KeyError(f"轮次不存在: {round_id}")
        if rnd.status != "running":
            raise RuntimeError(f"轮次 {round_id} 已结束（{rnd.status}）")
        return rnd

    def state(self) -> dict:
        return {
            "clock": self.sim.clock,
            "free_pids": list(self.sim.free_pids),
            "procs": [
                {
                    "pid": p.pid, "ppid": p.ppid, "sid": p.sid,
                    "start_time": p.start_time, "comm": p.comm,
                    "alive": p.alive, "identity": p.identity.key(),
                }
                for p in self.sim.procs.values()
            ],
        }


def build_sim_supervisor(store: Store) -> tuple[Supervisor, ScenarioController]:
    backend = SimBackend(daemon_pid=1)
    if not store.jobs:
        store.add_job(SCENARIO_JOB)
    sup = Supervisor(store, backend, daemon_pid=1)
    ctrl = ScenarioController(backend, sup)
    return sup, ctrl
