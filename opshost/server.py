"""HTTP 服务：标准库实现，零第三方依赖。

路由前缀 /api/live 与 /api/sim 分别对应真实后端与模拟后端；
模拟后端额外提供 /api/sim/action/* 演练动作。
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .backends.live import LiveBackend
from .scenario import SCENARIO_JOB_ID, build_sim_supervisor
from .store import Store
from .supervisor import Supervisor

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class HttpGateway:
    def __init__(self, store: Store, mode: str, host: str, port: int) -> None:
        self.store = store
        self.mode = mode
        self.host = host
        self.port = port
        self.live_sup: Supervisor | None = None
        self.live: LiveBackend | None = None
        if mode in ("live", "both"):
            self.live = LiveBackend()
            self.live_sup = Supervisor(store, self.live, daemon_pid=os.getpid())
        self.sim_sup = None
        self.sim_ctrl = None
        if mode in ("sim", "both"):
            self.sim_sup, self.sim_ctrl = build_sim_supervisor(store)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 启停

    def start(self) -> None:
        if self.live_sup:
            self.live_sup.start()
        if self.sim_sup:
            self.sim_sup.start()
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="opshost-http", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        if self._server:
            self._server.shutdown()
        if self.live_sup:
            self.live_sup.shutdown()
        if self.sim_sup:
            self.sim_sup.shutdown()

    # ------------------------------------------------------------ 路由分发

    def dispatch(self, method: str, path: str, body: dict) -> object:
        if not path.startswith("/api/"):
            raise ApiError(404, "not found")
        parts = [p for p in path.split("/") if p]
        # parts: ['api', 'live'|'sim', ...]
        if len(parts) < 2:
            raise ApiError(404, "not found")
        backend_name = parts[1]
        sub = parts[2:]

        if backend_name == "live":
            if self.live_sup is None:
                raise ApiError(400, "服务未启用 live 后端（当前模式不含真实作业）")
            return self._route_crud(method, sub, body, self.live_sup)
        if backend_name == "sim":
            if self.sim_sup is None:
                raise ApiError(400, "服务未启用 sim 后端")
            return self._route_sim(method, sub, body)
        if backend_name == "system":
            return self._route_system(sub)
        raise ApiError(404, "unknown backend")

    def _route_system(self, sub: list[str]) -> object:
        if sub == ["info"]:
            return {
                "mode": self.mode,
                "daemon_pid": os.getpid(),
                "backends": [
                    name for name, sup in (("live", self.live_sup), ("sim", self.sim_sup))
                    if sup is not None
                ],
                "clk_tck": self.live.clk_tck if self.live else None,
                "btime": self.live.btime if self.live else None,
            }
        raise ApiError(404, "unknown system route")

    def _route_crud(self, method: str, sub: list[str], body: dict, sup: Supervisor) -> object:
        # /api/<b>/jobs
        if sub == ["jobs"]:
            if method == "GET":
                return [j.to_dict() for j in sup.store.jobs.values()]
            if method == "POST":
                job = sup.create_job(
                    name=str(body.get("name", "未命名作业")),
                    command=str(body.get("command", "")),
                    cwd=str(body.get("cwd", "")),
                    env=body.get("env") or {},
                )
                return job.to_dict()
            raise ApiError(405, "method not allowed")

        # /api/<b>/jobs/<job_id>/rounds
        if len(sub) == 3 and sub[0] == "jobs" and sub[2] == "rounds":
            job_id = sub[1]
            if method == "GET":
                return [r.to_dict() for r in sup.store.rounds_of_job(job_id)]
            if method == "POST":
                return sup.start_round(job_id).to_dict()
            raise ApiError(405, "method not allowed")

        # /api/<b>/rounds/<round_id>[/tree|/events|/stop]
        if len(sub) >= 2 and sub[0] == "rounds":
            round_id = sub[1]
            rnd = sup.store.get_round(round_id)
            if rnd is None:
                raise ApiError(404, "轮次不存在")
            if len(sub) == 2:
                if method != "GET":
                    raise ApiError(405, "method not allowed")
                return rnd.to_dict()
            action = sub[2]
            if action == "tree" and method == "GET":
                return sup.tree(round_id)
            if action == "events" and method == "GET":
                return sup.store.events.list(round_id)
            if action == "stop" and method == "POST":
                return sup.stop_round(round_id, force=bool(body.get("force", False)))
            raise ApiError(404, f"no route: {sub}")

        raise ApiError(404, f"no route: {sub}")

    def _route_sim(self, method: str, sub: list[str], body: dict) -> object:
        sup, ctrl = self.sim_sup, self.sim_ctrl
        # 模拟内核状态视图
        if sub == ["state"] and method == "GET":
            sup.poll_once()
            return ctrl.state()
        if sub == ["jobs"] and method == "GET":
            return [j.to_dict() for j in sup.store.jobs.values()]

        # /api/sim/rounds ...
        if sub and sub[0] == "rounds":
            if sub == ["rounds"] and method == "POST":
                return sup.start_round(SCENARIO_JOB_ID).to_dict()
            if sub == ["rounds"] and method == "GET":
                return [r.to_dict() for r in sup.store.rounds.values()]
            if len(sub) < 2:
                raise ApiError(404, "no sim route")
            round_id = sub[1]
            rnd = sup.store.get_round(round_id)
            if rnd is None:
                raise ApiError(404, "轮次不存在")
            if len(sub) == 2 and method == "GET":
                return rnd.to_dict()
            if len(sub) == 3:
                action = sub[2]
                if action == "tree" and method == "GET":
                    return sup.tree(round_id)
                if action == "events" and method == "GET":
                    return sup.store.events.list(round_id)
                if action == "stop" and method == "POST":
                    return sup.stop_round(round_id, force=bool(body.get("force", False)))
            # 演练动作 /api/sim/rounds/<id>/action/<name>
            if len(sub) == 4 and sub[2] == "action" and method == "POST":
                action = sub[3]
                if action == "spawn-worker":
                    return ctrl.spawn_worker(round_id, str(body.get("comm", "worker")))
                if action == "spawn-parent-child":
                    return ctrl.spawn_parent_child(round_id)
                if action == "parent-exits-first":
                    return ctrl.parent_exits_first(round_id, int(body["parent_pid"]))
                if action == "occupy-pid":
                    return ctrl.occupy_pid(round_id, int(body["pid"]))
        raise ApiError(404, f"no sim route: {sub}")

    # ------------------------------------------------------------ HTTP 包装

    def _make_handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "opshost/1.0"

            def log_message(self, fmt, *args):  # 静默默认访问日志
                pass

            def _send_json(self, status: int, payload) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise ApiError(400, f"请求体不是合法 JSON: {exc}")
                if not isinstance(parsed, dict):
                    raise ApiError(400, "请求体必须是 JSON 对象")
                return parsed

            def _serve_static(self, path: str) -> None:
                rel = path.lstrip("/") or "index.html"
                # 防目录穿越
                full = os.path.normpath(os.path.join(WEB_DIR, rel))
                if not full.startswith(WEB_DIR + os.sep) and full != WEB_DIR:
                    self._send_json(403, {"error": "forbidden"})
                    return
                if os.path.isdir(full):
                    full = os.path.join(full, "index.html")
                if not os.path.isfile(full):
                    self._send_json(404, {"error": "not found"})
                    return
                ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                with open(full, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path.startswith("/api/"):
                        body = self._read_body() if method in ("POST", "PUT") else {}
                        result = gateway.dispatch(method, parsed.path, body)
                        self._send_json(200, {"ok": True, "data": result})
                    elif method == "GET":
                        self._serve_static(parsed.path)
                    else:
                        self._send_json(405, {"error": "method not allowed"})
                except ApiError as exc:
                    self._send_json(exc.status, {"ok": False, "error": exc.message})
                except (KeyError, ValueError, RuntimeError) as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001 - 兜底，保证服务不挂
                    self._send_json(500, {"ok": False, "error": f"内部错误: {exc}"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        return Handler


def serve(store_path: str, mode: str, host: str, port: int) -> HttpGateway:
    store = Store(store_path)
    store.load()
    gw = HttpGateway(store, mode, host, port)
    gw.start()
    return gw
