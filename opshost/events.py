"""事件日志：启动链、身份复核、停止动作全部留痕，可审计。"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional


class EventLog:
    """线程安全的追加式事件日志（按 round_id 分桶）。"""

    def __init__(self, per_round_capacity: int = 2000) -> None:
        self._lock = threading.RLock()
        self._events: dict[str, deque] = {}
        self._capacity = per_round_capacity

    def emit(
        self,
        round_id: str,
        kind: str,
        message: str,
        *,
        pid: Optional[int] = None,
        identity: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> dict:
        evt = {
            "ts": time.time(),
            "round_id": round_id,
            "kind": kind,
            "message": message,
            "pid": pid,
            "identity": identity,
            "data": data or {},
        }
        with self._lock:
            bucket = self._events.setdefault(round_id, deque(maxlen=self._capacity))
            bucket.append(evt)
        return evt

    def list(self, round_id: str) -> list[dict]:
        with self._lock:
            return list(self._events.get(round_id, ()))

    def snapshot(self) -> dict[str, list[dict]]:
        with self._lock:
            return {rid: list(bucket) for rid, bucket in self._events.items()}

    def load(self, raw: dict[str, list[dict]]) -> None:
        with self._lock:
            self._events = {
                rid: deque(evts, maxlen=self._capacity) for rid, evts in raw.items()
            }
