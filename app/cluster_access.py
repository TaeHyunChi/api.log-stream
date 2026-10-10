"""어느 클러스터에 붙을지 — `clusterId` 한 개를 접속 정보(서버·인증·TLS)로 바꾼다.

예전에는 이 서비스가 **자기가 떠 있는 클러스터**(in-cluster ServiceAccount)만 읽었다. 클러스터 관리에
다른 클러스터가 추가되면 그 클러스터의 Pod 로그·터미널이 열리지 않았고, 같은 이름의 Pod 가 플랫폼
클러스터에 있으면 엉뚱한 것을 보여 줄 수도 있었다(2026-10-10). 이제 화면이 `clusterId` 를 넘기면:

    clusterId 없음 · LOCAL_CLUSTER_IDS · 서버 주소가 LOCAL_API_SERVER_URLS   → 이 클러스터(in-cluster SA)
    그 밖                                                                → 등록된 kubeconfig 로 그 클러스터

kubeconfig 원문은 k8s-cluster-service 의 **서비스 토큰 전용** `GET /clusters/{id}/credentials` 로 받는다
(secret-management-service · deploy-management-service 와 같은 경로). 60초 기억한다.

kubeconfig 해석은 k8s-cluster-service `app/target_cluster.py` 의 사본이다(서비스마다 자기 사본 — 이
워크스페이스의 규칙). current-context 의 server · CA · token · client-certificate 를 쓴다.
클라이언트 인증서는 ssl 이 파일 경로만 받아 임시 파일로 내려썼다가 로드 즉시 지운다.
이 파일은 api.pod-terminal `app/cluster_access.py` 와 같은 내용이다(서비스 이름만 다르다) — 함께 고친다.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
import yaml
from flask import current_app

log = logging.getLogger(__name__)

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = f"{_SA_DIR}/token"
CA_PATH = f"{_SA_DIR}/ca.crt"

#: k8s-cluster-service 의 서비스 전용 경로가 요구하는 `sub` 접두어(`svc:`).
SERVICE_SUBJECT = "svc:log-stream-service"
CACHE_SECONDS = 60.0

_cache: dict[str, tuple[float, Access]] = {}
_lock = threading.Lock()


class ClusterAccessError(Exception):
    """그 클러스터에 붙을 정보를 얻지 못했다 — 사용자에게 보여 줄 사유."""


class Access:
    """한 클러스터에 붙는 방법. `local` 이면 이 서비스의 ServiceAccount 로 붙는다."""

    def __init__(self, server: str, *, local: bool, cluster_id: str = "", token: str = "",
                 ca_pem: bytes | None = None, client_cert_pem: bytes | None = None,
                 client_key_pem: bytes | None = None, insecure: bool = False):
        self.server = server.rstrip("/")
        self.local = local
        self.cluster_id = cluster_id
        self._token = token
        self.ca_pem = ca_pem
        self.client_cert_pem = client_cert_pem
        self.client_key_pem = client_key_pem
        self.insecure = insecure

    def token(self) -> str:
        if self.local:
            # ServiceAccount 토큰은 회전한다 — 쓸 때마다 다시 읽는다.
            with open(TOKEN_PATH, encoding="utf-8") as handle:
                return handle.read().strip()
        return self._token

    def auth_headers(self) -> dict[str, str]:
        token = self.token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    def ssl_context(self) -> ssl.SSLContext:
        if self.local:
            return ssl.create_default_context(cafile=CA_PATH)
        if self.insecure:
            context = ssl._create_unverified_context()  # noqa: S323 — kubeconfig 가 명시한 경우만
        else:
            context = ssl.create_default_context(
                cadata=self.ca_pem.decode("utf-8") if self.ca_pem else None
            )
        if self.client_cert_pem and self.client_key_pem:
            cert_path = key_path = ""
            try:
                with tempfile.NamedTemporaryFile("wb", delete=False) as cert_file:
                    cert_file.write(self.client_cert_pem)
                    cert_path = cert_file.name
                with tempfile.NamedTemporaryFile("wb", delete=False) as key_file:
                    key_file.write(self.client_key_pem)
                    key_path = key_file.name
                context.load_cert_chain(cert_path, key_path)
            finally:
                for path in (cert_path, key_path):
                    if path:
                        try:
                            os.unlink(path)
                        except OSError:  # pragma: no cover
                            log.warning("임시 인증서 파일을 지우지 못했습니다.")
        return context

    def request(self, path: str, *, method: str = "GET", body: dict | None = None) -> urllib.request.Request:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.server}{path}", data=data, method=method)  # noqa: S310
        for key, value in self.auth_headers().items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return req


def in_cluster() -> bool:
    return os.path.exists(TOKEN_PATH)


def local_access() -> Access:
    return Access(current_app.config["K8S_API"], local=True)


def is_local_id(cluster_id: str) -> bool:
    return not cluster_id or cluster_id in current_app.config.get("LOCAL_CLUSTER_IDS", ())


def resolve(cluster_id: str | None) -> Access:
    """clusterId → 접속 정보. 실패하면 `ClusterAccessError`."""
    cluster_id = (cluster_id or "").strip()
    if is_local_id(cluster_id):
        return local_access()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(cluster_id)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    creds = _credentials(cluster_id)
    servers = {creds.get("apiServerUrl") or ""}
    access = _from_kubeconfig(creds.get("kubeconfig") or "", cluster_id)
    servers.add(access.server)
    local_urls = {u.rstrip("/") for u in current_app.config.get("LOCAL_API_SERVER_URLS", ())}
    if {s.rstrip("/") for s in servers if s} & local_urls:
        # 클러스터 관리에 노드 주소로 등록된 이 플랫폼 클러스터 — ServiceAccount 로 붙는다
        # (네임스페이스 허용 목록 등 기존 규칙 그대로).
        access = local_access()
        access.cluster_id = cluster_id
    with _lock:
        _cache[cluster_id] = (now, access)
    return access


def _service_token() -> str:
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "sub": SERVICE_SUBJECT,
            "roleIds": ["seed-admin"],
            "iat": now,
            "exp": now + dt.timedelta(seconds=120),
        },
        current_app.config["JWT_SECRET"],
        algorithm=current_app.config.get("JWT_ALGORITHM", "HS256"),
    )


def _credentials(cluster_id: str) -> dict:
    base = (current_app.config.get("K8S_CLUSTER_SERVICE_URL") or "").rstrip("/")
    if not base or not current_app.config.get("JWT_SECRET"):
        raise ClusterAccessError("다른 클러스터에 붙을 설정(K8S_CLUSTER_SERVICE_URL)이 없습니다.")
    req = urllib.request.Request(  # noqa: S310 - 설정으로 주입한 내부 URL
        f"{base}/clusters/{urllib.parse.quote(cluster_id, safe='')}/credentials"
    )
    req.add_header("Authorization", f"Bearer {_service_token()}")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:  # noqa: S310
            body = json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ClusterAccessError("등록되지 않은 클러스터입니다.") from None
        log.warning("클러스터 %s 자격 조회 실패: %s", cluster_id, exc.code)
        raise ClusterAccessError(f"클러스터 접속 정보를 가져오지 못했습니다({exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("클러스터 %s 자격 조회 실패: %s", cluster_id, exc)
        raise ClusterAccessError("클러스터 관리 서비스에 연결할 수 없습니다.") from None
    if not isinstance(body, dict):
        raise ClusterAccessError("클러스터 접속 정보 형식이 올바르지 않습니다.")
    return body


def _decode(data: str, what: str) -> bytes:
    try:
        return base64.b64decode(data)
    except (ValueError, TypeError):
        raise ClusterAccessError(f"kubeconfig 의 {what} 를 해석할 수 없습니다.") from None


def _from_kubeconfig(text: str, cluster_id: str) -> Access:
    if not (text or "").strip():
        raise ClusterAccessError(
            "이 클러스터에 등록된 kubeconfig 가 없습니다. 클러스터 관리에서 등록하세요."
        )
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ClusterAccessError("kubeconfig 를 해석할 수 없습니다(YAML 오류).") from None
    if not isinstance(doc, dict):
        raise ClusterAccessError("kubeconfig 형식이 올바르지 않습니다.")

    def by_name(section: str, name: str) -> dict:
        for entry in doc.get(section) or []:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
        return {}

    context = (by_name("contexts", doc.get("current-context") or "").get("context")) or {}
    cluster = (by_name("clusters", context.get("cluster") or "").get("cluster")) or {}
    user = (by_name("users", context.get("user") or "").get("user")) or {}
    server = (cluster.get("server") or "").strip()
    if not server:
        raise ClusterAccessError("kubeconfig 에 API 서버 주소(server)가 없습니다.")
    ca_data = cluster.get("certificate-authority-data") or ""
    cert_data = user.get("client-certificate-data") or ""
    key_data = user.get("client-key-data") or ""
    return Access(
        server,
        local=False,
        cluster_id=cluster_id,
        token=(user.get("token") or "").strip(),
        ca_pem=_decode(ca_data, "CA 인증서") if ca_data else None,
        client_cert_pem=_decode(cert_data, "클라이언트 인증서") if cert_data else None,
        client_key_pem=_decode(key_data, "클라이언트 키") if key_data else None,
        insecure=bool(cluster.get("insecure-skip-tls-verify")),
    )


def clear_cache() -> None:
    with _lock:
        _cache.clear()
