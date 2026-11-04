# The ingestion worker kn-ingest (and the software node, for the stack check). Build from the repository root:
#   docker build -f backend/ingestion/Dockerfile .
# Only source and lock files are copied: certificates and passwords reach the container by read-only mounts (compose.yaml).
FROM python:3.13.9-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY core /app/core
COPY contracts /app/contracts
COPY backend/ingestion/pyproject.toml backend/ingestion/uv.lock backend/ingestion/README.md /app/backend/ingestion/
WORKDIR /app/backend/ingestion
RUN uv sync --frozen --no-dev --extra postgres --no-install-project
COPY backend/ingestion/src /app/backend/ingestion/src
RUN uv sync --frozen --no-dev --extra postgres
ENV PATH=/app/backend/ingestion/.venv/bin:$PATH
RUN useradd --create-home --uid 10001 app
USER app
CMD ["python", "-m", "kisaniti_ingestion.worker"]
