"""Tempo(LGTM 트레이스 저장소) 조회 — 트랜잭션 검색 · 트레이스 상세(0.41.0).

    search(TraceQL, 기간)  → 트레이스 요약 목록(루트 서비스 · 이름 · 시작 · 소요 시간)
    get_trace(trace_id)    → 스팬 목록(부모 · 서비스 · 이름 · 시작 오프셋 · 소요 시간 · 상태 · 주요 속성)

Tempo 는 클러스터 안(oncloud-ai-monitoring)에만 열려 있어 브라우저가 직접 부를 수 없다 — 서비스가 대신 읽고,
누가 무엇을 볼 수 있는지는 부르는 쪽(엔드포인트)이 정한다. 같은 모듈의 사본이 api.agent 에 있다
(서비스별 사본 관례 — 한쪽을 고치면 다른 쪽도). 이 사본에는 서비스 이름 목록(service_names)이 더 있다.

속성은 화면에 쓸 것만 골라 자른다(값 300자). SQL 은 바인딩 변수(%s)로 남아 값이 실리지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_SPANS = 2000
MAX_ATTR_CHARS = 300
#: 화면에 싣는 속성 — 나머지(네트워크 · 라이브러리 내부)는 소음이다.
ATTR_KEYS = (
    "http.method",
    "http.request.method",
    "http.route",
    "http.target",
    "url.path",
    "url.full",
    "http.url",
    "http.status_code",
    "http.response.status_code",
    "db.system",
    "db.operation",
    "db.statement",
    "net.peer.name",
    "server.address",
    "rpc.method",
    "error.type",
    "exception.type",
    "exception.message",
)
KIND = {
    "SPAN_KIND_SERVER": "server",
    "SPAN_KIND_CLIENT": "client",
    "SPAN_KIND_INTERNAL": "internal",
    "SPAN_KIND_PRODUCER": "producer",
    "SPAN_KIND_CONSUMER": "consumer",
}


class TempoError(Exception):
    """Tempo 에 닿지 못했거나 응답이 이상하다 — 화면에 그대로 보여 줄 문장."""


def _base() -> str:
    base = (current_app.config.get("TEMPO_URL") or "").rstrip("/")
    if not base:
        raise TempoError("트레이스 저장소(TEMPO_URL)가 설정되지 않았습니다.")
    return base


def _get(path: str, params: dict | None = None) -> dict | None:
    url = f"{_base()}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - 설정 주소
    timeout = float(current_app.config.get("TEMPO_TIMEOUT", 10) or 10)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310
            return json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read(300).decode("utf-8", "replace")
        raise TempoError(f"트레이스 저장소 응답 {exc.code}: {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise TempoError(f"트레이스 저장소에 연결할 수 없습니다: {exc}") from None


def _iso_from_nanos(nanos) -> str | None:
    try:
        value = int(nanos)
    except (TypeError, ValueError):
        return None
    moment = dt.datetime.fromtimestamp(value / 1e9, tz=dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _attr_value(value: dict):
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            raw = value[key]
            return int(raw) if key == "intValue" else raw
    return None


def search(query: str, *, start: dt.datetime, end: dt.datetime, limit: int = 50) -> list[dict]:
    """TraceQL 검색 — 최신순 요약 목록."""
    body = (
        _get(
            "/api/search",
            {
                "q": query,
                "start": int(start.replace(tzinfo=dt.UTC).timestamp()),
                "end": int(end.replace(tzinfo=dt.UTC).timestamp()) + 1,
                "limit": max(1, min(limit, 200)),
                "spss": 3,
            },
        )
        or {}
    )
    out = []
    for item in body.get("traces") or []:
        if not isinstance(item, dict) or not item.get("traceID"):
            continue
        out.append(
            {
                "traceId": str(item["traceID"]).rjust(32, "0"),
                "rootService": item.get("rootServiceName") or None,
                "rootName": item.get("rootTraceName") or None,
                "start": _iso_from_nanos(item.get("startTimeUnixNano")),
                "durationMs": int(item.get("durationMs") or 0),
            }
        )
    out.sort(key=lambda t: t.get("start") or "", reverse=True)
    return out


def service_names() -> list[str]:
    """트레이스를 보낸 서비스 이름 — 검색 화면의 서비스 선택지."""
    body = _get("/api/v2/search/tag/resource.service.name/values") or {}
    values = body.get("tagValues") or body.get("values") or []
    names = {str(v.get("value") if isinstance(v, dict) else v) for v in values}
    return sorted(n for n in names if n and n != "None")


def get_trace(trace_id: str) -> dict | None:
    """트레이스 하나 — 스팬을 시작 순으로. 없으면 None(보존 기간이 지났거나 아직 안 들어옴)."""
    if not TRACE_ID.match(trace_id or ""):
        raise TempoError("트레이스 ID 는 32자리 16진수입니다.")
    body = _get(f"/api/v2/traces/{trace_id}")
    if body is None:
        return None
    batches = (body.get("trace") or {}).get("resourceSpans") or body.get("batches") or []
    spans: list[dict] = []
    for batch in batches:
        resource = {
            a.get("key"): _attr_value(a.get("value") or {})
            for a in (batch.get("resource") or {}).get("attributes") or []
        }
        service = str(resource.get("service.name") or "unknown")
        for scope in batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []:
            for span in scope.get("spans") or []:
                if len(spans) >= MAX_SPANS:
                    break
                spans.append(_span(span, service))
    if not spans:
        return None
    spans.sort(key=lambda s: s["_start"])
    origin = spans[0]["_start"]
    end = max(s["_start"] + s["_duration"] for s in spans)
    for span in spans:
        span["offsetMs"] = round((span.pop("_start") - origin) / 1e6, 3)
        span["durationMs"] = round(span.pop("_duration") / 1e6, 3)
    return {
        "traceId": trace_id,
        "start": _iso_from_nanos(origin),
        "durationMs": round((end - origin) / 1e6, 3),
        "services": sorted({s["service"] for s in spans}),
        "errorCount": sum(1 for s in spans if s["status"] == "error"),
        "spans": spans,
        "truncated": len(spans) >= MAX_SPANS,
    }


def _span(span: dict, service: str) -> dict:
    start = int(span.get("startTimeUnixNano") or 0)
    end = int(span.get("endTimeUnixNano") or start)
    attrs = {}
    for attr in span.get("attributes") or []:
        key = attr.get("key")
        if key in ATTR_KEYS:
            value = _attr_value(attr.get("value") or {})
            attrs[key] = str(value)[:MAX_ATTR_CHARS] if isinstance(value, str) else value
    code = str((span.get("status") or {}).get("code") or "")
    status_code = attrs.get("http.status_code") or attrs.get("http.response.status_code")
    error = code in ("STATUS_CODE_ERROR", "2") or (isinstance(status_code, int) and status_code >= 500)
    return {
        "spanId": _hex(span.get("spanId")),
        "parentSpanId": _hex(span.get("parentSpanId")) or None,
        "name": str(span.get("name") or "")[:200],
        "service": service,
        "kind": KIND.get(str(span.get("kind") or ""), "internal"),
        "status": "error" if error else "ok",
        "attributes": attrs,
        "_start": start,
        "_duration": max(0, end - start),
    }


def _hex(value) -> str:
    """Tempo v2 는 ID 를 base64 로 준다(OTLP JSON). 16진수면 그대로."""
    if not value:
        return ""
    text = str(value)
    if re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", text):
        return text
    import base64

    try:
        return base64.b64decode(text).hex()
    except (ValueError, TypeError):
        return text


def grafana_explore_url(trace_id: str) -> str | None:
    """관리자용 Grafana Explore 링크 — GRAFANA_URL 이 있을 때만(내부망 주소일 수 있다)."""
    base = (current_app.config.get("GRAFANA_URL") or "").rstrip("/")
    if not base:
        return None
    pane = {
        "t": {
            "datasource": current_app.config.get("GRAFANA_TEMPO_UID") or "tempo",
            "queries": [{"refId": "A", "queryType": "traceql", "query": trace_id}],
            "range": {"from": "now-7d", "to": "now"},
        }
    }
    return (
        f"{base}/explore?schemaVersion=1&panes={urllib.parse.quote(json.dumps(pane, separators=(',', ':')))}"
    )
