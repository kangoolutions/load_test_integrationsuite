# syntax=docker/dockerfile:1.10
ARG PY=3.12

FROM python:${PY}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Abhängigkeiten vor dem Quellcode auflösen, damit der Layer-Cache bei Code-Änderungen hält.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY static ./static
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM python:${PY}-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH=/app/.venv/bin:$PATH \
    CPILOAD_DATA_DIR=/data \
    CPILOAD_DB_PATH=/state/cpiload.db \
    CPILOAD_STATIC_DIR=/app/static
RUN groupadd -g 10001 app && useradd -u 10001 -g app -M -s /usr/sbin/nologin app \
    && mkdir -p /data /state && chown 10001:10001 /state
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/static /app/static
WORKDIR /app
USER 10001:10001
EXPOSE 8080
VOLUME ["/state"]
# Kein curl im slim-Image – der Healthcheck kommt aus der Standardbibliothek.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=4 \
  CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health/live',timeout=2).status==200 else 1)"]
# Ein Worker: Laufzustand liegt im Prozess.
CMD ["uvicorn", "cpiload.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--timeout-graceful-shutdown", "20"]
