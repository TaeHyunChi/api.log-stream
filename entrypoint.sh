#!/bin/sh
set -e

# 사용법:
#   entrypoint.sh serve      기본값 — gunicorn 기동
#   entrypoint.sh <cmd...>   임의 명령 실행
#
# 이 서비스는 DB 를 쓰지 않아 마이그레이션 단계가 없다.

case "${1:-serve}" in
  serve)
    echo "[entrypoint] gunicorn on 0.0.0.0:${PORT:-8246}"
    # 로그 스트림은 응답이 끝나지 않는다. 워커당 스레드 수가 곧 동시 접속 수라
    # 넉넉히 잡고, 타임아웃으로 끊기지 않도록 --timeout 0 을 쓴다.
    # 연결 하나가 k8s 를 읽는 스레드를 하나 더 쓰므로 알림 서비스보다 여유를 둔다.
    exec gunicorn wsgi:app \
      --bind "0.0.0.0:${PORT:-8246}" \
      --workers "${GUNICORN_WORKERS:-1}" \
      --threads "${GUNICORN_THREADS:-64}" \
      --worker-class gthread \
      --timeout 0 \
      --graceful-timeout 10 \
      --access-logfile - \
      --error-logfile -
    ;;
  *)
    exec "$@"
    ;;
esac
