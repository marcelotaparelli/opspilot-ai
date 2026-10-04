FROM ghcr.io/astral-sh/uv:0.7.3 AS uv
FROM python:3.12.10-slim-bookworm AS build
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
# Missing uv.lock is an explicit acceptance blocker (see docs/CHECKS.md).
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.10-slim-bookworm
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "opspilot.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "15"]
