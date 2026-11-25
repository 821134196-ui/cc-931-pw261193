"""领域模型：进程身份、实例、作业、轮次。

身份设计说明
============
Linux 上 PID 会被回收复用，单独的 PID 无法跨时间标识同一进程。
``/proc/<pid>/stat`` 第 22 列是进程自系统启动以来的启动时刻（时钟滴答），
同一 PID 新老两代的 start_time 必然不同。因此实例身份定义为::

    identity = (pid, start_time)

该身份在「同一台机器、同一次内核 uptime」内唯一；模拟器使用同一套接口，
用单调的模拟时钟给出 start_time，行为与真实 procfs 后端一致。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------- 进程身份

@dataclass(frozen=True)
class Identity:
    """某一代进程实例的唯一身份。"""

    pid: int
    start_time: int  # 系统启动后的滴答数(CLK_TCK)，模拟器里为模拟时钟

    def key(self) -> str:
        return f"{self.pid}#{self.start_time}"

    def to_dict(self) -> dict:
        return {"pid": self.pid, "start_time": self.start_time}

    @staticmethod
    def from_dict(d: dict) -> "Identity":
        return Identity(pid=int(d["pid"]), start_time=int(d["start_time"]))


@dataclass
class ProcInfo:
    """对 /proc/<pid>/stat 的后端中立快照。"""

    identity: Identity
    ppid: int
    comm: str
    cmdline: str
    state: str
    sid: int                      # 会话 ID；同会话是断链时的重要旁证
    alive: bool = True

    @property
    def pid(self) -> int:
        return self.identity.pid

    @property
    def start_time(self) -> int:
        return self.identity.start_time


# ---------------------------------------------------------------- 归属判定

CONFIRMED = "confirmed"   # 启动链完整，可证明属于该轮次
SUSPECT = "suspect"       # 链有断点，只能标疑似
FOREIGN = "foreign"       # 有证据表明不属于该轮次（如 PID 被新进程占用）

CONFIDENCE_LABELS = {
    CONFIRMED: "确认",
    SUSPECT: "疑似",
    FOREIGN: "外来",
}


@dataclass
class Instance:
    """轮次内登记的一个进程实例。"""

    identity: Identity
    ppid_at_register: int
    comm: str
    cmdline: str
    role: str = "worker"          # root / worker / stray
    confidence: str = CONFIRMED
    reasons: list[str] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)          # 身份 key 链，根在最后
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    alive: bool = True
    exit_event: Optional[str] = None                       # exited / killed / vanished
    miss_count: int = 0  # 连续读不到 /proc 的次数，防瞬态竞态误判退出

    def to_dict(self) -> dict:
        return {
            "identity": self.identity.to_dict(),
            "ppid_at_register": self.ppid_at_register,
            "comm": self.comm,
            "cmdline": self.cmdline,
            "role": self.role,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "chain": list(self.chain),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "alive": self.alive,
            "exit_event": self.exit_event,
            "miss_count": self.miss_count,
        }

    @staticmethod
    def from_dict(d: dict) -> "Instance":
        return Instance(
            identity=Identity.from_dict(d["identity"]),
            ppid_at_register=d["ppid_at_register"],
            comm=d["comm"],
            cmdline=d["cmdline"],
            role=d.get("role", "worker"),
            confidence=d.get("confidence", CONFIRMED),
            reasons=list(d.get("reasons", [])),
            chain=list(d.get("chain", [])),
            first_seen=d.get("first_seen", 0.0),
            last_seen=d.get("last_seen", 0.0),
            alive=d.get("alive", True),
            exit_event=d.get("exit_event"),
            miss_count=d.get("miss_count", 0),
        )


# ---------------------------------------------------------------- 作业/轮次

@dataclass
class Job:
    """一条可托管的本地作业定义。"""

    job_id: str
    name: str
    command: str
    cwd: str = ""
    env: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Job":
        return Job(**d)


@dataclass
class Round:
    """作业的一次执行。"""

    round_id: str
    job_id: str
    backend: str                       # live / sim
    root: Optional[Identity] = None
    root_sid: Optional[int] = None
    instances: dict[str, Instance] = field(default_factory=dict)   # key -> Instance
    # 本轮次（含同作业往轮）见过的全部 PID，用于跨轮检测 PID 复用
    observed_pids: dict[int, str] = field(default_factory=dict)    # pid -> 旧身份 key
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    status: str = "running"            # running / stopped / finished
    stop_summary: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "job_id": self.job_id,
            "backend": self.backend,
            "root": self.root.to_dict() if self.root else None,
            "root_sid": self.root_sid,
            "instances": {k: v.to_dict() for k, v in self.instances.items()},
            "observed_pids": dict(self.observed_pids),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "stop_summary": self.stop_summary,
        }

    @staticmethod
    def from_dict(d: dict) -> "Round":
        return Round(
            round_id=d["round_id"],
            job_id=d["job_id"],
            backend=d["backend"],
            root=Identity.from_dict(d["root"]) if d.get("root") else None,
            root_sid=d.get("root_sid"),
            instances={k: Instance.from_dict(v) for k, v in d.get("instances", {}).items()},
            observed_pids={int(k): v for k, v in d.get("observed_pids", {}).items()},
            started_at=d.get("started_at", 0.0),
            ended_at=d.get("ended_at"),
            status=d.get("status", "running"),
            stop_summary=d.get("stop_summary"),
        )
