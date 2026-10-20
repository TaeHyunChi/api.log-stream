"""트랜잭션 검색(0.5.0) — 서비스 관리 > 트랜잭션 검색 화면의 백엔드. **관리자 전용.**

    GET /traces/services                 트레이스를 보낸 서비스 이름
    GET /traces/search?…                 TraceQL 검색(서비스 · 기간 · 최소 소요 시간 · 오류만 · 주소 ·
                                         트레이스 ID)
    GET /traces/{traceId}                트레이스 상세(스팬) + 관리자 Grafana 링크

트레이스에는 누가 요청했는지가 남지 않아(스팬에 사용자 속성 없음) 사용자별로 범위를 나눌 수 없다 —
그래서 관리자만.
에이전트 소유자는 에이전트 운영 > 로그 탭의 [트랜잭션 검색](agent-service, 자기 대화만)을 쓴다.
"""

from __future__ import annotations

import datetime as dt
import re

from flask import Blueprint, request

from ... import tempo
from ...auth import claims_from_request, is_admin

bp = Blueprint("traces", __name__, url_prefix="/traces")

DEFAULT_HOURS = 1
MAX_RANGE_DAYS = 14
MAX_LIMIT = 100
_SERVICE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")


def _deny():
    claims = claims_from_request()
    if claims is None:
        return {"code": "UNAUTHORIZED", "message": "인증이 필요합니다."}, 401
    if not is_admin(claims):
        return {"code": "FORBIDDEN", "message": "트랜잭션 검색은 관리자만 쓸 수 있습니다."}, 403
    return None


def _bad(message: str):
    return {"code": "INVALID_PARAMETER", "message": message}, 400


def _time(name: str) -> dt.datetime | None:
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value.astimezone(dt.UTC).replace(tzinfo=None) if value.tzinfo else value


def _quote(text: str) -> str:
    """TraceQL 문자열 안에 넣을 정규식 — 정규식 특수문자와 따옴표 · 역슬래시를 이스케이프."""
    return re.escape(text).replace("\\", "\\\\").replace('"', '\\"')


def build_query(*, service: str, min_ms: int | None, errors: bool, path: str) -> str:
    parts = []
    if service:
        parts.append(f'resource.service.name = "{service}"')
    if errors:
        parts.append("status = error")
    if min_ms:
        parts.append(f"duration >= {min_ms}ms")
    if path:
        parts.append(f'span.http.target =~ ".*{_quote(path)}.*"')
    return "{ " + " && ".join(parts) + " }" if parts else "{}"


@bp.get("/services")
def list_services():
    if (denied := _deny()) is not None:
        return denied
    try:
        return {"items": tempo.service_names()}
    except tempo.TempoError as exc:
        return {"code": "TEMPO_UNAVAILABLE", "message": str(exc)}, 502


@bp.get("/search")
def search_traces():
    if (denied := _deny()) is not None:
        return denied
    trace_id = (request.args.get("traceId") or "").strip().lower()
    if trace_id:
        if not tempo.TRACE_ID.match(trace_id):
            return _bad("traceId 는 32자리 16진수입니다.")
        try:
            trace = tempo.get_trace(trace_id)
        except tempo.TempoError as exc:
            return {"code": "TEMPO_UNAVAILABLE", "message": str(exc)}, 502
        if trace is None:
            return {"items": [], "query": None}
        root = trace["spans"][0]
        return {
            "items": [
                {
                    "traceId": trace_id,
                    "rootService": root["service"],
                    "rootName": root["name"],
                    "start": trace["start"],
                    "durationMs": trace["durationMs"],
                }
            ],
            "query": None,
        }
    try:
        end = _time("to") or dt.datetime.now(dt.UTC).replace(tzinfo=None)
        start = _time("from") or end - dt.timedelta(hours=DEFAULT_HOURS)
    except ValueError:
        return _bad("from · to 는 ISO 8601 시각입니다.")
    if start >= end:
        return _bad("from 이 to 보다 앞이어야 합니다.")
    if end - start > dt.timedelta(days=MAX_RANGE_DAYS):
        return _bad(f"기간은 {MAX_RANGE_DAYS}일까지입니다.")
    service = (request.args.get("service") or "").strip()
    if service and not _SERVICE.match(service):
        return _bad("service 이름이 올바르지 않습니다.")
    path = (request.args.get("path") or "").strip()[:100]
    if any(ord(ch) < 32 for ch in path):
        return _bad("path 에 제어 문자를 쓸 수 없습니다.")
    try:
        min_ms = int(request.args.get("minDurationMs") or 0) or None
        limit = min(MAX_LIMIT, max(1, int(request.args.get("limit") or 50)))
    except ValueError:
        return _bad("minDurationMs · limit 는 정수입니다.")
    errors = (request.args.get("status") or "").strip() == "error"
    query = build_query(service=service, min_ms=min_ms, errors=errors, path=path)
    try:
        items = tempo.search(query, start=start, end=end, limit=limit)
    except tempo.TempoError as exc:
        return {"code": "TEMPO_UNAVAILABLE", "message": str(exc)}, 502
    return {"items": items, "query": query}


@bp.get("/<trace_id>")
def get_trace(trace_id: str):
    if (denied := _deny()) is not None:
        return denied
    trace_id = (trace_id or "").lower()
    if not tempo.TRACE_ID.match(trace_id):
        return _bad("트레이스 ID 는 32자리 16진수입니다.")
    try:
        trace = tempo.get_trace(trace_id)
    except tempo.TempoError as exc:
        return {"code": "TEMPO_UNAVAILABLE", "message": str(exc)}, 502
    if trace is None:
        return {
            "code": "TRACE_NOT_FOUND",
            "message": "트레이스를 찾을 수 없습니다(보존 기간이 지났거나 아직 저장되지 않았습니다).",
        }, 404
    trace["grafanaUrl"] = tempo.grafana_explore_url(trace_id)
    return trace
