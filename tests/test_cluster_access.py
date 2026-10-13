"""다른 클러스터(클러스터 관리에 추가된 것)의 Pod 로그 — clusterId 해석·권한·허용 목록."""

import base64
import datetime as dt

import jwt
import pytest

from app import cluster_access
from app.api.v1 import logs

KUBECONFIG = """
apiVersion: v1
kind: Config
current-context: test
contexts:
- name: test
  context: {cluster: test, user: admin}
clusters:
- name: test
  cluster:
    server: https://10.74.89.47:6443
    certificate-authority-data: %s
users:
- name: admin
  user:
    token: remote-token
""".replace("%s", base64.b64encode(b"-----BEGIN CERTIFICATE-----\n").decode())


@pytest.fixture(autouse=True)
def _clear():
    cluster_access.clear_cache()
    yield
    cluster_access.clear_cache()


def _creds(server="https://10.74.89.47:6443", kubeconfig=KUBECONFIG):
    return {"clusterId": "c-remote", "apiServerUrl": server, "kubeconfig": kubeconfig}


def test_kubeconfig_becomes_remote_access(app, monkeypatch):
    monkeypatch.setattr(cluster_access, "_credentials", lambda cid: _creds())
    with app.app_context():
        access = cluster_access.resolve("c-remote")
    assert not access.local
    assert access.server == "https://10.74.89.47:6443"
    assert access.auth_headers() == {"Authorization": "Bearer remote-token"}


def test_no_cluster_id_and_local_ids_use_service_account(app):
    app.config["LOCAL_CLUSTER_IDS"] = ("prod-1",)
    with app.app_context():
        assert cluster_access.resolve("").local
        assert cluster_access.resolve(None).local
        assert cluster_access.resolve("prod-1").local


def test_platform_cluster_registered_by_node_address_is_local(app, monkeypatch):
    app.config["LOCAL_API_SERVER_URLS"] = ("https://192.168.201.50:6443",)
    monkeypatch.setattr(
        cluster_access, "_credentials", lambda cid: _creds(server="https://192.168.201.50:6443")
    )
    with app.app_context():
        access = cluster_access.resolve("prod")
    assert access.local and access.cluster_id == "prod"


def test_missing_kubeconfig_is_a_clear_error(app, monkeypatch):
    monkeypatch.setattr(cluster_access, "_credentials", lambda cid: _creds(kubeconfig=""))
    with app.app_context(), pytest.raises(cluster_access.ClusterAccessError, match="kubeconfig"):
        cluster_access.resolve("c-remote")


def test_remote_pod_status_skips_platform_allowlist(client, monkeypatch):
    """다른 클러스터는 이 플랫폼의 네임스페이스 허용 목록을 적용하지 않고 그 클러스터로 묻는다."""
    monkeypatch.setattr(cluster_access, "_credentials", lambda cid: _creds())
    seen = {}

    def fake_status(ns, pod, access=None):
        seen["server"] = access.server
        return {"phase": "Running", "ready": True, "startedAt": "", "reason": "", "containers": []}

    monkeypatch.setattr(logs, "pod_status", fake_status)
    res = client.get("/api/v1/logs/pod-status?namespace=kube-system&pod=coredns-1&clusterId=c-remote")
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["available"] is True
    assert seen["server"] == "https://10.74.89.47:6443"


def test_local_cluster_keeps_allowlist(client, monkeypatch):
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    client.application.config["ADMIN_NAMESPACES"] = []  # 인증을 끈 테스트는 관리자라 관리자 전용 목록을 끈다
    res = client.get("/api/v1/logs/pod-status?namespace=kube-system&pod=x")
    assert res.status_code == 403


def _token(app, **claims):
    now = dt.datetime.now(dt.UTC)
    return jwt.encode({"sub": "u1", "exp": now + dt.timedelta(minutes=5), **claims}, "s3cret", algorithm="HS256")


def test_remote_cluster_requires_admin_or_cluster_permission(app, client, monkeypatch):
    app.config.update(AUTH_DISABLED=False, JWT_SECRET="s3cret")
    monkeypatch.setattr(cluster_access, "_credentials", lambda cid: _creds())
    monkeypatch.setattr(logs, "pod_status", lambda ns, pod, **_kw: None)
    url = "/api/v1/logs/pod-status?namespace=default&pod=p&clusterId=c-remote"

    plain = {"Authorization": f"Bearer {_token(app, roleIds=['seed-user'])}"}
    assert client.get(url, headers=plain).status_code == 403

    admin = {"Authorization": f"Bearer {_token(app, roleIds=['seed-admin'])}"}
    assert client.get(url, headers=admin).status_code == 200

    manager = {"Authorization": f"Bearer {_token(app, roleIds=['r'], permissions=['k8s-cluster.resource.manage'])}"}
    assert client.get(url, headers=manager).status_code == 200


# --- 네임스페이스 권한 설정(로그 조회, 클러스터별 — volume-service 0.7.0) -------------------------------
@pytest.fixture
def grants(app, monkeypatch):
    from app import granted

    granted.reset()
    app.config.update(AUTH_DISABLED=False, JWT_SECRET="s3cret", VOLUME_SERVICE_URL="http://volume/api/v1")
    table = {"": ["team-a"], "c-remote": ["team-x"]}
    calls = []

    def fake_fetch(key):
        calls.append(key)
        return list(table.get(key, []))

    monkeypatch.setattr(granted, "_fetch", fake_fetch)
    yield table, calls
    granted.reset()


def test_remote_grant_opens_namespace_for_plain_users(app, client, grants, monkeypatch):
    monkeypatch.setattr(cluster_access, "_credentials", lambda cid: _creds())
    monkeypatch.setattr(logs, "pod_status", lambda ns, pod, **_kw: None)
    plain = {"Authorization": f"Bearer {_token(app, roleIds=['seed-user'])}"}
    ok = client.get("/api/v1/logs/pod-status?namespace=team-x&pod=p&clusterId=c-remote", headers=plain)
    assert ok.status_code == 200, ok.get_json()
    # 다른 클러스터의 권한은 그 클러스터에만 — 같은 이름이라도 플랫폼 클러스터 행(team-a)은 열지 않는다.
    no = client.get("/api/v1/logs/pod-status?namespace=team-a&pod=p&clusterId=c-remote", headers=plain)
    assert no.status_code == 403
    listed = client.get("/api/v1/logs/namespaces?clusterId=c-remote", headers=plain).get_json()["items"]
    assert listed == ["team-x"]


def test_local_grant_adds_to_allowlist(app, client, grants, monkeypatch):
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    monkeypatch.setattr(logs, "pod_status", lambda ns, pod, **_kw: None)
    plain = {"Authorization": f"Bearer {_token(app, roleIds=['seed-user'])}"}
    assert client.get("/api/v1/logs/pod-status?namespace=team-a&pod=p", headers=plain).status_code == 200
    names = client.get("/api/v1/logs/namespaces", headers=plain).get_json()["items"]
    assert names[-1] == "team-a" and names[0] == app.config["ALLOWED_NAMESPACES"][0]


def test_platform_cluster_id_is_resolved_before_refusing_plain_users(app, client, grants, monkeypatch):
    """화면이 플랫폼 클러스터의 클러스터 관리 id 를 넘겨도 일반 사용자가 허용 목록의 로그를 본다."""
    app.config["LOCAL_API_SERVER_URLS"] = ("https://10.74.89.47:6443",)
    monkeypatch.setattr(cluster_access, "_credentials", lambda cid: _creds())
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    monkeypatch.setattr(logs, "pod_status", lambda ns, pod, **_kw: None)
    plain = {"Authorization": f"Bearer {_token(app, roleIds=['seed-user'])}"}
    ns = app.config["ALLOWED_NAMESPACES"][0]
    res = client.get(f"/api/v1/logs/pod-status?namespace={ns}&pod=p&clusterId=c-platform", headers=plain)
    assert res.status_code == 200, res.get_json()
    granted_ns = client.get("/api/v1/logs/pod-status?namespace=team-a&pod=p&clusterId=c-platform", headers=plain)
    assert granted_ns.status_code == 200


def test_grants_are_read_once_until_restart(app, client, grants, monkeypatch):
    table, calls = grants
    monkeypatch.setattr(logs, "in_cluster", lambda: True)
    monkeypatch.setattr(logs, "pod_status", lambda ns, pod, **_kw: None)
    plain = {"Authorization": f"Bearer {_token(app, roleIds=['seed-user'])}"}
    client.get("/api/v1/logs/pod-status?namespace=team-a&pod=p", headers=plain)
    table[""] = ["team-b"]  # 저장만 하고 '권한 적용'(재시작) 전 — 반영되지 않는다.
    assert client.get("/api/v1/logs/pod-status?namespace=team-b&pod=p", headers=plain).status_code == 403
    assert calls == [""]
