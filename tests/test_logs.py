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
    app.config["ADMIN_NAMESPACES"] = []  # 관리자 전용 네임스페이스를 끈 경우(인증을 끈 테스트는 모두 관리자다)
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


# --------------------------------------------------------------------------- #
# Pod 상태 확인 — 스트림을 열기 전에 "접속해도 되는가" 를 서버가 판단한다
# --------------------------------------------------------------------------- #
def _status(client, query):
    return client.get(f"/api/v1/logs/pod-status?{query}")


def test_pod_status_requires_namespace_and_pod(client):
    assert _status(client, "namespace=oncloud-ai-devops-service").status_code == 400
    assert _status(client, "pod=p").status_code == 400


def test_pod_status_refuses_namespaces_outside_the_allowlist(client):
    # 관리자 전용 네임스페이스를 끄면(ADMIN_NAMESPACES 비움) 허용 목록 밖은 누구에게나 닫힌다.
    client.application.config["ADMIN_NAMESPACES"] = []
    res = _status(client, "namespace=kube-system&pod=etcd-0")
    assert res.status_code == 403
    assert res.get_json()["code"] == "FORBIDDEN"
    assert "kube-system" not in res.get_json()["message"]


def _auth_client(role_ids):
    import datetime as dt

    import jwt

    from app import create_app
    from app.config import TestConfig

    class _AuthConfig(TestConfig):
        AUTH_DISABLED = False
        JWT_SECRET = "test-secret"

    client = create_app(_AuthConfig()).test_client()
    token = jwt.encode(
        {"sub": "u1", "roleIds": role_ids, "exp": dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)},
        "test-secret",
        algorithm="HS256",
    )
    return client, {"Authorization": f"Bearer {token}"}


def test_admin_only_namespace_tells_users_it_is_for_admins():
    # 허용 목록 밖(kube-system)은 관리자 전용 — 일반 사용자에게는 "관리자 전용 POD 입니다."
    client, headers = _auth_client(["seed-developer"])
    res = client.get("/api/v1/logs/pod-status?namespace=kube-system&pod=metrics-server-0", headers=headers)
    assert res.status_code == 403
    assert res.get_json() == {"code": "ADMIN_ONLY", "message": "관리자 전용 POD 입니다."}


def test_admin_passes_admin_only_namespace():
    # 관리자는 통과한다 — 여기서는 클러스터 밖이라 502(접근 판정은 넘어섰다).
    client, headers = _auth_client(["seed-admin"])
    res = client.get("/api/v1/logs/pod-status?namespace=kube-system&pod=metrics-server-0", headers=headers)
    assert res.status_code == 502


def test_pod_status_outside_cluster_is_502(client):
    res = _status(client, "namespace=oncloud-ai-devops-service&pod=p")
    assert res.status_code == 502


def test_missing_pod_is_not_available(client, monkeypatch):
    """사라진 Pod 는 exists/available 이 false — 화면은 상태 정보만 그린다."""
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    monkeypatch.setattr(logs, "pod_status", lambda ns, pod, **_kw: None)
    body = _status(client, "namespace=oncloud-ai-devops-service&pod=gone").get_json()
    assert body["exists"] is False
    assert body["available"] is False
    assert body["message"]


def test_running_pod_is_available(client, monkeypatch):
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    monkeypatch.setattr(
        logs,
        "pod_status",
        lambda ns, pod, **_kw: {
            "phase": "Running", "ready": True,
            "startedAt": "2026-07-30T01:00:00Z", "reason": "",
        },
    )
    body = _status(client, "namespace=oncloud-ai-devops-service&pod=web-1").get_json()
    assert body["exists"] is True and body["available"] is True
    assert body["phase"] == "Running" and body["ready"] is True


def test_terminated_pod_still_offers_logs(client, monkeypatch):
    """Succeeded/Failed 라도 오브젝트가 남아 있으면 로그는 읽을 수 있다."""
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    monkeypatch.setattr(
        logs,
        "pod_status",
        lambda ns, pod, **_kw: {
            "phase": "Failed", "ready": False,
            "startedAt": "2026-07-30T01:00:00Z", "reason": "Error",
        },
    )
    body = _status(client, "namespace=oncloud-ai-devops-service&pod=done").get_json()
    assert body["available"] is True
    assert body["message"] == "Error"


def test_allowed_namespaces_require_login():
    """허용 네임스페이스 목록도 로그인한 사용자에게만 — 토큰 없거나 무효면 401."""
    import time

    import jwt

    from app import create_app
    from app.config import TestConfig

    class _AuthConfig(TestConfig):
        AUTH_DISABLED = False
        JWT_SECRET = "test-secret"

    client = create_app(_AuthConfig()).test_client()
    assert client.get("/api/v1/logs/namespaces").status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert client.get("/api/v1/logs/namespaces", headers=bad).status_code == 401

    token = jwt.encode({"sub": "u-1", "exp": int(time.time()) + 60}, "test-secret", algorithm="HS256")
    res = client.get("/api/v1/logs/namespaces", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert "oncloud-ai-devops-service" in res.get_json()["items"]
