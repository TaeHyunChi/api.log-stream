"""스팬의 요청 · 응답 본문(otel.py, 2026-10-21) — 가림 · 자름 · 형식 · 스트리밍 제외."""

import json

from flask import Response

from app import otel


class FakeSpan:
    def __init__(self):
        self.attrs = {}

    def is_recording(self):
        return True

    def set_attribute(self, key, value):
        self.attrs[key] = value


def test_mask_body_masks_secrets_and_pii_and_truncates(monkeypatch):
    body = json.dumps(
        {
            "user": {"email": "kim@example.com", "password": "pw!", "accessToken": "abc"},
            "phone": "010-1234-5678",
            "note": "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlMTIz 끝",
            "apiKey": "",
        }
    )
    out = otel.mask_body(body)
    data = json.loads(out)
    assert data["user"]["password"] == "****" and data["user"]["accessToken"] == "****"
    assert data["user"]["email"] == "[이메일]" and data["phone"] == "[전화번호]"
    assert "[토큰]" in data["note"] and data["apiKey"] == ""  # 빈 값은 그대로
    monkeypatch.setenv("OTEL_BODY_MAX_CHARS", "200")
    long = otel.mask_body("가" * 500)
    assert long.startswith("가" * 200) and long.endswith("(+300자)")
    assert otel.mask_body("plain text, no json") == "plain text, no json"


def test_record_bodies_only_text_and_not_streams(app):
    with app.test_request_context(
        "/x", method="POST", data='{"q": "hi", "secret": "s"}', content_type="application/json"
    ):
        span = FakeSpan()
        otel._record_bodies(span, Response('{"ok": true}', mimetype="application/json"))
        assert json.loads(span.attrs["http.request.body"]) == {"q": "hi", "secret": "****"}
        assert span.attrs["http.response.body"] == '{"ok": true}'

        span = FakeSpan()
        otel._record_bodies(span, Response(iter(["data: 1\n\n"]), mimetype="text/event-stream"))
        assert "http.response.body" not in span.attrs

    with app.test_request_context(
        "/x", method="POST", data=b"\x00\x01", content_type="application/octet-stream"
    ):
        span = FakeSpan()
        otel._record_bodies(span, Response(b"\x89PNG", mimetype="image/png"))
        assert span.attrs == {}


def test_client_request_hook_and_switch(monkeypatch):
    import urllib.request

    req = urllib.request.Request(
        "http://x", data=b'{"password": "p", "q": 1}', headers={"Content-Type": "application/json"}
    )
    span = FakeSpan()
    otel._client_request_hook(span, req)
    assert json.loads(span.attrs["http.request.body"]) == {"password": "****", "q": 1}
    monkeypatch.setenv("OTEL_CAPTURE_BODIES", "false")
    span = FakeSpan()
    otel._client_request_hook(span, req)
    assert span.attrs == {}
