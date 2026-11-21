# syntax=docker/dockerfile:1

# ---- build: resolve the locked dependencies into a virtualenv ----
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra postgres --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres --no-editable

# ---- runtime ----
FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="bluetti-grid-bot" \
      org.opencontainers.image.description="Telegram bot that reports grid power outages using a BLUETTI power station" \
      org.opencontainers.image.source="https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# Read-only root FS friendly: no bytecode writes, only STATE_DIR is writable.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data \
    HTTP_PORT=8080

RUN useradd --system --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin bot \
    && mkdir /data && chown 10001:10001 /data && chmod 700 /data
COPY --from=build /opt/venv /opt/venv

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('HTTP_PORT', '8080'), timeout=4)"]
ENTRYPOINT ["bluetti-grid-bot"]
CMD ["run"]
