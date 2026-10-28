from __future__ import annotations

import json
import zlib
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from google.protobuf.message import DecodeError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from tracebook.adapters.base import Adapter
from tracebook.capture import Capture
from tracebook.config import Config
from tracebook.model import now, obj
from tracebook.otel import decode, ingest_otlp
from tracebook.reports import nightly_window, report
from tracebook.store import Store


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "").split(":")[0]
        origin = request.headers.get("origin")
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "Invalid local Host"}, status_code=403)
        if origin and (
            urlsplit(origin).hostname not in {"127.0.0.1", "localhost"}
            or urlsplit(origin).netloc != request.headers.get("host")
        ):
            return JSONResponse({"detail": "Cross-origin access denied"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response


def create_api(
    store: Store, config: Config, registry: dict[str, Adapter], capture: Capture | None = None
) -> FastAPI:
    app = FastAPI(title="Tracebook", docs_url=None, redoc_url=None)
    app.add_middleware(LocalOnlyMiddleware)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "time": now(),
            "capture_errors": capture.errors if capture else {},
            "local_only": True,
            "zero_model_tokens": True,
        }

    @app.get("/api/overview")
    async def overview(
        start: str = "", end: str = "9999", agent: str | None = None
    ) -> dict[str, Any]:
        return {**store.overview(start, end, agent), "billing_mode": config.billing_mode}

    @app.get("/api/adapters")
    async def adapter_list() -> list[dict[str, Any]]:
        return [
            {
                "id": a.id,
                "name": a.display_name,
                "verification": a.verification,
                "capabilities": asdict(a.capabilities()),
            }
            for a in registry.values()
        ]

    @app.get("/api/projects")
    async def projects() -> list[dict[str, Any]]:
        return store.rows(
            "SELECT p.*,COUNT(s.id) sessions FROM projects p LEFT JOIN sessions s ON s.project_id=p.id GROUP BY p.id ORDER BY sessions DESC"
        )

    @app.get("/api/sessions")
    async def sessions(
        agent: str | None = None,
        project: str | None = None,
        q: str = "",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        filters = ["1=1"]
        params: list[object] = []
        if agent:
            filters.append("s.agent=?")
            params.append(agent)
        if project:
            filters.append("s.project_id=?")
            params.append(project)
        if q:
            filters.append(
                "EXISTS(SELECT 1 FROM turns t WHERE t.session_id=s.id AND t.prompt LIKE ?)"
            )
            params.append("%" + q + "%")
        params += [limit, offset]
        return store.rows(
            "SELECT s.*,p.root project,(SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id AND t.kind='prompt') turns,(SELECT COUNT(*) FROM tool_calls c WHERE c.session_id=s.id) tools,(SELECT SUM(COALESCE(input_uncached,0)+COALESCE(input_cache_read,0)+COALESCE(input_cache_write,0)+COALESCE(output,0)+COALESCE(reasoning,0)) FROM accounted_model_calls m WHERE m.session_id=s.id) tokens,(SELECT prompt FROM turns t WHERE t.session_id=s.id AND t.prompt IS NOT NULL ORDER BY started_at LIMIT 1) first_prompt,(SELECT GROUP_CONCAT(DISTINCT model) FROM accounted_model_calls m WHERE m.session_id=s.id AND m.model IS NOT NULL) models FROM sessions s LEFT JOIN projects p ON p.id=s.project_id WHERE "
            + " AND ".join(filters)
            + " ORDER BY s.started_at DESC LIMIT ? OFFSET ?",
            tuple(params),
        )

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str) -> dict[str, Any]:
        rows = store.rows(
            "SELECT * FROM sessions WHERE id=? OR native_id=?", (session_id, session_id)
        )
        if not rows:
            raise HTTPException(404, "Session not found")
        session_row = rows[0]
        actual_session_id = str(session_row["id"])
        data_obj: dict[str, Any] = (
            json.loads(session_row["data"]) if session_row.get("data") else {}
        )
        parent_native_id = data_obj.get("parent_native_id")
        parent_session = None
        if parent_native_id:
            p_rows = store.rows(
                "SELECT id, agent, native_id, started_at FROM sessions WHERE native_id=?",
                (parent_native_id,),
            )
            if p_rows:
                parent_session = p_rows[0]

        subagents = store.rows(
            "SELECT s.id, s.agent, s.native_id, s.started_at, s.data, "
            "(SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id) turns, "
            "(SELECT COUNT(*) FROM tool_calls c WHERE c.session_id=s.id) tools, "
            "(SELECT SUM(COALESCE(input_uncached,0)+COALESCE(input_cache_read,0)+COALESCE(input_cache_write,0)+COALESCE(output,0)+COALESCE(reasoning,0)) FROM accounted_model_calls m WHERE m.session_id=s.id) tokens "
            "FROM sessions s WHERE json_extract(data, '$.parent_native_id') = ?",
            (session_row["native_id"],),
        )

        turns_with_usage = store.rows(
            "SELECT t.*, "
            "COALESCE(SUM(m.input_uncached), 0) input_uncached, "
            "COALESCE(SUM(m.input_cache_read), 0) input_cache_read, "
            "COALESCE(SUM(m.input_cache_write), 0) input_cache_write, "
            "COALESCE(SUM(m.output), 0) output, "
            "COALESCE(SUM(m.reasoning), 0) reasoning, "
            "COALESCE(SUM(COALESCE(m.cost_reported, m.cost_estimated)), 0) cost, "
            "GROUP_CONCAT(DISTINCT m.model) models "
            "FROM turns t "
            "LEFT JOIN accounted_model_calls m ON m.turn_id = t.id "
            "WHERE t.session_id = ? "
            "GROUP BY t.id "
            "ORDER BY t.started_at, t.id "
            "LIMIT 1000",
            (actual_session_id,),
        )

        return {
            **session_row,
            "parent_session": parent_session,
            "subagents": subagents,
            "turns": turns_with_usage,
            "tools": store.rows(
                "SELECT * FROM tool_calls WHERE session_id=? ORDER BY ts LIMIT 1000",
                (actual_session_id,),
            ),
            "findings": store.rows(
                "SELECT * FROM findings WHERE session_id=? ORDER BY ts", (actual_session_id,)
            ),
        }

    @app.get("/api/turns/{turn_id}")
    async def turn_detail(turn_id: str) -> dict[str, Any]:
        rows = store.rows("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not rows:
            raise HTTPException(404, "Turn not found")
        return {
            **rows[0],
            "calls": store.rows(
                "SELECT * FROM accounted_model_calls WHERE turn_id=? ORDER BY ts", (turn_id,)
            ),
            "tools": store.rows("SELECT * FROM tool_calls WHERE turn_id=? ORDER BY ts", (turn_id,)),
        }

    @app.get("/api/events")
    async def events(
        session_id: str,
        turn_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        filters = "session_id=?" + (" AND turn_id=?" if turn_id else "")
        params: tuple[object, ...] = (
            (session_id, turn_id, limit, offset) if turn_id else (session_id, limit, offset)
        )
        return [
            {**row, "data": json.loads(row["data"]), "provenance": json.loads(row["provenance"])}
            for row in store.rows(
                f"SELECT * FROM events WHERE {filters} ORDER BY ts,id LIMIT ? OFFSET ?", params
            )
        ]

    @app.get("/api/findings")
    async def findings(
        limit: int = Query(100, ge=1, le=1000), agent: str | None = None
    ) -> list[dict[str, Any]]:
        return store.rows(
            "SELECT * FROM findings "
            + ("WHERE agent=? " if agent else "")
            + "ORDER BY ts DESC LIMIT ?",
            (agent, limit) if agent else (limit,),
        )

    @app.get("/api/reports")
    async def reports_list() -> list[dict[str, Any]]:
        return store.rows(
            "SELECT id,window_start,window_end,created_at FROM reports ORDER BY window_end DESC LIMIT 100"
        )

    @app.get("/api/reports/{report_id}")
    async def report_detail(report_id: str) -> dict[str, Any]:
        rows = store.rows("SELECT * FROM reports WHERE id=?", (report_id,))
        if not rows:
            raise HTTPException(404, "Report not found")
        return {**rows[0], "data": json.loads(rows[0]["data"])}

    @app.post("/api/reports")
    async def generate_report(request: Request) -> dict[str, Any]:
        try:
            options = obj(await request.json())
            start, end = nightly_window(config)
            return report(
                store,
                config,
                registry,
                str(options.get("start", start)),
                str(options.get("end", end)),
                enqueue=False,
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "Invalid report window") from exc

    static = Path(__file__).parent / "static"
    if (static / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/")
    async def index() -> Response:
        if (static / "index.html").exists():
            return FileResponse(static / "index.html")
        return JSONResponse(
            {"detail": "Dashboard not built. Run npm ci && npm run build in frontend/."},
            status_code=503,
        )

    return app


def create_otlp(store: Store, registry: dict[str, Adapter]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalOnlyMiddleware)

    @app.post("/v1/{signal}")
    async def receive(signal: str, request: Request) -> Response:
        if signal not in {"logs", "metrics", "traces"}:
            raise HTTPException(404)
        media = request.headers.get("content-type", "").split(";")[0]
        if media not in {"application/json", "application/x-protobuf"}:
            raise HTTPException(415, "Expected OTLP JSON or protobuf")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8 * 1024 * 1024:
                raise HTTPException(413, "Request exceeds 8 MiB")
        encoding = request.headers.get("content-encoding", "identity")
        if encoding not in {"gzip", "identity"}:
            raise HTTPException(415, "Unsupported content encoding")
        try:
            raw = bytes(body)
            if encoding == "gzip":
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw = decompressor.decompress(raw, 16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024 or decompressor.unconsumed_tail:
                    raise HTTPException(413, "Decoded request exceeds 16 MiB")
                if not decompressor.eof:
                    raise ValueError("Incomplete gzip")
            if signal != "traces":
                document = decode(raw, signal, media == "application/x-protobuf")
                ingest_otlp(store, registry, document, signal)
        except (ValueError, TypeError, DecodeError, zlib.error) as exc:
            raise HTTPException(400, "Malformed OTLP request") from exc
        return Response(b"" if media == "application/x-protobuf" else b"{}", media_type=media)

    return app
