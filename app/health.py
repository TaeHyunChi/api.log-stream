"""프로브/메타 엔드포인트 — 인증 없이 열려 있다."""

from flask import Blueprint, current_app

from .kube import in_cluster

bp = Blueprint("health", __name__)


@bp.get("/healthz")
def healthz():
    """liveness — 프로세스만 확인한다."""
    return {"status": "ok"}


@bp.get("/readyz")
def readyz():
    """readiness.

    클러스터 밖(로컬)이라 로그를 못 읽는 상태여도 **준비됨으로 둔다.** 그건 이
    프로세스의 장애가 아니고, not-ready 를 내면 쿠버네티스가 이미 붙어 있는
    WebSocket 연결까지 끊어 상황을 악화시킨다. 대신 상태를 함께 알려 준다.
    """
    return {"status": "ok", "inCluster": in_cluster()}


@bp.get("/")
def index():
    return {
        "service": current_app.config["SERVICE_NAME"],
        "allowedNamespaces": list(current_app.config["ALLOWED_NAMESPACES"]),
    }


@bp.get("/api/v1/system/info")
def system_info():
    """표준 시스템 정보 — 플랫폼 컨트롤 패널 fanout 대상 (MSA-TEMPLATE 4절).

    `dependsOn` 은 설정에 담긴 클러스터 내부 서비스 URL 에서 자동 추출한다 —
    목록을 손으로 들고 있지 않아 설정이 바뀌면 연관 그래프도 따라온다.
    """
    import re

    from flask import current_app

    self_name = current_app.config.get("SERVICE_NAME", "")
    # 인프라(k8s API/DB/브로커 등)는 MSA API 가 아니라 제외한다 — 그래프의 목적은
    # "어떤 API 를 끄면 어떤 API 가 영향을 받는가" 이다.
    infra = {
        "kubernetes", "postgres", "mariadb", "redis", "chroma", "rabbitmq",
        "langfuse-web", "langfuse", "kong-admin", "kong-proxy", "minio", "gitea-service",
    }
    dependencies = set()
    for value in current_app.config.values():
        if not isinstance(value, str):
            continue
        for match in re.finditer(r"([a-z0-9-]+)\.[a-z0-9-]+\.svc(?:\.cluster\.local)?", value):
            name = match.group(1)
            if name and name != self_name and name not in infra:
                dependencies.add(name)
    return {"service": self_name, "status": "ok", "dependsOn": sorted(dependencies)}
