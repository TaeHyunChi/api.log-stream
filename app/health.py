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
