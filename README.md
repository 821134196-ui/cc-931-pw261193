# opshost —— 本地运维作业托管工具

记录本地作业的**启动链**与**每代实例身份**，能在父进程先退出、PID 被复用的
情况下正确判定进程归属：停止作业时带走本轮遗留的 worker，同时证明「占用了
相同 PID 的新进程」不归自己、绝不误杀。

## 核心问题与设计

### 1. 实例身份 = (pid, start_time)

PID 会被内核回收复用，单独的 PID 无法跨时间标识同一进程。
`/proc/<pid>/stat` 第 22 列的 starttime（系统启动后的时钟滴答）对每一代实例
唯一，因此身份定义为：

```
identity = (pid, start_time)   # 字符串形式  pid#start_time
```

同一 PID 的新老两代 start_time 必然不同，身份因此可区分。模拟后端使用同一套
接口，用单调模拟时钟给出 start_time，行为与真实 procfs 一致。

### 2. subreaper 收养「父先退出」的 worker

守护进程通过 `prctl(PR_SET_CHILD_SUBREAPER)` 成为子收割者。父进程先退出后，
其子进程被 reparent 到守护进程名下而非 PID 1，从而仍在托管范围内、可被发现
和停止。

### 3. 启动链判定：确认 / 疑似 / 外来

| 判定 | 条件 | 页面标识 |
|---|---|---|
| **确认 confirmed** | 沿 ppid 逐级回溯，祖先链全部命中在档身份并闭合到轮次根 | ✓ 确认（绿） |
| **疑似 suspect** | 链断裂（父已退出，无法重建血缘），但同 SID 且被 subreaper 直接收养 | ? 疑似（黄，附原因） |
| **外来 foreign** | 在档/历史 PID 被另一个 start_time 的进程占用，与本轮无启动链关系 | ⛔ 外来（红，受保护） |

疑似节点一定会在页面上写明**为什么无法确认**，而不是静默归入或丢弃。

### 4. 停止时的身份复核

每次发信号前重新读取 `/proc/<pid>/stat` 复核身份：

- 身份一致 → 发 SIGTERM，宽限后仍存活再 SIGKILL；
- 身份不符（PID 已复用）→ **拒绝发信号**，记录为受保护的外来进程并写审计事件；
- 全程事件留痕（`root_spawn` / `chain_broken` / `pid_reuse` / `stop_signal` /
  `stop_protected` / `round_stopped` …）。

### 5. 竞态防护

- 派生用 **pipe + CLOEXEC 同步**：父进程等到子进程完成 `setsid` 且 `execve`
  成功后才读 `/proc`，避免读到继承来的旧 SID；
- 消失判定用**连续宽限计数**：全量扫描存在 listdir/read 窗口，单次读不到
  `/proc` 不立即判死，连续超过阈值才确认消失，避免把存活进程误判退出。

## 目录结构

```
opshost/
  models.py            身份 Identity / 实例 Instance / 作业 Job / 轮次 Round
  events.py            追加式审计事件日志
  store.py             JSON 原子持久化（重启后仍能复核历代身份）
  tracker.py           观察、登记、启动链解析、身份复核、停止（系统核心）
  supervisor.py        轮次生命周期 + 后台轮询线程
  scenario.py          模拟演练脚本（父先退出、PID 复用、外来占用）
  server.py            标准库 HTTP 服务（零第三方依赖）
  main.py              CLI 入口
  backends/
    base.py            后端接口
    live.py            真实 procfs 后端（fork/exec、subreaper、信号）
    sim.py             内存模拟内核（fork/reparent/僵尸/PID 复用）
  web/                 原生 HTML/CSS/JS 页面，无构建
jobs/                  示例真实作业（含父先退出的孤儿场景）
tests/                 13 个单元/端到端测试
```

## 运行

要求 Linux（依赖 `/proc` 与 `prctl`）、Python 3.10+，**无第三方依赖**。

```bash
# 同时开启真实作业与模拟演练（默认）
python3 -m opshost.main --mode both --port 8000

# 仅模拟 / 仅真实
python3 -m opshost.main --mode sim  --port 8000
python3 -m opshost.main --mode live --port 8000
```

打开 http://127.0.0.1:8000 ，两个页签：

- **真实本地作业**：创建作业 → 启动一轮 → 查看真实进程树归属 → 停止。
  可直接用 `jobs/demo-job.sh`（含父先退出留下的疑似孤儿）。
- **模拟演练**：按 ①～⑥ 单步确定性复现跨轮 PID 复用与外来进程保护。

## HTTP API（节选）

```
GET  /api/system/info
POST /api/live/jobs                       创建作业
GET  /api/live/jobs/<job_id>/rounds       作业的轮次列表
POST /api/live/jobs/<job_id>/rounds       启动一轮真实作业
GET  /api/live/rounds/<rid>/tree          归属树（含置信度与原因）
GET  /api/live/rounds/<rid>/events        审计事件
POST /api/live/rounds/<rid>/stop          停止（body: {"force": false}）

POST /api/sim/rounds                      启动模拟轮次
POST /api/sim/rounds/<rid>/action/<name>  演练动作：
       spawn-worker | spawn-parent-child | parent-exits-first | occupy-pid
GET  /api/sim/state                       模拟内核进程表与空闲 PID 池
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `test_tracker.py`：身份登记、启动链、断链疑似、跨轮 PID 复用、停止保护、持久化
- `test_race.py`：瞬态读不到的宽限机制、持续消失的最终判定
- `test_live.py`：真实 fork/exec、subreaper 收养、真实身份复核拒绝（端到端）
