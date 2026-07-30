"""Pod 로그 실시간 중계 서비스.

모니터링할 Pod(네임스페이스 + 이름)와 마지막으로 받은 로그 시각을 받아, 그 Pod 에서
새로 생기는 로그를 WebSocket 으로 흘려보낸다.

왜 별도 서비스인가
------------------
로그 스트림은 **응답이 끝나지 않는 연결**이다. 일반 REST 서비스에 붙이면 워커
스레드를 계속 붙잡아, 로그 창을 몇 개 열어 둔 것만으로 그 서비스의 다른 API 가
막힌다. 그래서 오래 붙어 있는 연결만 다루는 서비스로 떼어 낸다 — 스레드 수를
동시 접속 수에 맞춰 따로 조절할 수 있다(알림 WebSocket 과 같은 이유).

DB 를 쓰지 않는다. 로그의 원본은 k8s 이고 이 서비스는 중계만 한다.
"""

import uuid

from flask import Flask, g, request

from .api.v1 import register_v1
from .config import Config
from .errors import register_error_handlers
from .health import bp as health_bp
from .logging_config import configure_logging


def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config()
    app = Flask(__name__)
    app.config.from_object(cfg)
    # 오류 문구가 한글이라 \uXXXX 로 이스케이프되지 않게 UTF-8 그대로 내보낸다.
    app.json.ensure_ascii = False

    configure_logging(app.config["LOG_LEVEL"], app.config["JSON_LOGS"])

    register_error_handlers(app)
    app.register_blueprint(health_bp)
    register_v1(app, app.config["API_PREFIX"])

    @app.before_request
    def _assign_request_id():
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex

    @app.after_request
    def _propagate_request_id(response):
        response.headers.setdefault("X-Request-ID", getattr(g, "request_id", "-"))
        return response

    return app
