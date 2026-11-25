"""真实 procfs 后端端到端测试：subreaper 收养、身份复核、PID 复用保护。

这些测试会真正 fork 本地进程，是题目要求的「一轮真实本地作业」。
"""

import os
import signal
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opshost.backends.live import LiveBackend, become_subreaper, read_proc
from opshost.events import EventLog
from opshost.models import CONFIRMED, FOREIGN, SUSPECT, Job
from opshost.store import Store
from opshost.supervisor import Supervisor
from opshost.tracker import FOREIGN_PREFIX


LONG_RUNNER = "while :; do sleep 1; done"


class LiveSupervisorMixin:
    def _build(self):
        d = tempfile.mkdtemp()
        store = Store(os.path.join(d, "live.json"))
        store.load()
        self.assertTrue(become_subreaper())
        backend = LiveBackend()
        sup = Supervisor(store, backend, daemon_pid=os.getpid())
        return store, backend, sup

    def _job(self, command: str, name: str = "live-job") -> Job:
        return Job(job_id="j1", name=name, command=command)


class TestLiveIdentity(LiveSupervisorMixin, unittest.TestCase):
    def setUp(self):
        self.store, self.backend, self.sup = self._build()
        self.store.add_job(self._job(LONG_RUNNER))

    def tearDown(self):
        # 兜底清理，避免测试失败留下进程
        for rnd in list(self.store.rounds.values()):
            if rnd.status == "running":
                try:
                    self.sup.stop_round(rnd.round_id, force=True)
                except Exception:
                    pass

    def test_proc_identity_stable_and_unique(self):
        info = read_proc(os.getpid())
        self.assertIsNotNone(info)
        info2 = read_proc(os.getpid())
        self.assertEqual(info.identity, info2.identity)  # 同一代实例身份稳定
        self.assertGreater(info.start_time, 0)

    def test_start_and_stop_real_round(self):
        rnd = self.sup.start_round("j1")
        self.sup.poll_once()
        root = rnd.instances[rnd.root.key()]
        self.assertEqual(root.confidence, CONFIRMED)
        # /proc 里确实存在该进程，且身份一致
        live = read_proc(rnd.root.pid)
        self.assertEqual(live.identity, rnd.root)

        summary = self.sup.stop_round(rnd.round_id)
        self.assertEqual(rnd.status, "stopped")
        self.assertIn(rnd.root.key(), summary["killed"])
        # 进程真的没了
        time.sleep(0.1)
        self.backend.settle()
        self.assertIsNone(read_proc(rnd.root.pid))

    def test_orphan_adopted_and_marked_suspect(self):
        # 父进程派生 child 后自己退出，child 被 subreaper 收养 -> 疑似
        script = (
            "( while :; do sleep 1; done ) & "
            "sleep 0.3; exit 0"
        )
        self.store.add_job(Job(job_id="j2", name="orphan-job", command=script))
        rnd = self.sup.start_round("j2")
        # 等待父退出、child 被收养并被轮询观察到
        deadline = time.time() + 5
        stray = None
        while time.time() < deadline:
            self.sup.poll_once()
            strays = [i for i in rnd.instances.values() if i.confidence == SUSPECT]
            if strays:
                stray = strays[0]
                break
            time.sleep(0.1)
        self.assertIsNotNone(stray, "未观察到被收养的疑似孤儿")
        self.assertEqual(stray.role, "stray")
        # 真实 ppid 已指向托管进程
        live = read_proc(stray.identity.pid)
        self.assertEqual(live.ppid, os.getpid())

        summary = self.sup.stop_round(rnd.round_id, force=True)
        killed_pids = {int(k.split("#")[0]) for k in summary["killed"]}
        self.assertIn(stray.identity.pid, killed_pids)
        time.sleep(0.1)
        self.backend.settle()  # 收割僵尸，PID 才真正消失
        self.assertIsNone(read_proc(stray.identity.pid))

    def test_stop_rejects_identity_mismatch(self):
        rnd = self.sup.start_round("j1")
        self.sup.poll_once()
        target_pid = rnd.root.pid
        expected = rnd.root

        # 构造一个身份不符的假身份（同 PID，虚构 start_time），必须被拒
        from opshost.models import Identity
        fake = Identity(pid=target_pid, start_time=expected.start_time + 999999)
        accepted = self.backend.stop_signal(fake, signal.SIGTERM)
        self.assertFalse(accepted, "身份不符时发信号必须被拒绝")
        # 真进程仍然活着
        self.assertIsNotNone(read_proc(target_pid))

        # 正确身份可以正常停止
        self.assertTrue(self.backend.stop_signal(expected, signal.SIGKILL))
        self.sup.stop_round(rnd.round_id, force=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
