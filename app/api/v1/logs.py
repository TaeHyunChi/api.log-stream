"""Pod 로그 실시간 중계.

    브라우저 ── WS  /api/v1/logs/stream?namespace=&pod=&since=&token=
                     │
                 k8s  GET /api/v1/namespaces/{ns}/pods/{pod}/log?follow=true

보내기만 하는 채널이다. 프레임은 한 줄에 하나씩 JSON 으로 나간다.

    {"type":"ready",  "namespace":"...", "pod":"...", "container":"..."}
    {"type":"log",    "time":"2026-07-30T10:42:28.06Z", "text":"GET / ..."}
    {"type":"ping"}                                   유휴 연결 유지
    {"type":"end",    "reason":"stream-closed"}       Pod 가 끝났다
    {"type":"error",  "message":"Pod 를 찾을 수 없습니다."}

`since` 는 **마지막으로 받은 로그의 시각**이다. 끊겼다 다시 붙을 때 그 값을 주면
그 뒤에 생긴 줄부터 온다 — 처음부터 다시 받아 화면에 중복으로 쌓이지 않는다.
`time` 을 그대로 돌려주는 이유가 이것이다.
"""

import json
import logging
import queue
import threading

from flask import Blueprint, current_app, request

from ...auth import subject_from_query
from ...kube import KubeError, in_cluster, split_timestamp, stream_lines

log = logging.getLogger(__name__)

bp = Blueprint("logs", __name__, url_prefix="/logs")

#: 읽는 쪽이 느릴 때 쌓아 둘 줄 수. 넘치면 오래된 줄부터 버린다 — 실시간 로그는
#: 밀린 것을 다 보여 주는 것보다 지금 것을 보여 주는 게 맞다.
_QUEUE_SIZE = 1000


@bp.get("/namespaces")
def allowed_namespaces():
    """로그를 읽을 수 있는 네임스페이스 목록 — 화면이 고를 수 있게."""
    return {"items": list(current_app.config["ALLOWED_NAMESPACES"])}


def _send(ws, payload: dict) -> None:
    ws.send(json.dumps(payload, ensure_ascii=False))


def _reader(app, params: dict, out: queue.Queue) -> None:
    """k8s 로그를 읽어 큐에 넣는 스레드.

    별도 스레드인 이유 — `stream_lines` 는 새 줄이 없으면 블로킹한다. 그 자리에서
    읽으면 ping 을 보낼 수도, 클라이언트가 끊은 것을 알아챌 수도 없다.
    """
    try:
        with app.app_context():
            for line in stream_lines(
                params["namespace"],
                params["pod"],
                container=params["container"],
                since=params["since"],
                tail=params["tail"],
            ):
                stamp, text = split_timestamp(line)
                try:
                    out.put_nowait({"type": "log", "time": stamp, "text": text})
                except queue.Full:
                    # 읽는 쪽이 밀렸다. 가장 오래된 것을 버리고 최신을 넣는다.
                    try:
                        out.get_nowait()
                        out.put_nowait({"type": "log", "time": stamp, "text": text})
                    except (queue.Empty, queue.Full):
                        pass
        out.put({"type": "end", "reason": "stream-closed"})
    except KubeError as exc:
        out.put({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 — 스레드에서 죽으면 조용히 사라진다
        log.warning("로그 스트림 중단: %s", exc)
        out.put({"type": "error", "message": "로그 스트림이 끊겼습니다."})


def _params() -> tuple[dict | None, str]:
    """요청 파라미터 검증. (값, 오류 메시지)."""
    namespace = (request.args.get("namespace") or "").strip()
    pod = (request.args.get("pod") or request.args.get("podId") or "").strip()
    if not namespace or not pod:
        return None, "namespace 와 pod 는 필수입니다."

    allowed = current_app.config["ALLOWED_NAMESPACES"]
    if namespace not in allowed:
        # 어떤 네임스페이스가 있는지는 알려 주지 않는다.
        return None, "이 네임스페이스의 로그는 읽을 수 없습니다."

    tail = current_app.config["DEFAULT_TAIL_LINES"]
    raw_tail = request.args.get("tail")
    if raw_tail:
        try:
            tail = max(0, min(int(raw_tail), current_app.config["MAX_TAIL_LINES"]))
        except ValueError:
            return None, "tail 은 정수여야 합니다."

    return {
        "namespace": namespace,
        "pod": pod,
        "container": (request.args.get("container") or "").strip(),
        # 마지막으로 받은 시각. 있으면 tail 대신 이 시점부터 이어 받는다.
        "since": (request.args.get("since") or request.args.get("sinceTime") or "").strip(),
        "tail": tail,
    }, ""


def stream(ws) -> None:
    """로그 WebSocket. 연결이 끊길 때까지 살아 있는다."""
    if not subject_from_query():
        # 1008 = policy violation. 브라우저 콘솔에 이유가 남는다.
        ws.close(1008, "unauthorized")
        return

    params, error = _params()
    if params is None:
        _send(ws, {"type": "error", "message": error})
        ws.close(1008, "bad-request")
        return

    if not in_cluster():
        # 로컬/테스트 — 클러스터가 없으면 읽을 로그도 없다. 연결은 정상으로 닫는다.
        _send(ws, {"type": "error", "message": "클러스터 밖에서는 로그를 읽을 수 없습니다."})
        ws.close(1011, "no-cluster")
        return

    _send(
        ws,
        {
            "type": "ready",
            "namespace": params["namespace"],
            "pod": params["pod"],
            "container": params["container"],
        },
    )

    out: queue.Queue = queue.Queue(maxsize=_QUEUE_SIZE)
    worker = threading.Thread(
        target=_reader,
        args=(current_app._get_current_object(), params, out),
        daemon=True,
        name=f"logs-{params['pod'][:40]}",
    )
    worker.start()

    ping_seconds = current_app.config["WS_PING_SECONDS"]
    try:
        while True:
            try:
                payload = out.get(timeout=ping_seconds)
            except queue.Empty:
                # 새 로그가 없다. Kong 이나 브라우저가 유휴 연결을 끊지 않도록 알린다.
                _send(ws, {"type": "ping"})
                continue

            _send(ws, payload)
            if payload["type"] in ("end", "error"):
                break
    except Exception:  # noqa: BLE001 — 정상 종료도 예외로 온다
        pass
    finally:
        # 스레드는 daemon 이고, 제너레이터가 닫히면서 k8s 연결도 함께 닫힌다.
        log.info("로그 스트림 종료: %s/%s", params["namespace"], params["pod"])
