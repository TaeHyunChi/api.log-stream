# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=wsgi.py \
    PORT=8246

# 비루트 실행 (k3s securityContext와 맞춘다)
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=1000:1000 wsgi.py entrypoint.sh ./
COPY --chown=1000:1000 app ./app
# DB 를 쓰지 않아 migrations 가 없다 — 로그의 원본은 k8s 이고 이 서비스는 중계만 한다.
RUN chmod +x /app/entrypoint.sh

USER 1000:1000
EXPOSE 8246

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('PORT','8246')}/healthz\").read()" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
