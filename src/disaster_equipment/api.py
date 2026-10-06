"""灾害装备战备与调拨服务的 HTTP/JSON 边界（仅标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from science_strategy_foundation.errors import DomainError
from science_strategy_foundation.storage import Database

from .service import EquipmentService


def route(service: EquipmentService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到装备战备服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    segments = [segment for segment in parsed.path.split("/") if segment]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and segments == ["health"]:
            valid, count = service.domain.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count,
                         "recovery": service.recovery_summary}
        if method == "POST" and segments == ["equipment"]:
            result, replayed = service.register_equipment(actor_id=actor_id, **body)
            return (200 if replayed else 201), result
        if method == "POST" and len(segments) == 3 and segments[0] == "equipment" \
                and segments[2] == "maintenance":
            result, replayed = service.record_maintenance(actor_id=actor_id,
                                                          equipment_id=segments[1], **body)
            return (200 if replayed else 201), result
        if method == "GET" and segments == ["equipment"]:
            items = service.list_equipment(site_id=query.get("site_id", [None])[0],
                                           category=query.get("category", [None])[0])
            return 200, {"items": items}
        if method == "POST" and segments == ["teams"]:
            result, replayed = service.register_team(actor_id=actor_id, **body)
            return (200 if replayed else 201), result
        if method == "POST" and segments == ["routes"]:
            result, replayed = service.register_route(actor_id=actor_id, **body)
            return (200 if replayed else 201), result
        if method == "POST" and segments == ["agreements"]:
            result, replayed = service.register_agreement(actor_id=actor_id, **body)
            return (200 if replayed else 201), result
        if method == "POST" and segments == ["alerts"]:
            result, replayed = service.raise_alert(actor_id=actor_id, **body)
            return (200 if replayed else 201), result
        if method == "POST" and len(segments) == 3 and segments[0] == "tasks":
            task_id, action = segments[1], segments[2]
            if action == "accept":
                result, replayed = service.accept_task(actor_id=actor_id, task_id=task_id, **body)
                return (200 if replayed else 201), result
            if action == "dispatch":
                result, replayed = service.dispatch_task(actor_id=actor_id, task_id=task_id, **body)
                return (200 if replayed else 201), result
            if action == "extend":
                result, replayed = service.extend_task(actor_id=actor_id, task_id=task_id, **body)
                return (200 if replayed else 201), result
            if action == "cancel":
                result, replayed = service.cancel_task(actor_id=actor_id, task_id=task_id, **body)
                return (200 if replayed else 201), result
            if action == "takeover":
                result, replayed = service.takeover_task(actor_id=actor_id, task_id=task_id, **body)
                return (200 if replayed else 201), result
            if action == "finish":
                result, replayed = service.finish_task(actor_id=actor_id, task_id=task_id, **body)
                return (200 if replayed else 201), result
        if method == "GET" and segments == ["tasks"]:
            items = service.list_tasks(site_id=query.get("site_id", [None])[0],
                                       status=query.get("status", [None])[0])
            return 200, {"items": items}
        if method == "GET" and len(segments) == 2 and segments[0] == "tasks":
            return 200, service.get_task(segments[1])
        if method == "GET" and segments == ["waitlist"]:
            return 200, {"items": service.waitlist(site_id=query.get("site_id", [None])[0])}
        if method == "POST" and len(segments) == 3 and segments[0] == "dispatches":
            dispatch_id, action = segments[1], segments[2]
            if action == "arrival":
                result, replayed = service.confirm_arrival(actor_id=actor_id,
                                                           dispatch_id=dispatch_id, **body)
                return (200 if replayed else 201), result
            if action == "breakdown":
                result, replayed = service.report_breakdown(actor_id=actor_id,
                                                            dispatch_id=dispatch_id, **body)
                return (200 if replayed else 201), result
            if action == "return":
                result, replayed = service.confirm_return(actor_id=actor_id,
                                                          dispatch_id=dispatch_id, **body)
                return (200 if replayed else 201), result
        if method == "GET" and len(segments) == 2 and segments[0] == "dispatches":
            return 200, service.get_dispatch(segments[1])
        if method == "POST" and segments == ["overrides"]:
            result, replayed = service.request_override(actor_id=actor_id, **body)
            return (200 if replayed else 201), result
        if method == "POST" and len(segments) == 3 and segments[0] == "overrides" \
                and segments[2] == "confirm":
            result, replayed = service.confirm_override(actor_id=actor_id,
                                                        override_id=segments[1], **body)
            return (200 if replayed else 201), result
        if method == "GET" and len(segments) == 3 and segments[0] == "regions" \
                and segments[2] == "capability":
            return 200, service.region_capability(segments[1], at=query.get("at", [None])[0])
        if method == "POST" and segments == ["recovery"]:
            return 200, service.recover()
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: EquipmentService

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
    parser.add_argument("--database", default="disaster_equipment.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = EquipmentService(database)
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
