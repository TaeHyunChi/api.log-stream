"""OpenAPI ErrorResponse(code, message) 형태로 모든 오류를 통일한다."""

import logging

from werkzeug.exceptions import HTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    """핸들러에서 직접 던지는 오류."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class BadRequest(ApiError):
    def __init__(self, message: str, code: str = "BAD_REQUEST"):
        super().__init__(400, code, message)


class Unauthorized(ApiError):
    def __init__(self, message: str = "인증이 필요합니다.", code: str = "UNAUTHORIZED"):
        super().__init__(401, code, message)


class NotFound(ApiError):
    def __init__(self, message: str = "대상을 찾을 수 없습니다.", code: str = "NOT_FOUND"):
        super().__init__(404, code, message)


class Forbidden(ApiError):
    """스펙의 403 — 멤버 신청 승인/반려는 소유자·관리자 멤버만 할 수 있다."""

    def __init__(self, message: str = "권한이 없습니다.", code: str = "FORBIDDEN"):
        super().__init__(403, code, message)


class Conflict(ApiError):
    def __init__(self, message: str, code: str = "CONFLICT"):
        super().__init__(409, code, message)


class BadGateway(ApiError):
    """이 서비스가 의존하는 외부 시스템(Gitea / user-service)이 실패했다.

    요청 자체는 정상이라 4xx 가 아니고, 이 서비스의 버그도 아니라 500 도 아니다.
    호출측이 "잠시 뒤 다시" 를 판단할 수 있도록 502 로 구분한다.
    """

    def __init__(self, message: str, code: str = "BAD_GATEWAY"):
        super().__init__(502, code, message)


_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    415: "UNSUPPORTED_MEDIA_TYPE",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
}


def register_error_handlers(app):
    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        return {"code": exc.code, "message": exc.message}, exc.status

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        status = exc.code or 500
        code = _STATUS_CODES.get(status, "HTTP_ERROR")
        return {"code": code, "message": exc.description or exc.name}, status

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        log.exception("unhandled error: %s", exc)
        return {"code": "INTERNAL_ERROR", "message": "내부 서버 오류가 발생했습니다."}, 500
