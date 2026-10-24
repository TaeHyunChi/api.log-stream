"""OpenTelemetry 트레이스 — OTLP(HTTP)로 Alloy 에 보내 Tempo 에 쌓는다(운영 매뉴얼 5.2).

`OTEL_EXPORTER_OTLP_ENDPOINT`(예: http://alloy.oncloud-ai-monitoring.svc.cluster.local:4318)가
비어 있으면 아무것도 하지 않는다 — 로컬 실행과 테스트는 계측 없이 돈다.

- 들어오는 요청(Flask)과 나가는 호출(urllib · requests · SQLAlchemy)을 스팬으로 남기고,
  나가는 호출에 W3C `traceparent` 를 실어 다음 서비스가 같은 트레이스를 잇는다.
  Kong 의 opentelemetry 플러그인이 루트 스팬을 만들어 넘겨준다.
- 로그(JSON)의 `trace_id` · `span_id` 는 logging_config 가 현재 스팬에서 채운다.
- 응답에 `X-Trace-Id` 를 붙인다 — 요청이 끝난 뒤 찍히는 gunicorn 접근 로그는 스팬 밖이라
  그 헤더(와 `X-Request-ID`)에서 꺼낸다(access_log_ids).
- 요청 · 응답 본문(2026-10-21): 들어온 요청 스팬에 `http.request.body` · `http.response.body`, 나가는 호출
  스팬에 `http.request.body` 를 싣는다(트랜잭션 검색의 구간 상세 — 입력 · 출력 데이터). 글 · JSON · 폼만,
  스트리밍 응답과 파일은 싣지 않는다. 비밀로 보이는 키(password · token · key · cookie …)의 값과 JWT ·
  이메일 · 전화 · 주민번호 · 카드번호는 가리고 OTEL_BODY_MAX_CHARS(기본 4000)자로 자른다.
  나가는 호출의 응답 본문은 싣지 않는다 — 훅에서 읽으면 스트림을 소비한다(받는 서비스의 스팬에 남는다).
  `OTEL_CAPTURE_BODIES=false` 면 본문을 싣지 않는다. 토큰 개수 · 한도 키(max_tokens …)는 가리지 않는다.
- 주소 속성(http.target · http.url · url.full)의 비밀 쿼리 값(ctx · token · api_key · key · signature …)은
  본문 기록과 상관없이 늘 가린다.
- gunicorn 은 preload 없이 워커마다 create_app 을 부르므로, 배치 내보내기 스레드는 워커 안에서 생긴다.
- 표본(RequestRootSampler): 부모 트레이스가 있으면 sampled 플래그와 상관없이 기록하고, 부모가 없으면
  들어온 요청(SERVER 스팬)만 기록한다. Kong 3.4 는 자기가 시작한 트레이스를 sampled=0 으로 넘겨서
  플래그를 따르면 서비스 스팬이 모두 버려지고, 요청 밖(소비자 · 스레드 · 주기 작업)의 DB 쿼리는 하나하나가
  루트 트레이스로 쌓여 잡음이 된다. `OTEL_TRACES_SAMPLER` 를 주면 SDK 표준 표본기를 쓴다.

이 파일은 계측을 넣은 서비스들이 같은 내용으로 나눠 갖는다 — 고칠 때는 함께 고친다.
"""

import json
import logging
import os
import re

from flask import Flask, request

log = logging.getLogger(__name__)

# 헬스 체크·지표는 스팬을 만들지 않는다(초마다 들어와 트레이스를 덮는다).
EXCLUDED_URLS = "healthz,readyz,metrics"

# 요청 · 응답 본문 — 싣는 형식 · 길이 · 가림 규칙
BODY_TYPES = ("application/json", "application/x-www-form-urlencoded", "application/xml", "text/")
BODY_READ_LIMIT = 256 * 1024  # 이보다 큰 본문은 읽지 않는다(스팬에 실을 것은 앞부분뿐)
_SECRET_KEY = re.compile(
    r"pass(word|wd)?|secret|token|authorization|api[_-]?key|credential|cookie|private[_-]?key", re.I
)
# "token" 이 들어가도 비밀이 아닌 키 — 토큰 개수 · 한도(max_tokens, inputTokens, token_count …)
_NOT_SECRET_KEY = re.compile(r"tokens|token_?(count|limit|usage)|max_?token", re.I)
# 주소의 비밀 쿼리 파라미터 — 값을 가린다(도구 게이트웨이 호출 맥락 ctx, 토큰, 키, 서명)
_SECRET_QUERY = re.compile(
    r"([?&](?:ctx|token|access_token|refresh_token|id_token|api_?key|key|secret|signature|sig|code"
    r"|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&#\s]*",
    re.I,
)
# 본문 안의 서명된 주소(에이전트 첨부 Access URL `?exp=&sig=` · presigned URL) — 값만 가린다(2026-10-24).
_SIGNED_IN_BODY = re.compile(
    r"([?&](?:sig|signature|ctx|x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s\"'\\<]+",
    re.I,
)
_URL_ATTRS = ("http.target", "http.url", "url.full", "url.query")
_MASKS = (
    (re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}"), "[토큰]"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"\b\d{6}-?[1-4]\d{6}\b"), "[주민번호]"),
    (re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"), "[카드번호]"),
    (re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b"), "[전화번호]"),
)

_provider_ready = False


def init_tracing(app: Flask) -> None:
    global _provider_ready
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip() or app.testing:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # 이미지에 패키지가 없으면 계측 없이 뜬다.
        log.warning("opentelemetry 패키지가 없어 트레이스를 보내지 않습니다")
        return

    if not _provider_ready:
        # 이미지 태그 등 나머지 속성은 OTEL_RESOURCE_ATTRIBUTES 로 더한다(SDK 가 읽는다).
        name = os.getenv("OTEL_SERVICE_NAME") or app.config["SERVICE_NAME"]
        sampler = None if os.getenv("OTEL_TRACES_SAMPLER") else _request_root_sampler()
        provider = TracerProvider(resource=Resource.create({"service.name": name}), sampler=sampler)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _instrument_clients()
        _provider_ready = True

    FlaskInstrumentor().instrument_app(app, excluded_urls=EXCLUDED_URLS)

    @app.after_request
    def _trace_id_header(response):
        ids = current_trace_ids()
        if ids:
            response.headers.setdefault("X-Trace-Id", ids[0])
        try:
            _record_bodies(trace.get_current_span(), response)
        except Exception:  # noqa: BLE001 - 계측이 응답을 망가뜨리면 안 된다
            log.debug("본문을 스팬에 싣지 못했습니다", exc_info=True)
        return response


def _capture_bodies() -> bool:
    return os.getenv("OTEL_CAPTURE_BODIES", "true").strip().lower() != "false"


def _body_limit() -> int:
    try:
        return max(200, int(os.getenv("OTEL_BODY_MAX_CHARS", "4000")))
    except ValueError:
        return 4000


def _textual(content_type: str | None) -> bool:
    value = (content_type or "").lower()
    return any(t in value for t in BODY_TYPES) and "event-stream" not in value


def mask_body(text: str) -> str:
    """스팬에 실을 본문 — JSON 이면 비밀 키 값을 가리고, 어떤 형식이든 토큰 · 개인정보를 가린 뒤 자른다."""
    if not text:
        return ""
    limit = _body_limit()
    try:
        text = json.dumps(_mask_value(json.loads(text)), ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        pass
    for pattern, label in _MASKS:
        text = pattern.sub(label, text)
    text = _SIGNED_IN_BODY.sub(r"\1****", text)
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit}자)"


def _secret_key(key) -> bool:
    return isinstance(key, str) and bool(_SECRET_KEY.search(key)) and not _NOT_SECRET_KEY.search(key)


def mask_url(url: str) -> str:
    """주소의 비밀 쿼리 값을 가린다 — `?ctx=…&q=1` → `?ctx=****&q=1`."""
    return _SECRET_QUERY.sub(r"\1****", url) if url else url


def _mask_span_urls(span) -> None:
    """계측이 스팬 시작 때 실은 주소 속성에서 비밀 쿼리를 가린다(같은 키로 덮어쓴다)."""
    attrs = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRS:
        value = attrs.get(key)
        if isinstance(value, str) and "=" in value:
            masked = mask_url(value)
            if masked != value:
                span.set_attribute(key, masked)


def _mask_value(value, depth: int = 0):
    if depth > 20:
        return value
    if isinstance(value, dict):
        return {
            k: ("****" if _secret_key(k) and v not in (None, "") else _mask_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_value(v, depth + 1) for v in value]
    return value


def _record_bodies(span, response) -> None:
    if span is None or not span.is_recording():
        return
    _mask_span_urls(span)
    if not _capture_bodies():
        return
    length = request.content_length
    if length and length <= BODY_READ_LIMIT and _textual(request.content_type):
        body = request.get_data(cache=True, as_text=True)
        if body:
            span.set_attribute("http.request.body", mask_body(body))
    if (
        not response.is_streamed
        and not response.direct_passthrough
        and _textual(response.mimetype)
        and (response.content_length or 0) <= BODY_READ_LIMIT
    ):
        body = response.get_data(as_text=True)
        if body:
            span.set_attribute("http.response.body", mask_body(body))


def _request_root_sampler():
    from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
    from opentelemetry.trace import SpanKind, get_current_span

    class RequestRootSampler(Sampler):
        def should_sample(
            self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None
        ):
            parent = get_current_span(parent_context).get_span_context()
            if parent.is_valid:
                return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes, parent.trace_state)
            if kind == SpanKind.SERVER:
                return SamplingResult(Decision.RECORD_AND_SAMPLE, attributes)
            return SamplingResult(Decision.DROP)

        def get_description(self) -> str:
            return "RequestRootSampler"

    return RequestRootSampler()


def _instrument_clients() -> None:
    """설치된 클라이언트 라이브러리만 계측한다 — 서비스마다 쓰는 것이 다르다."""
    # 계측 패키지는 그 라이브러리를 쓰는 서비스에만 설치된다(requirements.txt) — 없으면 건너뛴다.
    # 라이브러리 자체(requests · sqlalchemy)나 importlib 를 직접 import 하지 않는다: 일부 서비스는
    # "앱 코드가 requests · importlib 를 import 하지 않는다" 같은 가드 테스트를 둔다.
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor

    URLLibInstrumentor().instrument(request_hook=_client_request_hook)
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
    except ImportError:
        RequestsInstrumentor = None
    if RequestsInstrumentor is not None:
        RequestsInstrumentor().instrument(request_hook=_requests_request_hook)
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        SQLAlchemyInstrumentor = None
    if SQLAlchemyInstrumentor is not None:
        SQLAlchemyInstrumentor().instrument()


def _client_request_hook(span, req) -> None:
    """urllib 나가는 호출 — 주소의 비밀 쿼리를 가리고, 보내는 본문(있으면). 응답 본문은 읽지 않는다."""
    if span is None or not span.is_recording():
        return
    _mask_span_urls(span)
    if not _capture_bodies():
        return
    data = getattr(req, "data", None)
    content_type = req.get_header("Content-type") if hasattr(req, "get_header") else None
    if (
        isinstance(data, bytes | str)
        and data
        and len(data) <= BODY_READ_LIMIT
        and _textual(content_type or "text/")
    ):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        span.set_attribute("http.request.body", mask_body(text))


def _requests_request_hook(span, prepared) -> None:
    """requests 나가는 호출 — 주소의 비밀 쿼리를 가리고, 보내는 본문(있으면)."""
    if span is None or not span.is_recording():
        return
    _mask_span_urls(span)
    if not _capture_bodies():
        return
    body = getattr(prepared, "body", None)
    content_type = (getattr(prepared, "headers", None) or {}).get("Content-Type")
    if (
        isinstance(body, bytes | str)
        and body
        and len(body) <= BODY_READ_LIMIT
        and _textual(content_type or "text/")
    ):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        span.set_attribute("http.request.body", mask_body(text))


def current_trace_ids() -> tuple[str, str] | None:
    """로그에 남길 (trace_id, span_id). 스팬 밖이거나 계측이 없으면 None."""
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def access_log_ids(record: logging.LogRecord, request_id: str) -> dict:
    """gunicorn 접근 로그 줄의 request_id · trace_id — 응답 헤더(X-Request-ID · X-Trace-Id)에서 꺼낸다.

    gunicorn 은 접근 로그 인자로 응답 헤더를 `{x-trace-id}o` 같은 키로 넘긴다. 다른 로그는 빈 dict.
    """
    args = record.args
    if record.name != "gunicorn.access" or not isinstance(args, dict):
        return {}
    out = {}
    rid = args.get("{x-request-id}o")
    if rid and request_id == "-":
        out["request_id"] = rid
    tid = args.get("{x-trace-id}o")
    if tid:
        out["trace_id"] = tid
    return out
