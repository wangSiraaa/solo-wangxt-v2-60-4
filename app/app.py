"""Dependency-free JSON HTTP adapter for ReservationService."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import BusinessError
from .openapi import openapi_spec
from .service import ReservationService

Route = tuple[str, Callable[[BaseHTTPRequestHandler], None]]


class ReservationServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: ReservationService):
        super().__init__(address, RequestHandler)
        self.service = service


class RequestHandler(BaseHTTPRequestHandler):
    server: ReservationServer
    server_version = "LocalReservation/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # Keep test output concise.
        return

    @property
    def service(self) -> ReservationService:
        return self.server.service

    def _send_json(self, status_code: int, payload: Any) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError("invalid_request", "request body must be valid JSON", status_code=400) from exc
        if not isinstance(payload, dict):
            raise BusinessError("invalid_request", "request body must be a JSON object", status_code=400)
        return payload

    def _query(self) -> dict[str, str]:
        parsed = parse_qs(urlparse(self.path).query)
        return {key: values[-1] for key, values in parsed.items() if values}

    def _match(self, method: str) -> tuple[Callable[..., Any], dict[str, str]] | None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        routes: dict[str, list[tuple[str, Callable[..., Any]]]] = ROUTES[method]
        for pattern, handler in routes:
            parts = [part for part in pattern.split("/") if part]
            target = [part for part in path.split("/") if part]
            if len(parts) != len(target):
                continue
            params: dict[str, str] = {}
            for part, value in zip(parts, target):
                if part.startswith("{") and part.endswith("}"):
                    params[part[1:-1]] = value
                elif part != value:
                    break
            else:
                return handler, params
        return None

    def _dispatch(self, method: str) -> None:
        try:
            matched = self._match(method)
            if matched is None:
                raise BusinessError("not_found", "unknown API route", status_code=404)
            handler, params = matched
            result = handler(self, **params)
            if result is None:
                status_code, payload = 204, {}
            elif isinstance(result, tuple):
                status_code, payload = result
            else:
                status_code, payload = 200, result
            self._send_json(status_code, payload)
        except BusinessError as exc:
            self._send_json(exc.status_code, exc.to_dict())
        except Exception as exc:  # Defensive boundary: never leak a stack trace.
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": str(exc)}},
            )

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def health(handler: RequestHandler) -> dict[str, Any]:
    return {"status": "ok", "service": "local-reservation"}


def openapi(handler: RequestHandler) -> dict[str, Any]:
    return openapi_spec()


def segment_rules(handler: RequestHandler) -> dict[str, Any]:
    return {"rules": handler.service.list_segment_rules()}


def list_resources(handler: RequestHandler) -> dict[str, Any]:
    return {"resources": handler.service.list_resources()}


def create_resource(handler: RequestHandler) -> tuple[int, Any]:
    return 201, handler.service.create_resource(handler._read_json())


def expand_resource(handler: RequestHandler, sku: str) -> Any:
    return handler.service.expand_resource(sku, handler._read_json())


def list_units(handler: RequestHandler, sku: str) -> Any:
    return {"sku": sku, "units": handler.service.list_units(sku)}


def set_unit_status(handler: RequestHandler, unit_id: str) -> Any:
    return handler.service.set_unit_status(unit_id, handler._read_json())


def list_reservations(handler: RequestHandler) -> Any:
    return {"plans": handler.service.list_plans()}


def create_reservation(handler: RequestHandler) -> tuple[int, Any]:
    return handler.service.create_reservation(handler._read_json())


def get_reservation(handler: RequestHandler, plan_id: str) -> Any:
    return handler.service.get_plan(plan_id)


def readiness(handler: RequestHandler, plan_id: str) -> Any:
    return handler.service.readiness(plan_id)


def start_plan(handler: RequestHandler, plan_id: str) -> tuple[int, Any]:
    return handler.service.start_plan(plan_id)


def replace_unit(handler: RequestHandler, plan_id: str) -> tuple[int, Any]:
    return handler.service.replace_unit(plan_id, handler._read_json())


def release_plan(handler: RequestHandler, plan_id: str) -> tuple[int, Any]:
    return handler.service.release_plan(plan_id)


def list_audit(handler: RequestHandler) -> Any:
    return {"audit": handler.service.list_audit(handler._query().get("plan_id"))}


ROUTES: dict[str, list[tuple[str, Callable[..., Any]]]] = {
    "GET": [
        ("/healthz", health),
        ("/openapi.json", openapi),
        ("/segment-rules", segment_rules),
        ("/resources", list_resources),
        ("/resources/{sku}/units", list_units),
        ("/reservations", list_reservations),
        ("/reservations/{plan_id}", get_reservation),
        ("/reservations/{plan_id}/readiness", readiness),
        ("/audit-logs", list_audit),
    ],
    "POST": [
        ("/resources", create_resource),
        ("/resources/{sku}/expand", expand_resource),
        ("/resources/units/{unit_id}/status", set_unit_status),
        ("/reservations", create_reservation),
        ("/reservations/{plan_id}/start", start_plan),
        ("/reservations/{plan_id}/replacements", replace_unit),
        ("/reservations/{plan_id}/release", release_plan),
    ],
}


def serve(database: str, host: str = "127.0.0.1", port: int = 8080) -> tuple[ReservationServer, ReservationService]:
    service = ReservationService(database)
    return ReservationServer((host, port), service), service


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run local reservation API")
    parser.add_argument("--db", default="data/reservation.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server, service = serve(args.db, args.host, args.port)
    try:
        print(f"listening on http://{args.host}:{args.port}")
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
