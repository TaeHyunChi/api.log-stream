"""v1 라우트 등록."""

from flask import Blueprint

from .logs import bp as logs_bp
from .logs import stream


def register_v1(app, prefix: str = "/api/v1") -> None:
    v1 = Blueprint("v1", __name__, url_prefix=prefix)
    v1.register_blueprint(logs_bp)
    app.register_blueprint(v1)
    _register_socket(app, prefix)


def _register_socket(app, prefix: str) -> None:
    """로그 WebSocket 을 붙인다.

    flask-sock 이 없으면(의존성을 안 깐 로컬) 조용히 건너뛴다 — REST 는 그대로
    동작해야 한다. 운영 이미지는 requirements.txt 로 항상 설치된다.
    """
    try:
        from flask_sock import Sock
    except ImportError:  # pragma: no cover - 의존성이 있는 환경에서는 지나가지 않는다
        app.logger.warning("flask-sock 이 없어 로그 WebSocket 을 열지 않습니다.")
        return

    sock = Sock(app)
    sock.route(f"{prefix}/logs/stream")(stream)
