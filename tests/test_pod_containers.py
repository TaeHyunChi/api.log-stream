"""Pod 상태의 컨테이너 목록 — 멀티 컨테이너 Pod 에서 화면이 고를 수 있어야 한다."""

from app.kube import _containers

POD = {
    "spec": {
        "initContainers": [{"name": "harness-clone", "image": "runtime:1"}],
        "containers": [
            {"name": "agent-service-instance", "image": "runtime:1"},
            {"name": "web", "image": "agent-web:1"},
        ],
    },
    "status": {
        "initContainerStatuses": [
            {"name": "harness-clone", "ready": True, "restartCount": 0,
             "state": {"terminated": {"reason": "Completed"}}}
        ],
        "containerStatuses": [
            {"name": "agent-service-instance", "ready": True, "restartCount": 0,
             "state": {"running": {}}},
            {"name": "web", "ready": False, "restartCount": 2,
             "state": {"waiting": {"reason": "CrashLoopBackOff"}}},
        ],
    },
}


def test_containers_include_init_first_in_spec_order():
    names = [c["name"] for c in _containers(POD)]
    assert names == ["harness-clone", "agent-service-instance", "web"]


def test_container_state_and_restarts():
    by_name = {c["name"]: c for c in _containers(POD)}
    assert by_name["harness-clone"]["init"] is True
    assert by_name["harness-clone"]["state"] == "terminated"
    assert by_name["agent-service-instance"]["state"] == "running"
    assert by_name["agent-service-instance"]["ready"] is True
    assert by_name["web"]["state"] == "waiting"
    assert by_name["web"]["restarts"] == 2


def test_status_without_containers_is_empty_not_error():
    assert _containers({}) == []
