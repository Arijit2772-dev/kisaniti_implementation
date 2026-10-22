# Build from the repository root: docker build -f backend/Dockerfile .
FROM python:3.13.9-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY core /app/core
COPY contracts /app/contracts
COPY backend/pyproject.toml backend/uv.lock /app/backend/
WORKDIR /app/backend
RUN uv sync --frozen --no-dev --no-install-project
COPY backend /app/backend
RUN uv sync --frozen --no-dev
ENV PATH=/app/backend/.venv/bin:$PATH
RUN useradd --create-home --uid 10001 app
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=12 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live').status==200 else 1)"
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn kisaniti_api.main:app --host 0.0.0.0 --port 8000"]
