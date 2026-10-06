"""灾害装备战备与调拨服务的 HTTP/JSON 边界。

沿用基础服务的标准库实现，不引入第三方框架。写入接口通过 ``X-Actor-Id``
标识指挥/操作人员；所有产生资源状态变化的写操作都要求幂等 ``request_id``。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from science_strategy_foundation.errors import DomainError, ValidationError

from .service import ReadinessService
from .storage import DisasterDatabase


def _actor(headers: dict[str, str], body: dict[str, Any]) -> str:
    actor_id = headers.get("X-Actor-Id", "") or body.pop("actor_id", "")
    return actor_id


def route(service: ReadinessService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到战备调拨领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = _actor(headers, body)

    def call(target, **arguments):
        return target(actor_id=actor_id, **arguments)

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        # ------------------------------------------------------------ 基础登记
        if method == "POST" and parsed.path == "/regions":
            return _receipt(call(service.register_region, **body))
        if method == "POST" and parsed.path == "/equipment":
            return _receipt(call(service.register_equipment, **body))
        if method == "POST" and parsed.path == "/certifications":
            return _receipt(call(service.register_certification, **body))
        if method == "POST" and parsed.path == "/maintenance":
            return _receipt(call(service.open_maintenance, **body))
        if method == "POST" and parsed.path == "/maintenance/close":
            return 200, call(service.close_maintenance, **body)
        if method == "POST" and parsed.path == "/crews":
            return _receipt(call(service.register_crew, **body))
        if method == "POST" and parsed.path == "/vehicles":
            return _receipt(call(service.register_vehicle, **body))
        if method == "POST" and parsed.path == "/travel-times":
            return _receipt(call(service.set_travel_time, **body))
        if method == "POST" and parsed.path == "/aid-agreements":
            return _receipt(call(service.register_aid_agreement, **body))
        if method == "POST" and parsed.path == "/missions":
            return _receipt(call(service.create_mission, **body))

        # ------------------------------------------------------------ 查询
        if method == "GET" and parsed.path == "/readiness":
            region_id = query.get("region_id", [""])[0]
            if not region_id:
                raise ValidationError("region_id 不能为空")
            environment = query.get("environment", [None])[0]
            return 200, service.region_readiness(region_id, environment=environment)
        if method == "GET" and parsed.path.startswith("/missions/") and parsed.path.endswith("/status"):
            mission_id = parsed.path.split("/")[2]
            return 200, service.mission_status(mission_id)
        if method == "GET" and parsed.path == "/waitlist":
            mission_id = query.get("mission_id", [None])[0]
            return 200, service.waitlist(mission_id=mission_id)
        if method == "GET" and parsed.path == "/in-flight":
            return 200, service.in_flight()
        if method == "GET" and parsed.path == "/feasibility":
            mission_id = query.get("mission_id", [""])[0]
            if not mission_id:
                raise ValidationError("mission_id 不能为空")
            return 200, service.check_feasibility(mission_id)

        # ------------------------------------------------------------ 承诺生命周期
        if method == "POST" and parsed.path == "/reservations":
            return _receipt(call(service.reserve, **body))
        if method == "POST" and parsed.path == "/dispatches":
            return _receipt(call(service.confirm_dispatch, **body))
        if method == "POST" and parsed.path == "/arrivals":
            return 200, call(service.mark_arrival, **body)
        if method == "POST" and parsed.path == "/faults":
            return 200, call(service.report_fault, **body)
        if method == "POST" and parsed.path == "/substitutes":
            return _receipt(call(service.attach_substitute, **body))
        if method == "POST" and parsed.path == "/extensions":
            return 200, call(service.extend_mission, **body)
        if method == "POST" and parsed.path == "/handovers":
            return _receipt(call(service.handover_to_region, **body))
        if method == "POST" and parsed.path == "/returns":
            return 200, call(service.accept_return, **body)
        if method == "POST" and parsed.path == "/cancellations":
            return 200, call(service.cancel_unexecuted, **body)
        if method == "POST" and parsed.path == "/overrides":
            body["initiator_id"] = body.get("initiator_id", actor_id)
            return _receipt(service.emergency_override(**body))
        if method == "POST" and parsed.path == "/processing/run-due":
            return 200, service.run_due_processing(actor_id=actor_id or "system")

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        payload: dict[str, Any] = {"error": exc.code, "message": str(exc)}
        text = str(exc)
        if text.startswith("{"):
            try:
                payload["details"] = json.loads(text)
                payload["message"] = payload["details"].get("message", text)
            except json.JSONDecodeError:
                pass
        return exc.status, payload
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status = 200 if result.get("replayed") else 201
    return status, result


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: ReadinessService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动灾害装备战备与调拨 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动灾害装备战备与调拨服务")
    parser.add_argument("--database", default="disaster_readiness.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = DisasterDatabase(args.database)
    Handler.service = ReadinessService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
