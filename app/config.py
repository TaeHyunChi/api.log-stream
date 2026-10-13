"""설정. 모든 값은 환경변수로 덮어쓴다(ConfigMap/Secret)."""

import os


def _csv(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "log-stream-service")
    API_PREFIX = os.getenv("API_PREFIX", "/api/v1")
    PORT = int(os.getenv("PORT", "8246"))
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    JSON_LOGS = os.getenv("JSON_LOGS", "true").lower() == "true"

    # WebSocket 은 브라우저가 헤더를 붙일 수 없어 `?token=` 쿼리로 JWT 를 받는다.
    JWT_SECRET = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    AUTH_DISABLED = os.getenv("AUTH_DISABLED", "false").lower() == "true"

    # --- k8s ---
    K8S_API = os.getenv("K8S_API", "https://kubernetes.default.svc")

    #: 로그를 읽어도 되는 네임스페이스. **비워 두면 아무 것도 못 읽는다.**
    #:
    #: 네임스페이스를 요청 파라미터로 받으므로 목록이 없으면 로그인한 사람이
    #: `kube-system` 의 로그까지 가져갈 수 있다. RBAC 으로도 막지만(Role 을 이
    #: 네임스페이스들에만 준다) 서버가 먼저 거절해 이유를 분명히 알려 준다.
    ALLOWED_NAMESPACES = _csv(
        "ALLOWED_NAMESPACES",
        "oncloud-ai-platform,oncloud-ai-sandbox,oncloud-ai-devops-workspace,oncloud-ai-devops-service,"
        "oncloud-ai-model-workspace,oncloud-ai-model-serving",
    )

    # --- 다른 클러스터(클러스터 관리에 추가된 것) — app/cluster_access.py ---
    #: kubeconfig 원문을 받는 곳(서비스 토큰 전용 /clusters/{id}/credentials). 비우면 이 클러스터만.
    K8S_CLUSTER_SERVICE_URL = os.getenv(
        "K8S_CLUSTER_SERVICE_URL", "http://k8s-cluster-service.oncloud-ai-platform.svc.cluster.local:8209/api/v1"
    ).rstrip("/")
    #: 이 플랫폼 클러스터로 볼 클러스터 id·API 서버 주소(노드 주소로 등록된 경우) — ServiceAccount 로 붙는다.
    LOCAL_CLUSTER_IDS = tuple(_csv("LOCAL_CLUSTER_IDS", ""))
    #: 네임스페이스 권한 설정(로그 조회, 클러스터별) — volume-service `GET /volumes/namespaces?grant=log`.
    #: 기동 뒤 처음 쓸 때 한 번 읽는다(app/granted.py). 비우면 설정 ALLOWED_NAMESPACES 만.
    VOLUME_SERVICE_URL = os.getenv(
        "VOLUME_SERVICE_URL", "http://volume-service.oncloud-ai-platform.svc.cluster.local:8215/api/v1"
    ).rstrip("/")
    LOCAL_API_SERVER_URLS = tuple(_csv("LOCAL_API_SERVER_URLS", ""))
    ADMIN_ROLE_ID = os.getenv("ADMIN_ROLE_ID", "seed-admin")
    # 관리자 전용 네임스페이스 — 허용 목록 밖이라도 관리자(ADMIN_ROLE_ID)는 연다. 일반 사용자는
    # "관리자 전용 POD 입니다."(ADMIN_ONLY). `*` 는 허용 목록 밖 전부(kube-system 포함), 비우면 끈다.
    # RBAC 도 클러스터 단위로 줘야 한다(30-rbac.yaml 의 ClusterRole).
    ADMIN_NAMESPACES = _csv("ADMIN_NAMESPACES", "*")
    #: 다른 클러스터의 Pod 를 볼 수 있는 권한 키(관리자는 늘 가능).
    REMOTE_CLUSTER_PERMISSIONS = tuple(
        _csv("REMOTE_CLUSTER_PERMISSIONS", "k8s-cluster.resource.manage,k8s-cluster.cluster.manage")
    )

    #: 처음 접속했을 때 거슬러 올라가 보낼 줄 수. `since` 를 주면 그쪽이 우선한다.
    DEFAULT_TAIL_LINES = int(os.getenv("DEFAULT_TAIL_LINES", "200"))
    MAX_TAIL_LINES = int(os.getenv("MAX_TAIL_LINES", "5000"))

    #: 유휴 연결이 끊기지 않도록 보내는 ping 간격(초).
    WS_PING_SECONDS = int(os.getenv("WS_PING_SECONDS", "30"))
    #: 로그 스트림을 여는 데 걸어 두는 시간(초). 연결 자체는 무한히 산다.
    K8S_CONNECT_TIMEOUT = int(os.getenv("K8S_CONNECT_TIMEOUT", "10"))


class TestConfig(Config):
    AUTH_DISABLED = True
    K8S_CLUSTER_SERVICE_URL = ""
    VOLUME_SERVICE_URL = ""
    JSON_LOGS = False
    ALLOWED_NAMESPACES = ["oncloud-ai-devops-service", "oncloud-ai-devops-workspace"]
