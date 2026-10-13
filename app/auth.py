"""WebSocket 인증 — `?token=` 쿼리의 JWT 를 검증한다.

브라우저의 WebSocket API 는 헤더를 붙일 수 없어 Authorization 을 쓸 수 없다.
그래서 쿼리로 받는다. 대가가 있다 — 토큰이 URL 에 실려 접근 로그나 프록시 로그에
남을 수 있다. 그래서 이 서비스의 접근 로그는 쿼리를 남기지 않고(gunicorn 기본
포맷은 경로만 찍는다), 토큰 자체는 어디에도 기록하지 않는다.
"""

import logging

import jwt
from flask import current_app, request

log = logging.getLogger(__name__)


def subject_from_query() -> str | None:
    """접속한 사용자 id. 검증에 실패하면 None."""
    return _subject((request.args.get("token") or "").strip())


def subject_from_request() -> str | None:
    """HTTP 엔드포인트용 — Authorization 헤더를 먼저 보고 `?token=` 으로 물러난다.

    WebSocket 은 헤더를 못 붙여 쿼리로 받지만, 보통의 GET 은 헤더를 쓸 수 있고
    그쪽이 로그에 토큰이 남지 않아 낫다.
    """
    header = (request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return _subject(header[7:].strip())
    return subject_from_query()


def claims_from_request() -> dict | None:
    """검증된 JWT 클레임 — 헤더 먼저, 없으면 `?token=`. 실패하면 None(AUTH_DISABLED 면 빈 dict)."""
    header = (request.headers.get("Authorization") or "").strip()
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    else:
        token = (request.args.get("token") or "").strip()
    return _claims(token)


def _claims(token: str) -> dict | None:
    if current_app.config.get("AUTH_DISABLED"):
        return {"sub": (request.args.get("userId") or "anonymous").strip() or "anonymous"}
    secret = current_app.config.get("JWT_SECRET")
    if not token or not secret:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[current_app.config.get("JWT_ALGORITHM", "HS256")],
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.InvalidTokenError as exc:
        log.info("토큰 거절: %s", exc)
        return None
    return claims if isinstance(claims.get("sub"), str) and claims.get("sub") else None


def can_reach_other_clusters(claims: dict | None) -> bool:
    """다른 클러스터(클러스터 관리에 추가된 것)의 Pod 를 볼 자격 — 관리자 또는 클러스터 관리 권한.

    다른 클러스터는 이 서비스의 네임스페이스 허용 목록이 아니라 **그 클러스터의 관리자 kubeconfig** 로
    붙는다. 그래서 로그인만으로는 열지 않고, 그 클러스터를 다룰 수 있는 사람에게만 연다.
    """
    if current_app.config.get("AUTH_DISABLED"):
        return True
    if not claims:
        return False
    roles: list[str] = []
    for name in ("roleIds", "roles", "role_ids"):
        value = claims.get(name)
        if isinstance(value, str):
            roles.append(value)
        elif isinstance(value, list):
            roles.extend(r for r in value if isinstance(r, str))
    if current_app.config["ADMIN_ROLE_ID"] in roles:
        return True
    perms = claims.get("permissions")
    wanted = current_app.config["REMOTE_CLUSTER_PERMISSIONS"]
    return isinstance(perms, list) and any(p in perms for p in wanted)


def _subject(token: str) -> str | None:
    if current_app.config.get("AUTH_DISABLED"):
        return (request.args.get("userId") or "anonymous").strip() or "anonymous"

    secret = current_app.config.get("JWT_SECRET")
    if not token or not secret:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[current_app.config.get("JWT_ALGORITHM", "HS256")],
            options={"require": ["exp"], "verify_aud": False},
        )
    except jwt.InvalidTokenError as exc:
        log.info("ws 토큰 거절: %s", exc)
        return None

    subject = claims.get("sub")
    return subject if isinstance(subject, str) and subject else None

def is_admin(claims: dict | None) -> bool:
    """플랫폼 관리자(Role ADMIN_ROLE_ID) — 관리자 전용 네임스페이스(ADMIN_NAMESPACES)의 Pod 를 열 자격."""
    if current_app.config.get("AUTH_DISABLED"):
        return True
    if not claims:
        return False
    roles: list[str] = []
    for name in ("roleIds", "roles", "role_ids"):
        value = claims.get(name)
        if isinstance(value, str):
            roles.append(value)
        elif isinstance(value, list):
            roles.extend(r for r in value if isinstance(r, str))
    return current_app.config["ADMIN_ROLE_ID"] in roles


def is_admin_namespace(namespace: str) -> bool:
    """허용 목록 밖이지만 관리자는 열 수 있는 네임스페이스 — 설정 ADMIN_NAMESPACES(`*` = 나머지 전부)."""
    admin = current_app.config.get("ADMIN_NAMESPACES") or []
    return "*" in admin or namespace in admin
