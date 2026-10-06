"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
DISPUTES_RE = re.compile(r"^/api/records/(\d+)/disputes$")
AMENDMENTS_RE = re.compile(r"^/api/records/(\d+)/amendments$")
SERVICES_RE = re.compile(r"^/api/records/(\d+)/service-entries$")
SNAPSHOTS_RE = re.compile(r"^/api/records/(\d+)/snapshots$")
READINESS_RE = re.compile(r"^/api/records/(\d+)/readiness$")
DISPUTE_RE = re.compile(r"^/api/disputes/(\d+)/(accept|decide)$")
AMENDMENT_CONFIRM_RE = re.compile(r"^/api/amendments/(\d+)/confirm$")
MAKEUP_CONFIRM_RE = re.compile(r"^/api/makeup/(\d+)/confirm$")
BATCH_RE = re.compile(r"^/api/batches/([A-Za-z0-9._:-]+)$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "special-education/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            raw_scopes = self.headers.get("X-Scopes", "").strip()
            scopes = tuple(item.strip() for item in re.split(r"[,\s]+", raw_scopes) if item.strip())
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""), scopes=scopes)

        def _batch_id(self) -> Optional[str]:
            value = self.headers.get("X-Batch-Id", "").strip()
            return value or None

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                payload = {"error": exc.code, "message": str(exc)}
                if getattr(exc, "details", None):
                    payload["details"] = exc.details
                self._send(exc.status, payload)
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        @staticmethod
        def _version(body: Dict[str, Any]) -> int:
            version = body.get("expected_version")
            if not isinstance(version, int):
                raise ValidationError("expected_version必须是整数")
            return version

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "special-education", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                actor = self._actor()
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(actor, state=query.get("state", [None])[0],
                                                   limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(actor, int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(actor, int(match.group(1)))})
                    return
                match = DISPUTES_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.list_disputes(actor, int(match.group(1)))})
                    return
                match = AMENDMENTS_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.list_amendments(actor, int(match.group(1)))})
                    return
                match = SERVICES_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.service_entries(actor, int(match.group(1)))})
                    return
                match = SNAPSHOTS_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.snapshots(actor, int(match.group(1)))})
                    return
                match = READINESS_RE.match(parsed.path)
                if match:
                    self._send(200, service.readiness(actor, int(match.group(1))))
                    return
                match = BATCH_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_batch(actor, match.group(1)))
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(actor))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                actor = self._actor()
                batch_id = self._batch_id()
                if parsed.path == "/api/records":
                    record = service.create(actor, body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    record = service.act(actor, int(match.group(1)), self._version(body),
                                         match.group(2), body.get("data", {}), batch_id)
                    self._send(200, record)
                    return
                match = DISPUTES_RE.match(parsed.path)
                if match:
                    result = service.file_dispute(actor, int(match.group(1)), self._version(body),
                                                  body.get("data", {}), batch_id)
                    self._send(201, result)
                    return
                match = AMENDMENTS_RE.match(parsed.path)
                if match:
                    result = service.propose_amendment(actor, int(match.group(1)), self._version(body),
                                                       body.get("data", {}), batch_id)
                    self._send(201, result)
                    return
                match = DISPUTE_RE.match(parsed.path)
                if match:
                    dispute_id = int(match.group(1))
                    if match.group(2) == "accept":
                        result = service.accept_dispute(actor, dispute_id, batch_id)
                    else:
                        result = service.decide_dispute(actor, dispute_id, body.get("data", {}), batch_id)
                    self._send(200, result)
                    return
                match = AMENDMENT_CONFIRM_RE.match(parsed.path)
                if match:
                    expected = body.get("expected_version")
                    if expected is not None and not isinstance(expected, int):
                        raise ValidationError("expected_version必须是整数")
                    result = service.confirm_amendment(actor, int(match.group(1)), expected, batch_id)
                    self._send(200, result)
                    return
                match = MAKEUP_CONFIRM_RE.match(parsed.path)
                if match:
                    result = service.confirm_makeup(actor, int(match.group(1)), body.get("data", {}), batch_id)
                    self._send(200, result)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
