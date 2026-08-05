"""k8s Pod 로그를 이어서 읽는다.

공식 클라이언트를 쓰지 않고 in-cluster ServiceAccount 토큰으로 REST 를 직접 부른다
(플랫폼의 다른 서비스들과 같은 방식). 필요한 동사는 `pods/log` 의 get 하나뿐이다.

`follow=true` 로 열면 k8s 가 응답을 닫지 않고 새 줄이 생길 때마다 흘려보낸다.
그래서 **응답을 한 번에 읽지 않고 한 줄씩** 꺼내 쓴다 — `read()` 를 부르면 Pod 가
죽을 때까지 돌아오지 않는다.
"""

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from flask import current_app

log = logging.getLogger(__name__)

_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = f"{_SA_DIR}/token"
CA_PATH = f"{_SA_DIR}/ca.crt"


class KubeError(Exception):
    """k8s 쪽 실패. 호출자가 사용자에게 보여 줄 사유로 바꾼다."""


def in_cluster() -> bool:
    return os.path.exists(TOKEN_PATH)


def _request(path: str) -> urllib.request.Request:
    api = current_app.config["K8S_API"].rstrip("/")
    request = urllib.request.Request(f"{api}{path}", method="GET")  # noqa: S310
    # 토큰은 회전한다 — 열 때마다 다시 읽는다.
    with open(TOKEN_PATH, encoding="utf-8") as handle:
        request.add_header("Authorization", f"Bearer {handle.read().strip()}")
    return request


def _containers(body: dict) -> list[dict]:
    """Pod 의 컨테이너 목록 — 여러 개면 화면이 고르게 한다.

    init 컨테이너도 포함한다(로그가 남고, 실패 원인이 거기 있을 때가 많다).
    `ready`/`state` 는 상태 배지용이고, 순서는 spec 순서 그대로다.
    """
    spec = body.get("spec") or {}
    status = body.get("status") or {}
    state_of = {}
    for key in ("containerStatuses", "initContainerStatuses"):
        for cs in status.get(key) or []:
            if not isinstance(cs, dict):
                continue
            state = cs.get("state") or {}
            # 값이 아니라 **키 존재**로 판정한다 — running 이 빈 객체로 올 수 있어
            # 참/거짓으로 보면 실행 중인 컨테이너를 waiting 으로 오판한다.
            phase = "running" if "running" in state else (
                "terminated" if "terminated" in state else "waiting"
            )
            state_of[cs.get("name")] = {
                "ready": bool(cs.get("ready")),
                "state": phase,
                "restarts": int(cs.get("restartCount") or 0),
            }

    containers = []
    for key, is_init in (("initContainers", True), ("containers", False)):
        for c in spec.get(key) or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            meta = state_of.get(c["name"], {"ready": False, "state": "waiting", "restarts": 0})
            containers.append({
                "name": c["name"],
                "image": c.get("image") or "",
                "init": is_init,
                **meta,
            })
    return containers


def pod_status(namespace: str, pod: str) -> dict | None:
    """Pod 한 개의 현재 상태. 없으면(삭제됐으면) None.

    로그/터미널을 열기 전의 **상태 확인**에 쓴다 — 이미 사라진 Pod 에 스트림을
    열려고 하면 사용자에게는 원인 없는 연결 실패로만 보인다. 먼저 물어보고,
    없으면 화면이 상태 정보만 그리게 한다.
    """
    path = (
        f"/api/v1/namespaces/{urllib.parse.quote(namespace)}"
        f"/pods/{urllib.parse.quote(pod)}"
    )
    context = ssl.create_default_context(cafile=CA_PATH)
    timeout = current_app.config["K8S_CONNECT_TIMEOUT"]
    try:
        with urllib.request.urlopen(  # noqa: S310
            _request(path), timeout=timeout, context=context
        ) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code in (401, 403):
            raise KubeError("이 Pod 를 조회할 권한이 없습니다.") from None
        raise KubeError(f"Pod 상태를 가져오지 못했습니다({exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("Pod 상태 조회 실패: %s/%s %s", namespace, pod, exc)
        raise KubeError("k8s API 에 연결할 수 없습니다.") from None

    status = body.get("status") or {}
    conditions = status.get("conditions") or []
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in conditions
        if isinstance(c, dict)
    )
    # 왜 안 뜨는지(CrashLoopBackOff 같은 것)는 컨테이너 상태에 들어 있다.
    reason = ""
    for cs in status.get("containerStatuses") or []:
        state = (cs or {}).get("state") or {}
        waiting = state.get("waiting") or {}
        terminated = state.get("terminated") or {}
        reason = waiting.get("reason") or terminated.get("reason") or reason

    return {
        "phase": status.get("phase") or "",
        "ready": ready,
        "startedAt": status.get("startTime") or "",
        "reason": reason,
        "containers": _containers(body),
    }


def log_url(namespace: str, pod: str, *, container: str = "", since: str = "", tail: int = 0) -> str:
    """로그 스트림 경로.

    `sinceTime` 은 **마지막으로 받은 시각**이다. 재접속할 때 이 값을 주면 그 뒤에
    생긴 줄부터 온다 — 처음부터 다시 받아 화면에 중복으로 쌓이지 않는다.
    `sinceTime` 과 `tailLines` 를 함께 주면 k8s 가 tailLines 를 무시하므로 둘 중
    하나만 붙인다.
    """
    query: dict[str, str] = {"follow": "true", "timestamps": "true"}
    if container:
        query["container"] = container
    if since:
        query["sinceTime"] = since
    elif tail:
        query["tailLines"] = str(tail)
    path = f"/api/v1/namespaces/{urllib.parse.quote(namespace)}/pods/{urllib.parse.quote(pod)}/log"
    return f"{path}?{urllib.parse.urlencode(query)}"


def stream_lines(
    namespace: str, pod: str, *, container: str = "", since: str = "", tail: int = 0
) -> Iterator[str]:
    """로그를 한 줄씩 내보낸다. 연결이 끊기거나 Pod 가 사라지면 끝난다.

    호출측이 순회를 멈추면(WebSocket 이 닫히면) 제너레이터가 닫히면서 응답도
    함께 닫힌다 — k8s 쪽 연결이 남지 않는다.
    """
    url_path = log_url(namespace, pod, container=container, since=since, tail=tail)
    context = ssl.create_default_context(cafile=CA_PATH)
    timeout = current_app.config["K8S_CONNECT_TIMEOUT"]
    try:
        response = urllib.request.urlopen(  # noqa: S310
            _request(url_path), timeout=timeout, context=context
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        log.warning("로그 스트림 실패: %s %s %s", exc.code, url_path, detail)
        if exc.code == 404:
            raise KubeError("Pod 를 찾을 수 없습니다.") from None
        if exc.code in (401, 403):
            raise KubeError("이 Pod 의 로그를 읽을 권한이 없습니다.") from None
        raise KubeError(f"로그를 가져오지 못했습니다({exc.code}).") from None
    except (urllib.error.URLError, OSError) as exc:
        log.warning("로그 스트림 연결 실패: %s", exc)
        raise KubeError("k8s API 에 연결할 수 없습니다.") from None

    # 연결에는 제한 시간을 두되, **열린 뒤에는 제한을 푼다.** urllib 의 timeout 은
    # 읽기에도 걸려서 그대로 두면 조용한 Pod 의 스트림이 그 시간마다 끊긴다 —
    # 로그가 뜸한 서비스일수록 계속 끊기는 셈이라 실시간 중계로 쓸 수 없다.
    # (연결이 죽으면 읽기가 그대로 끝나므로 무한정 매달리지 않는다.)
    _unset_read_timeout(response)

    try:
        for raw in response:
            # 스트림이라 줄바꿈이 끊겨 올 수 있다. urllib 의 파일 객체는 줄 단위로
            # 모아 주므로 여기서는 개행만 떼면 된다.
            yield raw.decode("utf-8", errors="replace").rstrip("\n")
    finally:
        response.close()


def _unset_read_timeout(response) -> None:
    """열린 응답의 소켓 타임아웃을 해제한다.

    urllib 은 연결과 읽기에 같은 timeout 을 쓴다. 공개 API 로는 나눠 줄 수 없어
    내부 소켓을 직접 만진다 — 없으면 조용히 넘어가고, 그때는 기존처럼 타임아웃마다
    스트림이 다시 열린다(동작은 하되 끊김이 보인다).
    """
    sock = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(sock, "_sock", None)
    if sock is not None:
        try:
            sock.settimeout(None)
        except OSError:  # pragma: no cover - 플랫폼별 예외
            log.debug("소켓 타임아웃을 해제하지 못했습니다.")


def split_timestamp(line: str) -> tuple[str, str]:
    """`timestamps=true` 로 받은 줄을 (시각, 본문) 으로 나눈다.

    시각을 그대로 돌려주는 이유 — 화면이 재접속할 때 `since` 로 다시 보내야 한다.
    """
    stamp, sep, text = line.partition(" ")
    if sep and stamp.count("-") == 2 and "T" in stamp:
        return stamp, text
    return "", line
