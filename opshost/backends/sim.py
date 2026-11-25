"""模拟后端：在内存里忠实复刻 Linux 进程语义，用于确定性演练与测试。

模拟的内核行为：
- fork：子进程继承 sid，得到新 PID（可按 hint 复用已释放的 PID）与新的 start_time；
- 父进程先退出：存活子进程 reparent 给守护进程（subreaper），成为孤儿；
- kill：默认 SIGTERM/SIGKILL 使进程退出并变僵尸，由守护进程 wait 收割；
- PID 复用：旧进程被收割后 PID 回到空闲池，再分配时 start_time 一定不同，
  因此 (pid, start_time) 身份能区分两代实例——与真实 procfs 行为一致。
"""

from __future__ import annotations

import signal
from typing import Optional

from ..models import Identity, Job, ProcInfo


class SimProc:
    def __init__(
        self,
        pid: int,
        ppid: int,
        sid: int,
        start_time: int,
        comm: str,
        cmdline: str,
    ) -> None:
        self.pid = pid
        self.ppid = ppid
        self.sid = sid
        self.start_time = start_time
        self.comm = comm
        self.cmdline = cmdline
        self.alive = True          # False = 僵尸，等待 wait
        self.exit_code: Optional[int] = None

    @property
    def identity(self) -> Identity:
        return Identity(self.pid, self.start_time)

    def to_info(self) -> ProcInfo:
        return ProcInfo(
            identity=self.identity,
            ppid=self.ppid,
            comm=self.comm,
            cmdline=self.cmdline,
            state="Z" if not self.alive else "S",
            sid=self.sid,
            alive=self.alive,
        )


class SimBackend:
    name = "sim"

    def __init__(self, daemon_pid: int = 1) -> None:
        self.daemon_pid = daemon_pid
        self.clock = 1000                     # 模拟启动滴答，单调递增
        self.procs: dict[int, SimProc] = {}   # 含僵尸，直到被 wait
        self.free_pids: list[int] = []        # 已回收、可复用的 PID
        self.next_pid = 100                   # 新 PID 起点（避开 daemon 的 1）
        self._pid_hint: Optional[int] = None  # 下次 fork 期望复用的 PID
        self._round_counter = 0

    # ------------------------------------------------------------ 进程表

    def get(self, pid: int) -> Optional[ProcInfo]:
        p = self.procs.get(pid)
        return p.to_info() if p is not None else None

    def snapshot(self) -> dict[int, ProcInfo]:
        return {pid: p.to_info() for pid, p in self.procs.items()}

    # ------------------------------------------------------------ 轮次启动

    def start_round(self, job: Job, round_id: str) -> ProcInfo:
        self._round_counter += 1
        # 让第 2 轮复用第 1 轮根的 PID，天然制造 PID 复用场景
        if self._round_counter >= 2:
            first_free = self.free_pids[0] if self.free_pids else None
            if first_free is not None:
                self._pid_hint = first_free
        return self.spawn(
            ppid=self.daemon_pid,
            comm="sh",
            cmdline=f"/bin/sh -c {job.command}",
            new_session=True,
        )

    # ------------------------------------------------------------ 模拟原语

    def _alloc_pid(self) -> int:
        if self._pid_hint is not None:
            hint = self._pid_hint
            self._pid_hint = None
            if hint in self.free_pids:
                self.free_pids.remove(hint)
                return hint
        if self.free_pids:
            return self.free_pids.pop(0)
        pid = self.next_pid
        self.next_pid += 1
        return pid

    def spawn(
        self,
        ppid: int,
        comm: str,
        cmdline: str,
        *,
        new_session: bool = False,
        pid_hint: Optional[int] = None,
    ) -> ProcInfo:
        """模拟 fork+exec：诞生一代新实例（新 start_time）。"""
        if pid_hint is not None:
            self._pid_hint = pid_hint
        parent = self.procs.get(ppid)
        sid = parent.sid if parent else ppid
        pid = self._alloc_pid()
        self.clock += 1
        proc = SimProc(
            pid=pid,
            ppid=ppid,
            sid=pid if new_session else sid,
            start_time=self.clock,
            comm=comm,
            cmdline=cmdline,
        )
        self.procs[pid] = proc
        return proc.to_info()

    def exit_proc(self, pid: int, code: int = 0) -> None:
        """模拟进程主动退出：存活子进程 reparent 给守护进程。"""
        proc = self.procs.get(pid)
        if proc is None or not proc.alive:
            return
        proc.alive = False
        proc.exit_code = code
        for child in list(self.procs.values()):
            if child.ppid != pid:
                continue
            if child.alive:
                child.ppid = self.daemon_pid       # 孤儿被 subreaper 收养
            else:
                self._reap(child.pid)              # 僵尸子进程由收养者直接回收
        self.clock += 1

    def stop_signal(self, identity: Identity, sig: int) -> bool:
        proc = self.procs.get(identity.pid)
        if proc is None:
            return True  # 已被收割，视为无需处理
        if proc.identity != identity:
            return False  # PID 上已是另一代实例：拒绝
        if sig in (signal.SIGTERM, signal.SIGKILL, signal.SIGINT, signal.SIGHUP):
            # 模拟信号致死：走完整退出语义（孤儿 reparent），守护进程名下的
            # 僵尸立即收割，PID 立刻回到空闲池——与 live 端 waitpid 的净效果一致。
            self.exit_proc(identity.pid, 128 + sig)
            self.settle()
        return True

    def _reap(self, pid: int) -> None:
        self.procs.pop(pid, None)
        if pid not in self.free_pids:
            self.free_pids.append(pid)

    def settle(self) -> None:
        """模拟 waitpid：收割守护进程名下的全部僵尸，PID 回到空闲池。"""
        for pid in [pid for pid, p in self.procs.items()
                    if not p.alive and p.ppid == self.daemon_pid]:
            self._reap(pid)
        self.clock += 1

    # ------------------------------------------------------------ 演练脚本用

    def reset(self) -> None:
        self.procs.clear()
        self.free_pids.clear()
        self.next_pid = 100
        self.clock = 1000
        self._round_counter = 0
        self._pid_hint = None
