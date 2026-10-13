"""네임스페이스 권한 설정에서 '로그 조회' 가 켜진 네임스페이스 — 클러스터별 (volume-service 0.7.0).

관리자 메뉴 '네임스페이스 권한 설정'이 클러스터마다 네임스페이스에 로그 조회 권한을 켠다. 이 서비스는 그 목록을
일반 사용자 허용 목록에 더한다:

    이 플랫폼 클러스터   설정 ALLOWED_NAMESPACES + 권한 설정(clusterId '')
    다른 클러스터        권한 설정(clusterId = 그 클러스터) — 관리자·클러스터 관리 권한이 없어도 열린다

목록은 **처음 쓸 때 한 번** 읽어 기억한다(클러스터별). 화면의 '권한 적용'이 이 서비스를 재시작해 다시 읽게
한다 — 저장만 하고 적용하지 않은 설정은 반영되지 않는다. 읽지 못하면 30초 뒤 다시 시도하고, 그동안은
설정 ALLOWED_NAMESPACES 만 쓴다(다른 클러스터는 아무 것도 열지 않는다).

volume-service 는 서비스 토큰(cluster_access._service_token)으로 부른다 — 사용자 토큰이 없어도 읽힌다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

log = logging.getLogger(__name__)

GRANT_KIND = "log"
RETRY_SECONDS = 30.0

#: clusterKey('' = 이 플랫폼 클러스터) → (읽은 시각, 성공 여부, 이름 목록)
_CACHE: dict[str, tuple[float, bool, list[str]]] = {}
_LOCK = threading.Lock()


def _fetch(cluster_key: str) -> list[str]:
    from .cluster_access import _service_token  # 순환 import 회피

    base = (current_app.config.get("VOLUME_SERVICE_URL") or "").rstrip("/")
    query = {"grant": GRANT_KIND}
    if cluster_key:
        query["clusterId"] = cluster_key
    req = urllib.request.Request(  # noqa: S310 - 설정으로 주입한 내부 URL
        f"{base}/volumes/namespaces?{urllib.parse.urlencode(query)}"
    )
    req.add_header("Authorization", f"Bearer {_service_token()}")
    with urllib.request.urlopen(req, timeout=3) as res:  # noqa: S310
        body = json.loads(res.read() or b"{}")
    return [i["name"] for i in body.get("items") or [] if isinstance(i, dict) and i.get("name")]


def granted(cluster_key: str = "") -> list[str]:
    """권한 설정에서 로그 조회가 켜진 네임스페이스(그 클러스터). 설정이 없거나 읽지 못하면 []."""
    if not current_app.config.get("VOLUME_SERVICE_URL") or not current_app.config.get("JWT_SECRET"):
        return []
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(cluster_key)
        if hit and (hit[1] or now - hit[0] < RETRY_SECONDS):
            return list(hit[2])
    try:
        items = _fetch(cluster_key)
        ok = True
        log.info("네임스페이스 권한 설정(로그 조회, 클러스터 %r) %d개를 읽었습니다.", cluster_key or "플랫폼", len(items))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("네임스페이스 권한 설정(로그 조회) 목록을 읽지 못했습니다 — 30초 뒤 다시: %s", exc)
        items, ok = (list(hit[2]) if hit else []), False
    with _LOCK:
        _CACHE[cluster_key] = (now, ok, items)
    return list(items)


def is_allowed(namespace: str, cluster_key: str = "") -> bool:
    """일반 사용자가 이 네임스페이스의 로그를 읽어도 되는가(관리자 규칙은 따로)."""
    if not cluster_key and namespace in current_app.config["ALLOWED_NAMESPACES"]:
        return True
    return namespace in granted(cluster_key)


def allowed_namespaces(cluster_key: str = "") -> list[str]:
    """화면이 고를 수 있는 목록 — 플랫폼 클러스터는 설정이 앞, 그 뒤에 권한 설정(중복 없이)."""
    out = list(current_app.config["ALLOWED_NAMESPACES"]) if not cluster_key else []
    for name in granted(cluster_key):
        if name not in out:
            out.append(name)
    return out


def reset() -> None:
    """테스트용 — 기억한 목록을 비운다."""
    with _LOCK:
        _CACHE.clear()
