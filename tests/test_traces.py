"""트랜잭션 검색(0.5.0) — 관리자 전용 · TraceQL 조립 · Tempo 응답 정리. Tempo 는 가짜로 바꾼다."""

import datetime as dt

import jwt
import pytest

from app import tempo
from app.api.v1.traces import build_query

TID = "a" * 32


@pytest.fixture
def secured(app):
    app.config.update(AUTH_DISABLED=False, JWT_SECRET="test-secret")
    yield app
    app.config.update(AUTH_DISABLED=True)


def _auth(app, roles=None):
    claims = {"sub": "u1", "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)}
    if roles:
        claims["roleIds"] = roles
    return {"Authorization": "Bearer " + jwt.encode(claims, app.config["JWT_SECRET"], algorithm="HS256")}


def test_build_query_escapes_and_combines():
    assert build_query(service="", min_ms=None, errors=False, path="") == "{}"
    q = build_query(service="agent-service", min_ms=500, errors=True, path='/agents/a.b"c')
    assert q.startswith('{ resource.service.name = "agent-service" && status = error && duration >= 500ms')
    assert 'span.http.target =~ ".*/agents/a\\\\.b\\"c.*"' in q


def test_admin_only(secured, client):
    assert client.get("/api/v1/traces/search").status_code == 401
    assert client.get("/api/v1/traces/search", headers=_auth(secured)).status_code == 403


def test_search_detail_and_services(secured, client, monkeypatch):
    calls = []

    def fake_get(path, params=None):
        calls.append((path, params))
        if path == "/api/search":
            return {
                "traces": [
                    {
                        "traceID": "1" * 32,
                        "rootServiceName": "kong",
                        "rootTraceName": "GET /a",
                        "startTimeUnixNano": "1792529734000000000",
                        "durationMs": 12,
                    },
                    {
                        "traceID": "2" * 32,
                        "rootServiceName": "agent-service",
                        "rootTraceName": "POST /b",
                        "startTimeUnixNano": "1792529735000000000",
                        "durationMs": 340,
                    },
                ]
            }
        if path.startswith("/api/v2/search/tag"):
            return {
                "tagValues": [
                    {"type": "string", "value": "kong"},
                    {"type": "string", "value": "agent-service"},
                ]
            }
        return {
            "trace": {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [{"key": "service.name", "value": {"stringValue": "kong"}}]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "spanId": "AQIDBAUGBwg=",
                                        "name": "GET /a",
                                        "kind": "SPAN_KIND_SERVER",
                                        "startTimeUnixNano": "1000",
                                        "endTimeUnixNano": "2001000",
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr(tempo, "_get", fake_get)
    admin = _auth(secured, ["seed-admin"])
    body = client.get(
        "/api/v1/traces/search?service=kong&status=error&minDurationMs=10&from=2026-10-20T00:00:00Z&to=2026-10-20T01:00:00Z",
        headers=admin,
    ).get_json()
    assert [t["traceId"] for t in body["items"]] == ["2" * 32, "1" * 32]  # 최신순
    params = calls[-1][1]
    assert params["q"] == '{ resource.service.name = "kong" && status = error && duration >= 10ms }'
    assert params["end"] - params["start"] == 3601
    assert client.get("/api/v1/traces/services", headers=admin).get_json()["items"] == [
        "agent-service",
        "kong",
    ]
    detail = client.get(f"/api/v1/traces/{TID}", headers=admin).get_json()
    assert detail["spans"][0]["spanId"] == "0102030405060708" and detail["spans"][0]["durationMs"] == 2.0
    by_id = client.get(f"/api/v1/traces/search?traceId={TID}", headers=admin).get_json()
    assert by_id["items"][0]["rootService"] == "kong"
    bad = client.get("/api/v1/traces/search?service=a b", headers=admin)
    assert bad.status_code == 400
    too_long = client.get(
        "/api/v1/traces/search?from=2026-09-01T00:00:00Z&to=2026-10-20T00:00:00Z", headers=admin
    )
    assert too_long.status_code == 400

    def down(path, params=None):
        raise tempo.TempoError("트레이스 저장소에 연결할 수 없습니다")

    monkeypatch.setattr(tempo, "_get", down)
    assert client.get("/api/v1/traces/search", headers=admin).status_code == 502
