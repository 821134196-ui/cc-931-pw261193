"""竞态回归：/proc 瞬态读不到时，存活实例不得被误判为退出。

用一个包装后端制造「前 N 次读取随机/定向返回 None」的抖动，
验证 miss_count 宽限机制：未达阈值保持存活，连续超过阈值才判定消失。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opshost.backends.sim import SimBackend
from opshost.events import EventLog
from opshost.models import CONFIRMED, Job
from opshost.scenario import SCENARIO_JOB, build_sim_supervisor
from opshost.store import Store
from opshost.tracker import MISS_THRESHOLD


class FlakyBackend:
    """包装 SimBackend，对指定 pid 的前 flaky_times 次 get 返回 None。"""

    name = "sim"

    def __init__(self, inner: SimBackend, flaky_pid: int, flaky_times: int) -> None:
        self.inner = inner
        self.flaky_pid = flaky_pid
        self.remaining = flaky_times
        self.daemon_pid = inner.daemon_pid

    def get(self, pid: int):
        if pid == self.flaky_pid and self.remaining > 0:
            self.remaining -= 1
            return None
        return self.inner.get(pid)

    def snapshot(self):
        snap = self.inner.snapshot()
        if self.flaky_pid in snap and self.remaining > 0:
            self.remaining -= 1
            snap = dict(snap)
            snap.pop(self.flaky_pid, None)
        return snap

    def __getattr__(self, item):
        return getattr(self.inner, item)


class TestMissingGrace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(tmp, "s.json"))
        self.store.load()
        self.sup, self.ctrl = build_sim_supervisor(self.store)
        self.sim = self.sup.backend

    def test_transient_miss_keeps_instance_alive(self):
        rnd = self.sup.start_round(SCENARIO_JOB.job_id)
        root_pid = rnd.root.pid
        self.sup.poll_once()
        inst = rnd.instances[rnd.root.key()]
        self.assertTrue(inst.alive)

        # 替换成抖动后端：根进程前 (阈值-1) 次读不到，但其实一直存活
        flaky = FlakyBackend(self.sim, root_pid, MISS_THRESHOLD - 1)
        self.sup.backend = flaky
        self.sup.tracker.backend = flaky
        for _ in range(MISS_THRESHOLD - 1):
            self.sup.poll_once()
        # 进程仍在内核里，且实例未被误判退出
        self.assertTrue(self.sim.procs[root_pid].alive)
        self.assertTrue(inst.alive)
        self.assertEqual(inst.exit_event, None)

        # 恢复正常读取，宽限计数应清零
        self.sup.backend = self.sim
        self.sup.tracker.backend = self.sim
        self.sup.poll_once()
        self.assertEqual(inst.miss_count, 0)
        self.assertTrue(inst.alive)

    def test_persistent_miss_eventually_marks_gone(self):
        rnd = self.sup.start_round(SCENARIO_JOB.job_id)
        root_pid = rnd.root.pid
        self.sup.poll_once()
        inst = rnd.instances[rnd.root.key()]

        # 真正移除进程并持续观察，超过阈值后判定消失
        self.sim.exit_proc(root_pid, 0)
        self.sim.settle()  # 收割，snapshot 永久读不到
        for _ in range(MISS_THRESHOLD + 2):
            self.sup.poll_once()
        self.assertFalse(inst.alive)
        self.assertIsNotNone(inst.exit_event)


if __name__ == "__main__":
    unittest.main(verbosity=2)
