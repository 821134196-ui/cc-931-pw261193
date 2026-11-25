"""轮次追踪器：观察进程表、登记实例身份、解析启动链、执行带身份复核的停止。

判定规则（真实后端与模拟器共用同一套）：

确认(confirmed)
    从实例沿 ppid 逐级回溯，每一级都能在轮次已登记的身份中闭合到轮次根。

疑似(suspect)
    回溯链在到达根之前断裂。典型场景：父进程先退出，子进程被 subreaper
    收养而直接挂到守护进程之下，历史血缘无法再从实时进程表证明。
    此时用「同会话 SID + 直接挂在守护进程名下」作为旁证登记为疑似，
    并在 reasons 中写明无法确认的原因。

外来(foreign)
    已登记实例的 PID 此刻被另一个 start_time 的进程占用——PID 已复用，
    新进程与本轮次无关，停止时必须原样保留、只记录证据。
"""

from __future__ import annotations

import signal
import time
from typing import Optional

from .events import EventLog
from .models import (
    CONFIRMED,
    FOREIGN,
    SUSPECT,
    Identity,
    Instance,
    ProcInfo,
    Round,
)

FOREIGN_PREFIX = "foreign:"
TERM_GRACE_SECONDS = 2.0
STOP_TICK_SECONDS = 0.05
# 连续多少次观察读不到 /proc/<pid> 才判定进程真正消失。
# 全量扫描时 listdir 与 read_proc 之间存在窗口，单次读不到可能只是瞬态竞态。
MISS_THRESHOLD = 3


class _GhostOwner:
    """跨轮 PID 复用场景里，旧实例只存在于往轮记录、不在当前实例表。"""

    def __init__(self, pid: int, old_key: str) -> None:
        old_pid, old_start = old_key.split("#", 1)
        self.identity = Identity(pid=int(old_pid), start_time=int(old_start))


class Tracker:
    def __init__(self, backend, events: EventLog, daemon_pid: int) -> None:
        self.backend = backend
        self.events = events
        self.daemon_pid = daemon_pid

    # ============================================================ 登记

    def register_root(self, rnd: Round, info: ProcInfo) -> Instance:
        inst = Instance(
            identity=info.identity,
            ppid_at_register=info.ppid,
            comm=info.comm,
            cmdline=info.cmdline,
            role="root",
            confidence=CONFIRMED,
            reasons=["轮次根进程，由托管守护直接派生"],
            chain=[info.identity.key()],
        )
        rnd.root = info.identity
        rnd.root_sid = info.sid
        rnd.instances[inst.identity.key()] = inst
        rnd.observed_pids[info.pid] = info.identity.key()
        self.events.emit(
            rnd.round_id, "root_spawn",
            f"启动轮次根进程 pid={info.pid} 身份={info.identity.key()}",
            pid=info.pid, identity=info.identity.key(),
            data={"sid": info.sid, "cmdline": info.cmdline},
        )
        return inst

    def _register(
        self, rnd: Round, info: ProcInfo, *, role: str,
        confidence: str, reasons: list[str], chain: list[str],
    ) -> Instance:
        inst = Instance(
            identity=info.identity,
            ppid_at_register=info.ppid,
            comm=info.comm,
            cmdline=info.cmdline,
            role=role,
            confidence=confidence,
            reasons=reasons,
            chain=chain,
        )
        rnd.instances[inst.identity.key()] = inst
        rnd.observed_pids[info.pid] = inst.identity.key()
        self.events.emit(
            rnd.round_id,
            "member_register" if confidence == CONFIRMED else "member_suspect",
            f"登记{role} pid={info.pid} 身份={info.identity.key()}（{confidence}）",
            pid=info.pid, identity=info.identity.key(),
            data={"confidence": confidence, "reasons": reasons, "chain": chain},
        )
        return inst

    # ============================================================ 观察

    def observe(self, rnd: Round) -> None:
        """一轮进程表扫描：更新在档实例、发现新成员、识别 PID 复用。"""
        snap = self.backend.snapshot()
        known_by_pid: dict[int, Instance] = {}
        for inst in rnd.instances.values():
            known_by_pid.setdefault(inst.identity.pid, inst)

        # 1) 在档实例的存活/复用检查，并对存活成员重新评估启动链
        for fkey, inst in list(rnd.instances.items()):
            if fkey.startswith(FOREIGN_PREFIX):
                continue
            current = snap.get(inst.identity.pid)
            if current is None:
                self._handle_missing(rnd, inst)
                continue
            inst.miss_count = 0  # 读到了，清零宽限计数
            inst.last_seen = time.time()
            if current.identity != inst.identity:
                if current.alive:
                    # PID 上已是另一代存活实例：登记为受保护的外来者
                    self._record_foreign(rnd, inst, current)
                # 复用者已是僵尸：等收割后由 mark_gone 路径处理
                continue
            if not current.alive:
                self._mark_zombie(rnd, inst)
                continue
            self._reevaluate_confidence(rnd, inst, current, snap)

        # 2) 发现新成员（只看与根同会话的存活进程，避免把无关作业卷进来）
        if rnd.root_sid is None:
            return
        foreign_pids = {f.identity.pid for f in self._foreigns(rnd)}
        # 2a) 独立的 PID 复用扫描：轮次历史上出现过的 PID 若被其他会话的
        #     新进程占用，即便无法走启动链，也要标记为受保护的外来者。
        foreign_pids = {f.identity.pid for f in self._foreigns(rnd)}
        for pid, old_key in rnd.observed_pids.items():
            if pid in foreign_pids:
                continue
            current = snap.get(pid)
            if current is None or not current.alive or current.sid == rnd.root_sid:
                continue
            if current.identity.key() == old_key:
                continue  # 还是原来那一代，只是会话归属不同，交给正常流程
            owner = rnd.instances.get(old_key)
            self._record_foreign(rnd, owner or _GhostOwner(pid, old_key), current)
        foreign_pids = {f.identity.pid for f in self._foreigns(rnd)}
        pending = [
            info for pid, info in snap.items()
            if info.alive
            and info.sid == rnd.root_sid
            and pid not in known_by_pid
            and pid not in foreign_pids
        ]
        # 按 ppid 拓扑排序：父先于子。仍可能因数据竞争存在缺环，
        # 因此循环重试，直到一轮扫描没有任何新登记为止。
        pending.sort(key=lambda i: i.ppid)
        changed = True
        while changed:
            changed = False
            for info in list(pending):
                if info.identity.key() in rnd.instances:
                    pending.remove(info)
                    changed = True
                    continue
                before = len(rnd.instances)
                self._classify_new(rnd, info, snap)
                if len(rnd.instances) > before:
                    pending.remove(info)
                    changed = True

    def _classify_new(self, rnd: Round, info: ProcInfo, snap: dict[int, ProcInfo]) -> None:
        chain = self._resolve_chain(rnd, info, snap)
        if chain:
            self._register(
                rnd, info, role="worker", confidence=CONFIRMED,
                reasons=["实时 ppid 链可逐级闭合到轮次根"], chain=chain,
            )
            return
        # 断链：仅当进程直接挂在守护进程名下且同会话时，按疑似收养孤儿登记
        if info.ppid == self.daemon_pid:
            self._downgrade_stray(rnd, info)
        # 其余断链进程与本轮次没有足够关联，忽略不登记

    def _reevaluate_confidence(
        self, rnd: Round, inst: Instance, current: ProcInfo, snap: dict[int, ProcInfo]
    ) -> None:
        """父进程先退出后，已登记成员的启动链可能断裂，需要降级为疑似。"""
        if inst.role == "root":
            return
        chain = self._resolve_chain(rnd, current, snap)
        if chain:
            if inst.confidence != CONFIRMED:
                inst.confidence = CONFIRMED
                inst.role = "worker"
                inst.reasons = ["实时 ppid 链可逐级闭合到轮次根"]
                inst.chain = chain
                self.events.emit(
                    rnd.round_id, "chain_restored",
                    f"pid={current.pid} 启动链恢复，重新确认为轮次成员",
                    pid=current.pid, identity=current.identity.key(),
                )
            else:
                inst.chain = chain
            return
        # 链已断裂：被守护进程直接收养则降级疑似，否则保留原判定等待下一轮观察
        if current.ppid == self.daemon_pid and inst.confidence == CONFIRMED:
            self._downgrade_stray(rnd, current, existing=inst)

    def _downgrade_stray(
        self, rnd: Round, info: ProcInfo, existing: Optional[Instance] = None
    ) -> None:
        reasons = [
            "父进程已先退出，实时进程表中无法重建到轮次根的血缘",
            "旁证：与轮次根同会话(SID 相同)且被 subreaper 直接收养",
        ]
        chain = [info.identity.key(), f"daemon:{self.daemon_pid}"]
        if existing is not None:
            existing.confidence = SUSPECT
            existing.role = "stray"
            existing.reasons = reasons
            existing.chain = chain
            inst = existing
        else:
            inst = self._register(
                rnd, info, role="stray", confidence=SUSPECT,
                reasons=reasons, chain=chain,
            )
        self.events.emit(
            rnd.round_id, "chain_broken",
            f"pid={info.pid} 启动链断裂，按疑似孤儿登记（身份不变：{info.identity.key()}）",
            pid=info.pid, identity=info.identity.key(),
        )

    def _resolve_chain(self, rnd: Round, info: ProcInfo, snap: dict[int, ProcInfo]) -> list[str]:
        """沿 ppid 回溯；祖先链全程命中在档身份并闭合到根则返回 key 链，否则 []。

        链的顺序为 [自身, 父, ..., 根]，根固定在链尾。
        注意：``info`` 自身是待登记的新进程，不在实例表中是正常的，
        因此只要求祖先（ppid 起的每一级）都已登记。
        """
        chain = [info.identity.key()]
        cur = snap.get(info.ppid)
        seen: set[int] = {info.pid}
        while cur is not None and cur.pid not in seen:
            seen.add(cur.pid)
            if cur.identity.key() not in rnd.instances:
                return []  # 祖先未登记，血缘暂时无法证明
            chain.append(cur.identity.key())
            if cur.identity == rnd.root:
                return chain  # 链尾闭合于轮次根
            cur = snap.get(cur.ppid)
        return []

    # ------------------------------------------------------------ 状态变迁

    def _handle_missing(self, rnd: Round, inst: Instance) -> None:
        """读不到 /proc/<pid>：先累计宽限，连续超过阈值才判定消失。"""
        inst.miss_count += 1
        if inst.miss_count < MISS_THRESHOLD:
            return  # 可能只是扫描窗口内的瞬态不可读，保留存活判定
        self._mark_gone(rnd, inst)

    def _mark_zombie(self, rnd: Round, inst: Instance) -> None:
        if inst.alive and inst.exit_event is None:
            inst.alive = False
            inst.exit_event = "exited"
            self.events.emit(
                rnd.round_id, "member_exit",
                f"pid={inst.identity.pid} 身份={inst.identity.key()} 已退出，等待收割",
                pid=inst.identity.pid, identity=inst.identity.key(),
            )

    def _mark_gone(self, rnd: Round, inst: Instance) -> None:
        if inst.alive or inst.exit_event is None:
            inst.alive = False
            inst.exit_event = inst.exit_event or "vanished"
            self.events.emit(
                rnd.round_id, "member_reaped",
                f"pid={inst.identity.pid} 身份={inst.identity.key()} 已被内核回收",
                pid=inst.identity.pid, identity=inst.identity.key(),
            )

    def _record_foreign(self, rnd: Round, inst: Instance, current: ProcInfo) -> None:
        fkey = FOREIGN_PREFIX + current.identity.key()
        if fkey in rnd.instances:
            return
        note = Instance(
            identity=current.identity,
            ppid_at_register=current.ppid,
            comm=current.comm,
            cmdline=current.cmdline,
            role="occupant",
            confidence=FOREIGN,
            reasons=[
                f"PID {current.pid} 已被内核复用：新实例 start_time={current.start_time}，"
                f"轮次登记的旧实例 start_time={inst.identity.start_time}",
                "新进程与本轮次无启动链关系，停止时不会向其发送任何信号",
            ],
            chain=[],
        )
        rnd.instances[fkey] = note
        self.events.emit(
            rnd.round_id, "pid_reuse",
            f"检测到 PID 复用：pid={current.pid} 旧身份 {inst.identity.key()} "
            f"-> 新身份 {current.identity.key()}（外来，受保护）",
            pid=current.pid, identity=current.identity.key(),
            data={"old_identity": inst.identity.key(), "new_identity": current.identity.key()},
        )

    @staticmethod
    def _foreigns(rnd: Round) -> list[Instance]:
        return [v for k, v in rnd.instances.items() if k.startswith(FOREIGN_PREFIX)]

    # ============================================================ 停止

    def stop_round(self, rnd: Round, *, force: bool = False, timeout: float = TERM_GRACE_SECONDS) -> dict:
        """停止轮次：先 SIGTERM 宽限，后 SIGKILL；每发一次信号都做身份复核。"""
        summary = {"killed": [], "protected_foreign": [], "already_gone": [], "phases": []}
        # 强杀模式跳过宽限直接 KILL；否则先 TERM 等待，仍存活再 KILL 兜底
        if force:
            phases = [("SIGKILL", signal.SIGKILL, 0.0)]
        else:
            phases = [("SIGTERM", signal.SIGTERM, timeout), ("SIGKILL", signal.SIGKILL, 0.0)]

        for sig_name, sig, wait in phases:
            self.backend.settle()
            self.observe(rnd)
            # 已登记的外来占位节点：不动一根手指，只做审计
            for finst in self._foreigns(rnd):
                if finst.identity.pid not in summary["protected_foreign"]:
                    summary["protected_foreign"].append(finst.identity.pid)
                    self.events.emit(
                        rnd.round_id, "stop_protected",
                        f"跳过外来进程 pid={finst.identity.pid} 身份={finst.identity.key()}："
                        f"它占用了轮次历史 PID 但与本轮次无启动链关系，不发送任何信号",
                        pid=finst.identity.pid, identity=finst.identity.key(),
                    )
            alive_members = [
                i for key, i in rnd.instances.items()
                if not key.startswith(FOREIGN_PREFIX) and i.alive
            ]
            if not alive_members:
                break
            phase = {"signal": sig_name, "targets": [], "protected": [], "rejected": []}
            for inst in alive_members:
                ok = self.backend.stop_signal(inst.identity, sig)
                if ok:
                    inst.alive = False
                    inst.exit_event = "killed"
                    phase["targets"].append(inst.identity.key())
                    summary["killed"].append(inst.identity.key())
                    self.events.emit(
                        rnd.round_id, "stop_signal",
                        f"向 pid={inst.identity.pid} 发送 {sig_name}，身份复核通过 "
                        f"({inst.identity.key()})",
                        pid=inst.identity.pid, identity=inst.identity.key(),
                        data={"signal": sig_name, "confidence": inst.confidence},
                    )
                else:
                    current = self.backend.get(inst.identity.pid)
                    self._record_foreign(rnd, inst, current)
                    phase["protected"].append(inst.identity.pid)
                    summary["protected_foreign"].append(inst.identity.pid)
                    self.events.emit(
                        rnd.round_id, "stop_protected",
                        f"拒绝向 pid={inst.identity.pid} 发送 {sig_name}：身份复核失败，"
                        f"该 PID 现存进程不是轮次登记的实例（PID 复用，受保护）",
                        pid=inst.identity.pid,
                        data={"expected": inst.identity.key()},
                    )
            summary["phases"].append(phase)
            self.backend.settle()
            if wait:
                self._wait_drain(rnd, wait)

        # 收尾：最终观察，确认无遗留成员（外来占位节点不属于本轮，跳过）
        self.backend.settle()
        self.observe(rnd)
        for key, inst in rnd.instances.items():
            if key.startswith(FOREIGN_PREFIX):
                continue
            if inst.exit_event is None:
                inst.alive = False
                inst.exit_event = "vanished"
                summary["already_gone"].append(inst.identity.key())
        # 注意：外来占位节点的字典键带 foreign: 前缀，但其 identity.key() 不带，
        # 因此遗留判定必须基于字典键而非身份串，否则会把受保护的外来者误算成遗留。
        leftovers = [
            inst.identity.key()
            for key, inst in rnd.instances.items()
            if not key.startswith(FOREIGN_PREFIX) and inst.alive
        ]
        summary["leftovers"] = leftovers
        self.events.emit(
            rnd.round_id, "round_stopped",
            f"轮次停止完成：信号终止 {len(summary['killed'])} 个，"
            f"保护外来进程 {len(set(summary['protected_foreign']))} 个，遗留 {len(leftovers)} 个",
            data=summary,
        )
        return summary

    def _wait_drain(self, rnd: Round, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(STOP_TICK_SECONDS)
            self.backend.settle()
            self.observe(rnd)
            if not any(
                inst.alive and not key.startswith(FOREIGN_PREFIX)
                for key, inst in rnd.instances.items()
            ):
                return

    # ============================================================ 树形视图

    def build_tree(self, rnd: Round) -> dict:
        """构造供页面渲染的归属树。"""
        snap = self.backend.snapshot()
        nodes: dict[str, dict] = {}

        for key, inst in rnd.instances.items():
            is_foreign = key.startswith(FOREIGN_PREFIX)
            live = snap.get(inst.identity.pid)
            live_match = live is not None and live.identity == inst.identity and live.alive
            chain = list(inst.chain)
            parent_key = None
            if len(chain) >= 2:
                parent_key = chain[-2]
            elif inst.confidence == SUSPECT:
                parent_key = f"daemon:{self.daemon_pid}"
            nodes[key] = {
                "key": key,
                "pid": inst.identity.pid,
                "start_time": inst.identity.start_time,
                "identity": inst.identity.key(),
                "comm": inst.comm,
                "cmdline": inst.cmdline,
                "role": inst.role,
                "confidence": inst.confidence,
                "reasons": list(inst.reasons),
                "chain": chain,
                "alive": bool(live_match),
                "exit_event": inst.exit_event,
                "parent_key": parent_key,
                "is_foreign": is_foreign,
                "live_pid_owner": (
                    f"{live.identity.key()} ({live.comm})"
                    if live is not None and live.identity != inst.identity else None
                ),
            }

        daemon_key = f"daemon:{self.daemon_pid}"
        children: dict[str, list[str]] = {}
        for key, node in nodes.items():
            pk = node["parent_key"] or daemon_key
            children.setdefault(pk, []).append(key)

        root_keys = children.pop(daemon_key, [])
        return {
            "round_id": rnd.round_id,
            "daemon": {"key": daemon_key, "pid": self.daemon_pid},
            "root_identity": rnd.root.key() if rnd.root else None,
            "nodes": nodes,
            "children": children,
            "root_keys": root_keys,
        }
