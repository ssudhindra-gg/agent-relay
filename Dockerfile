# Agent Relay API.
#
# Normally started together with PostgreSQL by docker-compose.yaml:
#
#   docker compose up -d --build
#
# Run standalone by pointing it at an existing PostgreSQL database:
#
#   docker run -d -p 8000:8000 \
#     -e RELAY_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/relay \
#     agent-relay:local

FROM python:3.11-slim

# Keep in step with the uv version pinned in .github/workflows/ci.yml.
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so code edits don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY *.py dashboard.html ./

RUN useradd --system --uid 10001 --home-dir /app relay
USER relay

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
