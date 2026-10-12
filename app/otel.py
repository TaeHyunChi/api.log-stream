"""OpenTelemetry 트레이스 — OTLP(HTTP)로 Alloy 에 보내 Tempo 에 쌓는다(운영 매뉴얼 5.2).

`OTEL_EXPORTER_OTLP_ENDPOINT`(예: http://alloy.oncloud-ai-monitoring.svc.cluster.local:4318)가
비어 있으면 아무것도 하지 않는다 — 로컬 실행과 테스트는 계측 없이 돈다.

- 들어오는 요청(Flask)과 나가는 호출(urllib · requests · SQLAlchemy)을 스팬으로 남기고,
  나가는 호출에 W3C `traceparent` 를 실어 다음 서비스가 같은 트레이스를 잇는다.
  Kong 의 opentelemetry 플러그인이 루트 스팬을 만들어 넘겨준다.
- 로그(JSON)의 `trace_id` · `span_id` 는 logging_config 가 현재 스팬에서 채운다.
- 응답에 `X-Trace-Id` 를 붙인다 — 요청이 끝난 뒤 찍히는 gunicorn 접근 로그는 스팬 밖이라
  그 헤더(와 `X-Request-ID`)에서 꺼낸다(access_log_ids).
- gunicorn 은 preload 없이 워커마다 create_app 을 부르므로, 배치 내보내기 스레드는 워커 안에서 생긴다.
- 표본(RequestRootSampler): 부모 트레이스가 있으면 sampled 플래그와 상관없이 기록하고, 부모가 없으면
  들어온 요청(SERVER 스팬)만 기록한다. Kong 3.4 는 자기가 시작한 트레이스를 sampled=0 으로 넘겨서
  플래그를 따르면 서비스 스팬이 모두 버려지고, 요청 밖(소비자 · 스레드 · 주기 작업)의 DB 쿼리는 하나하나가
  루트 트레이스로 쌓여 잡음이 된다. `OTEL_TRACES_SAMPLER` 를 주면 SDK 표준 표본기를 쓴다.

이 파일은 계측을 넣은 서비스들이 같은 내용으로 나눠 갖는다 — 고칠 때는 함께 고친다.
"""

import logging
import os

from flask import Flask

log = logging.getLogger(__name__)

# 헬스 체크·지표는 스팬을 만들지 않는다(초마다 들어와 트레이스를 덮는다).
EXCLUDED_URLS = "healthz,readyz,metrics"

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
        return response


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

    URLLibInstrumentor().instrument()
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
    except ImportError:
        RequestsInstrumentor = None
    if RequestsInstrumentor is not None:
        RequestsInstrumentor().instrument()
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        SQLAlchemyInstrumentor = None
    if SQLAlchemyInstrumentor is not None:
        SQLAlchemyInstrumentor().instrument()


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
