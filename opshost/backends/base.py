"""后端抽象：真实 procfs 与模拟器实现同一套接口，追踪器只依赖这里。"""

from __future__ import annotations

from typing import Optional, Protocol

from ..models import Identity, ProcInfo


class ProcTable(Protocol):
    """进程表快照来源。"""

    def get(self, pid: int) -> Optional[ProcInfo]:
        """读取单个进程的当前信息；进程不存在返回 None。"""
        ...

    def snapshot(self) -> dict[int, ProcInfo]:
        """pid -> ProcInfo 的全量快照。"""
        ...


class Backend(ProcTable, Protocol):
    """追踪器需要的全部后端能力。"""

    name: str

    # 轮次生命周期
    def start_round(self, job, round_id: str) -> Identity:
        ...

    def stop_signal(self, identity: Identity, sig: int) -> bool:
        """向指定**身份**发信号；身份不匹配（PID 已被复用）时拒绝并返回 False。"""
        ...

    def settle(self) -> None:
        """收割僵尸、推进模拟时钟（live 后端为 waitpid 非阻塞收割）。"""
        ...
