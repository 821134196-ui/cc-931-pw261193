"""核心场景测试：身份登记、启动链、断链疑似、PID 复用与停止保护。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opshost.backends.sim import SimBackend
from opshost.events import EventLog
from opshost.models import CONFIRMED, FOREIGN, SUSPECT, Job
from opshost.scenario import SCENARIO_JOB, build_sim_supervisor
from opshost.store import Store
from opshost.supervisor import Supervisor
from opshost.tracker import FOREIGN_PREFIX, Tracker


def fresh_stack():
    tmp = tempfile.mkdtemp()
    store = Store(os.path.join(tmp, "state.json"))
    store.load()
    sup, ctrl = build_sim_supervisor(store)
    return store, sup, ctrl


class TestIdentityAndChain(unittest.TestCase):
    def setUp(self):
        self.store, self.sup, self.ctrl = fresh_stack()
        self.sim = self.sup.backend

    def test_full_chain_is_confirmed(self):
        rnd = self.sup.start_round(SCENARIO_JOB.job_id)
        root_pid = rnd.root.pid
        w = self.sim.spawn(root_pid, "worker", "worker.sh")
        self.sup.poll_once()

        inst = rnd.instances[w.identity.key()]
        self.assertEqual(inst.confidence, CONFIRMED)
        # 链从自身闭合到根
        self.assertEqual(inst.chain[-1], rnd.root.key())
        self.assertEqual(inst.chain[0], w.identity.key())

    def test_parent_first_exit_makes_suspect_orphan(self):
        rnd = self.sup.start_round(SCENARIO_JOB.job_id)
        pair = self.ctrl.spawn_parent_child(rnd.round_id)
        parent_pid = pair["parent"]["pid"]
        child_pid = pair["child"]["pid"]

        # 父退出前：parent 与 child 都是确认成员
        child_key = next(
            k for k, v in rnd.instances.items() if v.identity.pid == child_pid
        )
        self.assertEqual(rnd.instances[child_key].confidence, CONFIRMED)

        result = self.ctrl.parent_exits_first(rnd.round_id, parent_pid)
        self.sup.poll_once()

        # child 被 subreaper 收养，重新登记为疑似（保留同身份的一条 stray 记录）
        stray = next(i for i in rnd.instances.values() if i.role == "stray")
        self.assertEqual(stray.identity.pid, child_pid)
        self.assertEqual(stray.confidence, SUSPECT)
        self.assertTrue(any("无法重建" in r for r in stray.reasons))
        self.assertEqual(result["orphan_confidence"], SUSPECT)
        # 模拟内核里 ppid 已指向守护进程
        self.assertEqual(self.sim.procs[child_pid].ppid, self.sim.daemon_pid)

    def test_identity_distinguishes_pid_generations(self):
        # 第一轮：root(100) + worker(101)
        rnd1 = self.sup.start_round(SCENARIO_JOB.job_id)
        self.assertEqual(rnd1.root.pid, 100)
        w1 = self.sim.spawn(100, "worker", "w1")
        self.sup.poll_once()
        old_identity = rnd1.root.key()

        summary = self.sup.stop_round(rnd1.round_id)
        self.assertEqual(rnd1.status, "stopped")
        # 两个成员都被带走
        killed_pids = {int(k.split("#")[0]) for k in summary["killed"]}
        self.assertIn(100, killed_pids)
        self.assertIn(w1.pid, killed_pids)
        # PID 回收
        self.sim.settle()
        self.assertIn(100, self.sim.free_pids)

        # 第二轮：根复用 PID 100，但 start_time 不同
        rnd2 = self.sup.start_round(SCENARIO_JOB.job_id)
        self.assertEqual(rnd2.root.pid, 100)
        self.assertNotEqual(rnd2.root.key(), old_identity)

    def test_stop_protects_reused_pid_occupant(self):
        rnd1 = self.sup.start_round(SCENARIO_JOB.job_id)
        worker = self.ctrl.spawn_worker(rnd1.round_id, "worker")
        worker_pid = worker["spawned"]["pid"]
        self.sup.poll_once()
        self.sup.stop_round(rnd1.round_id)
        self.sim.settle()
        # 第一轮成员（含 worker 的 PID）全部回收
        self.assertIn(worker_pid, self.sim.free_pids)

        rnd2 = self.sup.start_round(SCENARIO_JOB.job_id)
        # 让无关进程占住已回收的旧 PID
        occupant = self.ctrl.occupy_pid(rnd2.round_id, worker_pid)
        self.sup.poll_once()

        # 轮次记录里应出现受保护的外来节点
        foreigns = [v for k, v in rnd2.instances.items() if k.startswith(FOREIGN_PREFIX)]
        self.assertTrue(foreigns)
        f = foreigns[0]
        self.assertEqual(f.confidence, FOREIGN)
        self.assertEqual(f.identity.start_time, occupant["occupant"]["start_time"])

        summary = self.sup.stop_round(rnd2.round_id)
        # 外来 PID 在保护名单里
        self.assertIn(worker_pid, summary["protected_foreign"])
        # 外来进程依然存活，没有被信号误伤
        self.assertTrue(self.sim.procs[worker_pid].alive)
        self.assertEqual(self.sim.procs[worker_pid].comm, "unrelated")

        # 审计事件完整
        kinds = [e["kind"] for e in self.store.events.list(rnd2.round_id)]
        self.assertIn("pid_reuse", kinds)
        self.assertIn("stop_protected", kinds)

    def test_stop_no_false_leftover_when_occupant_shares_worker_pid(self):
        # 边界：外来占用者的 PID 恰好是往轮 worker 的 PID，
        # 停止汇总不得把受保护的外来者误报为遗留。
        rnd1 = self.sup.start_round(SCENARIO_JOB.job_id)
        worker = self.ctrl.spawn_worker(rnd1.round_id, "worker")
        wpid = worker["spawned"]["pid"]
        self.sup.poll_once()
        self.sup.stop_round(rnd1.round_id)
        self.sim.settle()

        rnd2 = self.sup.start_round(SCENARIO_JOB.job_id)
        self.ctrl.occupy_pid(rnd2.round_id, wpid)
        self.sup.poll_once()
        summary = self.sup.stop_round(rnd2.round_id)

        self.assertEqual(summary["leftovers"], [])
        self.assertIn(wpid, summary["protected_foreign"])
        # 外来进程原样保留
        self.assertTrue(self.sim.procs[wpid].alive)
        self.assertEqual(self.sim.procs[wpid].comm, "unrelated")

    def test_unrelated_process_not_claimed(self):
        rnd = self.sup.start_round(SCENARIO_JOB.job_id)
        # 另一个会话里的进程，即便挂在守护进程名下也不得登记
        stranger = self.sim.spawn(self.sim.daemon_pid, "stranger", "outside-session")
        self.sim.procs[stranger.pid].sid = self.sim.daemon_pid  # 不同 SID
        self.sup.poll_once()
        self.assertNotIn(stranger.identity.key(), rnd.instances)


class TestStopTakesOrphans(unittest.TestCase):
    def setUp(self):
        self.store, self.sup, self.ctrl = fresh_stack()
        self.sim = self.sup.backend

    def test_stop_round_kills_orphan_too(self):
        rnd = self.sup.start_round(SCENARIO_JOB.job_id)
        self.ctrl.spawn_worker(rnd.round_id, "worker")
        pair = self.ctrl.spawn_parent_child(rnd.round_id)
        self.ctrl.parent_exits_first(rnd.round_id, pair["parent"]["pid"])
        self.sup.poll_once()

        members_before = [
            i for k, i in rnd.instances.items() if not k.startswith(FOREIGN_PREFIX)
        ]
        self.assertTrue(any(i.confidence == SUSPECT for i in members_before))

        summary = self.sup.stop_round(rnd.round_id)
        # 疑似孤儿也被带走
        killed_pids = {int(k.split("#")[0]) for k in summary["killed"]}
        self.assertIn(pair["child"]["pid"], killed_pids)
        leftovers = [
            p for p in self.sim.procs.values()
            if p.alive and p.sid == rnd.root_sid
        ]
        self.assertEqual(leftovers, [])


class TestPersistence(unittest.TestCase):
    def test_state_survives_reload(self):
        store, sup, ctrl = fresh_stack()
        rnd = sup.start_round(SCENARIO_JOB.job_id)
        ctrl.spawn_worker(rnd.round_id)
        sup.poll_once()
        store.save()

        reloaded = Store(store.path)
        reloaded.load()
        r2 = reloaded.get_round(rnd.round_id)
        self.assertIsNotNone(r2.root)
        self.assertEqual(r2.root, rnd.root)
        self.assertIn(r2.root.key(), r2.instances)
        # 事件也恢复
        self.assertTrue(reloaded.events.list(rnd.round_id))


if __name__ == "__main__":
    unittest.main(verbosity=2)
