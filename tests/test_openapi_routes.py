"""명세-라우트 일치 — 코드의 API 라우트 집합 == openapi 명세 paths × methods.

비교 규칙:
- 코드: Flask `app.url_map` 의 규칙 중 API prefix(`/api/v1`)로 시작하는 것만 본다.
  prefix 밖(`/healthz`·`/readyz`·`/` 같은 프로브/정보 엔드포인트)과 정적 파일(`static`)은
  명세 대상이 아니라 제외한다. HEAD/OPTIONS 는 Flask 가 자동으로 붙이므로 제외한다.
- 경로 변수 `<conv:name>` 은 `{name}` 으로 바꿔 비교한다(이름까지 일치해야 한다).
- 명세: servers[0] 경로가 `/api/v1` 이고 paths 는 그 뒤의 상대 경로다.
"""

import re
from pathlib import Path

import yaml

SPEC = Path(__file__).resolve().parent.parent / "openapi" / "log-stream-service.yaml"
PREFIX = "/api/v1"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _code_routes(app) -> set[tuple[str, str]]:
    routes = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or not rule.rule.startswith(PREFIX):
            continue
        path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule.rule[len(PREFIX) :]) or "/"
        for method in (rule.methods or set()) & METHODS:
            routes.add((method, path))
    return routes


def _spec_routes() -> set[tuple[str, str]]:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert spec["servers"][0]["url"].rstrip("/").endswith(PREFIX)
    return {
        (method.upper(), path)
        for path, item in (spec.get("paths") or {}).items()
        for method in item
        if method.upper() in METHODS
    }


def test_openapi_spec_matches_code_routes(app):
    code = _code_routes(app)
    spec = _spec_routes()
    assert sorted(code - spec) == [], "코드에는 있지만 명세에 없는 경로"
    assert sorted(spec - code) == [], "명세에는 있지만 코드에 없는 경로"
