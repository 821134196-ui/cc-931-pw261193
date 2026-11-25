/* opshost 前端：原生 JS，无依赖。
 * 核心渲染：进程归属树 + 置信度徽章（图标+文字，颜色不单独承载含义）。
 */
"use strict";

const CONF = {
  confirmed: { label: "确认", icon: "✓", cls: "confirmed" },
  suspect:   { label: "疑似", icon: "?", cls: "suspect" },
  foreign:   { label: "外来", icon: "⛔", cls: "foreign" },
};

const state = {
  live: { jobs: [], selectedJob: null, rounds: [], selectedRound: null, pollTimer: null },
  sim:  { rounds: [], selectedRound: null, lastParentPid: null, lastReapablePid: null, pollTimer: null },
};

/* ----------------------------- 基础工具 ----------------------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const payload = await res.json().catch(() => ({ ok: false, error: "响应解析失败" }));
  if (!payload.ok) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload.data;
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = "toast"), 3200);
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false }) +
    "." + String(d.getMilliseconds()).padStart(3, "0");
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ----------------------------- 进程树渲染 ----------------------------- */

function badge(confidence) {
  const c = CONF[confidence] || CONF.suspect;
  return `<span class="badge ${c.cls}"><span aria-hidden="true">${c.icon}</span>${c.label}</span>`;
}

function renderTree(hostId, tree) {
  const host = document.getElementById(hostId);
  if (!tree || !tree.nodes || Object.keys(tree.nodes).length === 0) {
    host.innerHTML = `<p class="empty">该轮次暂无进程记录。</p>`;
    return;
  }

  const nodeHtml = (key) => {
    const n = tree.nodes[key];
    if (!n) return "";
    const kids = (tree.children[key] || []).map(nodeHtml).join("");
    const reasons = n.reasons && n.reasons.length
      ? `<div class="node-reasons"><b>归属判定说明：</b><ul>${
          n.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`
      : "";
    const occupant = n.live_pid_owner
      ? `<div class="node-reasons"><b>该 PID 现存进程：</b>${esc(n.live_pid_owner)}（非本实例，受保护）</div>`
      : "";
    const stateCls = n.alive ? "alive" : "dead";
    const stateText = n.alive ? "● 存活" : `○ ${esc(n.exit_event || "已退出")}`;
    return `<div class="tree-node">
      <div class="node-card ${n.confidence} ${n.alive ? "" : "dead"}">
        <div class="node-icon" aria-hidden="true">${n.is_foreign ? "🚫" : n.role === "root" ? "🟦" : "⚙️"}</div>
        <div class="node-body">
          <div class="node-title">
            <span class="node-comm">${esc(n.comm)}</span>
            <span class="node-id">${esc(n.identity)}</span>
            ${badge(n.confidence)}
            <span class="role-tag">${esc(n.role)}</span>
            <span class="state-tag ${stateCls}">${stateText}</span>
          </div>
          <div class="node-cmdline">${esc(n.cmdline)}</div>
          ${reasons}
          ${occupant}
          <div class="node-meta">
            <span>PID ${n.pid}</span>
            <span>start_time ${n.start_time}</span>
            <span>链长度 ${n.chain.length}</span>
          </div>
        </div>
      </div>
      ${kids ? `<div class="tree-children">${kids}</div>` : ""}
    </div>`;
  };

  const daemon = `<div class="daemon-node"><span class="dicon">⬇️</span>
    托管守护进程 pid=${tree.daemon.pid}（subreaper，收养父先退出的孤儿）</div>`;
  const roots = (tree.root_keys || []).map(nodeHtml).join("");
  host.innerHTML = daemon + roots;
}

function renderEvents(hostId, events) {
  const host = document.getElementById(hostId);
  if (!events || !events.length) {
    host.innerHTML = `<p class="empty">暂无事件。</p>`;
    return;
  }
  const kindCls = (k) =>
    k.includes("protected") || k.includes("pid_reuse") ? "ev-danger"
    : k.includes("broken") || k.includes("suspect") ? "ev-warn"
    : k.includes("finish") || k.includes("stop") ? "ev-good" : "";
  host.innerHTML = events.slice().reverse().map((e) => `
    <div class="event-row">
      <span class="event-ts">${fmtTime(e.ts)}</span>
      <span class="event-kind ${kindCls(e.kind)}">${esc(e.kind)}</span>
      <span class="event-msg">${esc(e.message)}</span>
    </div>`).join("");
}

function renderRoundTabs(hostId, rounds, selectedId, onPick) {
  const host = document.getElementById(hostId);
  if (!rounds.length) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = rounds.map((r) => {
    const dot = r.status === "running" ? "🟢" : r.status === "stopped" ? "🔴" : "⚪";
    return `<button class="round-chip ${r.round_id === selectedId ? "active" : ""}"
      data-id="${esc(r.round_id)}">${dot} ${esc(r.round_id)} · ${esc(r.status)}</button>`;
  }).join("");
  host.querySelectorAll(".round-chip").forEach((b) =>
    b.addEventListener("click", () => onPick(b.dataset.id)));
}

/* =============================== 真实作业 =============================== */

async function loadLiveJobs() {
  const jobs = await api("/api/live/jobs");
  state.live.jobs = jobs;
  const list = document.getElementById("job-list");
  list.innerHTML = jobs.length ? jobs.map((j) => `
    <li class="job-item ${j.job_id === state.live.selectedJob ? "selected" : ""}" data-id="${esc(j.job_id)}">
      <div class="jname">${esc(j.name)}</div>
      <div class="jcmd">${esc(j.command)}</div>
      <div class="jmeta">${esc(j.job_id)} · ${j.cwd ? esc(j.cwd) : "默认目录"}</div>
    </li>`).join("") : `<p class="empty">还没有作业，先在上方创建。</p>`;
  list.querySelectorAll(".job-item").forEach((li) =>
    li.addEventListener("click", () => selectLiveJob(li.dataset.id)));
  document.getElementById("btn-start-round").disabled = !state.live.selectedJob;
}

async function selectLiveJob(jobId) {
  state.live.selectedJob = jobId;
  await loadLiveJobs();
  state.live.rounds = await api(`/api/live/jobs/${jobId}/rounds`);
  const latest = state.live.rounds[state.live.rounds.length - 1];
  state.live.selectedRound = latest ? latest.round_id : null;
  await refreshLiveRound();
}

document.getElementById("job-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/live/jobs", {
      method: "POST",
      body: {
        name: document.getElementById("job-name").value,
        command: document.getElementById("job-command").value,
        cwd: document.getElementById("job-cwd").value,
      },
    });
    e.target.reset();
    toast("作业已创建");
    await loadLiveJobs();
  } catch (err) {
    toast(err.message, true);
  }
});

document.getElementById("btn-start-round").addEventListener("click", async () => {
  try {
    const rnd = await api(`/api/live/jobs/${state.live.selectedJob}/rounds`, { method: "POST" });
    toast(`轮次 ${rnd.round_id} 已启动`);
    await selectLiveJob(state.live.selectedJob);
    state.live.selectedRound = rnd.round_id;
    await refreshLiveRound();
  } catch (err) {
    toast(err.message, true);
  }
});

async function refreshLiveRound() {
  const rid = state.live.selectedRound;
  const stopBtn = document.getElementById("btn-stop");
  const forceBtn = document.getElementById("btn-stop-force");
  renderRoundTabs("round-tabs", state.live.rounds, rid, async (id) => {
    state.live.selectedRound = id;
    await refreshLiveRound();
  });
  if (!rid) {
    document.getElementById("live-tree").innerHTML =
      `<p class="empty">该作业还没有轮次，点击「启动新一轮」。</p>`;
    document.getElementById("live-events").innerHTML = "";
    stopBtn.disabled = forceBtn.disabled = true;
    return;
  }
  const [rnd, tree, events] = await Promise.all([
    api(`/api/live/rounds/${rid}`),
    api(`/api/live/rounds/${rid}/tree`),
    api(`/api/live/rounds/${rid}/events`),
  ]);
  renderTree("live-tree", tree);
  renderEvents("live-events", events);
  const running = rnd.status === "running";
  stopBtn.disabled = forceBtn.disabled = !running;
  // 同步轮次状态徽标
  const idx = state.live.rounds.findIndex((r) => r.round_id === rid);
  if (idx >= 0) state.live.rounds[idx] = rnd;
}

async function doStopLive(force) {
  try {
    const summary = await api(`/api/live/rounds/${state.live.selectedRound}/stop`, {
      method: "POST",
      body: { force },
    });
    const prot = (summary.protected_foreign || []).length;
    document.getElementById("stop-result").textContent =
      `已终止 ${summary.killed.length} 个实例` +
      (prot ? `，保护 ${prot} 个同 PID 外来进程` : "") +
      (summary.leftovers && summary.leftovers.length ? `，遗留 ${summary.leftovers.length}` : "");
    toast("轮次已停止");
    await selectLiveJob(state.live.selectedJob);
    await refreshLiveRound();
  } catch (err) {
    toast(err.message, true);
  }
}
document.getElementById("btn-stop").addEventListener("click", () => doStopLive(false));
document.getElementById("btn-stop-force").addEventListener("click", () => doStopLive(true));
document.getElementById("btn-refresh").addEventListener("click", refreshLiveRound);

/* =============================== 模拟演练 =============================== */

async function loadSimRounds() {
  state.sim.rounds = await api("/api/sim/rounds");
  if (!state.sim.selectedRound && state.sim.rounds.length) {
    state.sim.selectedRound = state.sim.rounds[state.sim.rounds.length - 1].round_id;
  }
  await refreshSim();
}

function currentSimRound() {
  return state.sim.rounds.find((r) => r.round_id === state.sim.selectedRound) || null;
}

async function simAction(path, body) {
  try {
    const data = await api(path, { method: "POST", body: body || {} });
    await refreshSim();
    return data;
  } catch (err) {
    toast(err.message, true);
    throw err;
  }
}

document.getElementById("sim-new-round").addEventListener("click", async () => {
  await simAction("/api/sim/rounds");
  toast("新轮次已启动（根进程优先复用已回收 PID，但 start_time 不同）");
});

document.getElementById("sim-spawn-worker").addEventListener("click", () =>
  simAction(`/api/sim/rounds/${state.sim.selectedRound}/action/spawn-worker`, { comm: "worker" }));

document.getElementById("sim-spawn-pc").addEventListener("click", async () => {
  const data = await simAction(
    `/api/sim/rounds/${state.sim.selectedRound}/action/spawn-parent-child`);
  if (data && data.parent) state.sim.lastParentPid = data.parent.pid;
  toast("已派生 父→子，下一步让父先退出");
});

document.getElementById("sim-parent-exit").addEventListener("click", async () => {
  const pid = state.sim.lastParentPid;
  if (!pid) return toast("请先执行 ③ 派生 父→子", true);
  const data = await simAction(
    `/api/sim/rounds/${state.sim.selectedRound}/action/parent-exits-first`, { parent_pid: pid });
  state.sim.lastParentPid = null;
  if (data && data.orphan_confidence === "suspect") {
    toast("父已先退出：子进程被 subreaper 收养，启动链断裂 → 标记疑似");
  }
});

document.getElementById("sim-occupy").addEventListener("click", async () => {
  const kernel = await api("/api/sim/state");
  const pid = (kernel.free_pids || [])[0];
  if (!pid) return toast("空闲 PID 池为空：先停止一轮以回收 PID", true);
  state.sim.lastReapablePid = pid;
  await simAction(
    `/api/sim/rounds/${state.sim.selectedRound}/action/occupy-pid`, { pid });
  toast(`无关进程已占用回收来的 PID ${pid}，停止时将被保护`);
});

document.getElementById("sim-stop").addEventListener("click", async () => {
  try {
    const summary = await api(
      `/api/sim/rounds/${state.sim.selectedRound}/stop`, { method: "POST", body: {} });
    const prot = (summary.protected_foreign || []).length;
    document.getElementById("sim-action-result").textContent =
      `终止 ${summary.killed.length} 个，保护外来 ${prot} 个，遗留 ${(summary.leftovers || []).length} 个`;
    toast("轮次已停止");
    await loadSimRounds();
  } catch (err) {
    toast(err.message, true);
  }
});
document.getElementById("sim-refresh").addEventListener("click", refreshSim);

async function refreshSim() {
  state.sim.rounds = await api("/api/sim/rounds");
  const rid = state.sim.selectedRound;
  renderRoundTabs("sim-round-tabs", state.sim.rounds, rid, (id) => {
    state.sim.selectedRound = id;
    refreshSim();
  });

  // 内核状态
  const kernel = await api("/api/sim/state");
  document.getElementById("sim-kernel").innerHTML =
    kernel.procs.map((p) =>
      `<div class="kproc ${p.alive ? "" : "zombie"}">
        <span>${p.alive ? "🟢" : "🧟"} ${p.comm}(${p.pid})</span>
        <span>ppid=${p.ppid} sid=${p.sid} st=${p.start_time}</span></div>`).join("") +
    `<div class="freepids">空闲可复用 PID: [${kernel.free_pids.join(", ")}]</div>`;

  // 动作按钮可用性
  const rnd = currentSimRound();
  const running = rnd && rnd.status === "running";
  for (const id of ["sim-spawn-worker", "sim-spawn-pc", "sim-parent-exit", "sim-occupy", "sim-stop"]) {
    document.getElementById(id).disabled = !running;
  }
  document.getElementById("sim-parent-exit").disabled = !running || !state.sim.lastParentPid;

  if (!rid) {
    document.getElementById("sim-tree").innerHTML =
      `<p class="empty">点击「① 启动新轮次」开始演练。</p>`;
    document.getElementById("sim-events").innerHTML = "";
    return;
  }
  const [tree, events] = await Promise.all([
    api(`/api/sim/rounds/${rid}/tree`),
    api(`/api/sim/rounds/${rid}/events`),
  ]);
  renderTree("sim-tree", tree);
  renderEvents("sim-events", events);
}

/* =============================== 初始化 / 轮询 =============================== */

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("tab-" + t.dataset.tab).classList.add("active");
  }));

async function init() {
  try {
    const info = await api("/api/system/info");
    document.getElementById("system-info").innerHTML =
      `模式 ${esc(info.mode)} · 守护 pid ${info.daemon_pid}<br>` +
      `后端 ${info.backends.join(" / ")} · CLK_TCK ${info.clk_tck ?? "—"}`;
  } catch { /* 忽略 */ }

  await loadLiveJobs();
  if (state.live.jobs.length) await selectLiveJob(state.live.jobs[0].job_id);
  await loadSimRounds();

  // 轻量轮询：仅刷新当前选中的运行中轮次
  setInterval(async () => {
    const lr = state.live.rounds.find((r) => r.round_id === state.live.selectedRound);
    if (lr && lr.status === "running" &&
        document.getElementById("tab-live").classList.contains("active")) {
      await refreshLiveRound();
    }
    const sr = currentSimRound();
    if (sr && sr.status === "running" &&
        document.getElementById("tab-sim").classList.contains("active")) {
      await refreshSim();
    }
  }, 2500);
}

init().catch((e) => toast("初始化失败: " + e.message, true));
