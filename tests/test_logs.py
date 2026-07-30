"""Pod 로그 실시간 중계.

검증하는 것.

1. 허용한 네임스페이스만 읽는다 — 네임스페이스를 파라미터로 받기 때문이다.
2. `since`(마지막 수신 시각)를 주면 그 뒤부터 이어 받는다 — 재접속 시 중복 방지.
3. `timestamps=true` 로 받은 줄을 (시각, 본문)으로 나눠 준다. 시각을 그대로
   돌려줘야 화면이 다음 접속의 `since` 로 쓸 수 있다.
"""

import pytest

from app import kube
from app.api.v1 import logs


def test_allowed_namespaces_are_listed(client):
    body = client.get("/api/v1/logs/namespaces").get_json()
    assert "oncloud-ai-devops-service" in body["items"]


def test_healthz(client):
    assert client.get("/healthz").get_json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# 파라미터 검증
# --------------------------------------------------------------------------- #
def _params(app, query: str):
    with app.test_request_context(f"/api/v1/logs/stream?{query}"):
        return logs._params()


def test_namespace_and_pod_are_required(app):
    value, error = _params(app, "namespace=oncloud-ai-devops-service")
    assert value is None and "필수" in error


def test_namespace_outside_the_allowlist_is_refused(app):
    """RBAC 으로도 막히지만 서버가 먼저 거절해 이유를 분명히 알려 준다."""
    value, error = _params(app, "namespace=kube-system&pod=etcd-0")
    assert value is None
    # 어떤 네임스페이스가 있는지는 알려 주지 않는다.
    assert "kube-system" not in error


def test_tail_is_clamped(app):
    value, _ = _params(app, "namespace=oncloud-ai-devops-service&pod=p&tail=999999")
    assert value["tail"] == 5000


def test_last_received_time_is_taken(app):
    """화면이 마지막으로 받은 시각을 주면 그 뒤부터 이어 받는다."""
    value, _ = _params(
        app, "namespace=oncloud-ai-devops-service&pod=p&since=2026-07-30T10:42:28Z"
    )
    assert value["since"] == "2026-07-30T10:42:28Z"


# --------------------------------------------------------------------------- #
# k8s 로그 URL
# --------------------------------------------------------------------------- #
def test_stream_url_follows_and_keeps_timestamps(app):
    with app.app_context():
        url = kube.log_url("oncloud-ai-devops-service", "dev-svc-abc")
    assert "follow=true" in url and "timestamps=true" in url
    assert "/namespaces/oncloud-ai-devops-service/pods/dev-svc-abc/log" in url


def test_since_wins_over_tail(app):
    """k8s 는 sinceTime 과 tailLines 를 함께 주면 tailLines 를 무시한다.

    둘 다 붙이면 "이어 받기" 인지 "처음 N줄" 인지 읽는 사람이 알 수 없으므로
    하나만 붙인다.
    """
    with app.app_context():
        url = kube.log_url("ns", "p", since="2026-07-30T10:00:00Z", tail=100)
    assert "sinceTime=" in url and "tailLines" not in url


def test_tail_is_used_when_there_is_no_since(app):
    with app.app_context():
        url = kube.log_url("ns", "p", tail=100)
    assert "tailLines=100" in url and "sinceTime" not in url


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2026-07-30T10:42:28.06Z GET / 200", ("2026-07-30T10:42:28.06Z", "GET / 200")),
        # 시각 없이 오는 줄(러너가 붙이지 않은 경우)은 본문으로 둔다.
        ("no timestamp here", ("", "no timestamp here")),
    ],
)
def test_timestamp_is_split_off(line, expected):
    assert kube.split_timestamp(line) == expected


def test_read_timeout_is_unset_after_connecting():
    """연결 뒤에도 타임아웃이 걸려 있으면 조용한 Pod 의 스트림이 그때마다 끊긴다.

    실제로 그랬다 — 첫 줄들을 받은 뒤 10초 만에 "스트림이 끊겼습니다" 가 떴다.
    """

    class _Sock:
        def __init__(self):
            self.timeout = 10

        def settimeout(self, value):
            self.timeout = value

    # urllib 의 실제 구조: response.fp(BufferedReader).raw(SocketIO)._sock
    class _Raw:
        def __init__(self, sock):
            self._sock = sock

    class _Fp:
        def __init__(self, sock):
            self.raw = _Raw(sock)

    class _Response:
        def __init__(self, sock):
            self.fp = _Fp(sock)

    sock = _Sock()
    kube._unset_read_timeout(_Response(sock))
    assert sock.timeout is None


def test_unset_read_timeout_tolerates_a_missing_socket():
    """소켓을 못 찾아도 죽지 않는다 — 끊김이 보일 뿐 중계는 계속돼야 한다."""
    kube._unset_read_timeout(object())
