"""真实 procfs 后端。

身份要素来自 ``/proc/<pid>/stat`` 的 starttime（第 22 列，单位 CLK_TCK）。
守护进程通过 PR_SET_CHILD_SUBREAPER 收养孤儿，使「父进程先退出」的 worker
重新挂回托管进程之下，轮次停止时可以被发现并带走。
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import signal
import time
from typing import Optional

from ..models import Identity, Job, ProcInfo

# /proc/<pid>/stat 字段（0 基索引，去掉 pid 与 comm 后）
_STATE, _PPID, _PGRP, _SID = 0, 1, 2, 3
_STARTTIME = 19  # 第 22 列 -> 去掉前两列后的索引 19

_libc = ctypes.CDLL(None, use_errno=True)
PR_SET_CHILD_SUBREAPER = 36


def become_subreaper() -> bool:
    """把当前进程设为子收割者，孤儿会被 reparent 到本进程。"""
    return _libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def _read_btime() -> int:
    """系统启动的 epoch 秒，用于把 starttime 换算成人类可读时间。"""
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


_BTIME = _read_btime()
_CLK_TCK = os.sysconf("SC_CLK_TCK")


def read_proc(pid: int, *, retries: int = 1) -> Optional[ProcInfo]:
    """解析 /proc/<pid>/stat；进程已消失返回 None。

    全量扫描时 listdir 与实际读取之间有窗口，对瞬态读取失败按 retries 重试，
    避免把存活进程误判为消失。
    """
    raw = None
    for attempt in range(retries + 1):
        try:
            with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
                raw = f.read()
            break
        except (FileNotFoundError, ProcessLookupError):
            if attempt < retries:
                time.sleep(0.002)
                continue
            return None
        except OSError:
            return None
    if raw is None:
        return None

    # comm 里可能含空格/括号，用最后一个右括号切分
    rp = raw.rfind(")")
    fields = raw[rp + 2:].split()
    comm = raw[raw.find("(") + 1:rp]
    try:
        start = int(fields[_STARTTIME])
        ppid = int(fields[_PPID])
        sid = int(fields[_SID])
        state = fields[_STATE]
    except (IndexError, ValueError):
        return None

    cmdline = comm
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cl = f.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if cl:
            cmdline = cl
    except OSError:
        pass

    return ProcInfo(
        identity=Identity(pid=pid, start_time=start),
        ppid=ppid,
        comm=comm,
        cmdline=cmdline,
        state=state,
        sid=sid,
        alive=True,
    )


class LiveBackend:
    """本机 /proc 后端。"""

    name = "live"

    def __init__(self) -> None:
        self.clk_tck = _CLK_TCK
        self.btime = _BTIME
        self.daemon_pid = os.getpid()
        self._spawned: dict[int, str] = {}   # pid -> round_id（仅用于 spawn 记账）

    # ------------------------------------------------------------ 轮次启动

    def start_round(self, job: Job, round_id: str) -> ProcInfo:
        # 同步派生：管道写端置 CLOEXEC。父进程阻塞在读端，直到子进程
        # 完成 setsid 并 execve 成功（写端被自动关闭 -> EOF）才继续，
        # 避免在子进程 setsid/exec 之前读 /proc 拿到继承来的旧 SID。
        r_fd, w_fd = os.pipe()
        flags = fcntl.fcntl(w_fd, fcntl.F_GETFD)
        fcntl.fcntl(w_fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        env = dict(os.environ)
        env.update(job.env or {})
        pid = os.fork()
        if pid == 0:
            os.close(r_fd)
            try:
                os.setsid()
                if job.cwd:
                    os.chdir(job.cwd)
                os.execvpe("/bin/sh", ["/bin/sh", "-c", job.command], env)
            except BaseException:  # pragma: no cover - exec 失败直接退出
                os._exit(127)
        os.close(w_fd)
        # 阻塞直到子进程 exec（管道写端关闭）或退出
        while True:
            try:
                if not os.read(r_fd, 1):
                    break
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                break
        os.close(r_fd)
        self._spawned[pid] = round_id
        info = read_proc(pid)
        if info is None:  # 命令在两次调度间就退出了
            time.sleep(0.01)
            info = read_proc(pid)
        if info is None:
            raise RuntimeError(f"作业根进程 {pid} 启动后立即消失")
        return info

    # ------------------------------------------------------------ 进程表

    def get(self, pid: int) -> Optional[ProcInfo]:
        return read_proc(pid)

    def snapshot(self) -> dict[int, ProcInfo]:
        out: dict[int, ProcInfo] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return out
        for name in entries:
            if not name.isdigit():
                continue
            info = read_proc(int(name))
            if info is not None:
                out[info.pid] = info
        return out

    # ------------------------------------------------------------ 信号/收割

    def stop_signal(self, identity: Identity, sig: int) -> bool:
        """身份复核后再发信号。

        返回 True  = 身份一致，信号已发出（或进程刚好消失）。
        返回 False = 该 PID 上现存的是另一个 start_time，拒绝发信号。
        """
        current = read_proc(identity.pid)
        if current is None:
            return True  # 已退出，无需信号
        if current.identity != identity:
            return False  # PID 复用：现存进程不是当年的实例
        try:
            os.kill(identity.pid, sig)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            raise

    def settle(self) -> None:
        """非阻塞 waitpid 收割所有已退出子进程（含被 subreaper 收养的孤儿）。"""
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            self._spawned.pop(pid, None)

    @staticmethod
    def is_esrch(exc: OSError) -> bool:
        return exc.errno == errno.ESRCH

    # 供页面展示用
    def epoch_of(self, start_time: int) -> float:
        return self.btime + start_time / self.clk_tck
