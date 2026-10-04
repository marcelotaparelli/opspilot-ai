# Release image. Base images are pinned by digest; Python dependencies by uv.lock.
FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM python:3.12.15-slim-trixie@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d AS build
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
# Install the committed dependency graph only (no dev tools); fail on manifest/lock drift.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.15-slim-trixie@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d
# Debian security updates published after the pinned base (exact versions are recorded in
# the release SBOM). pip is not needed at runtime and is removed to shrink the attack surface.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y pip \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
# Exec form: uvicorn is PID 1 and receives SIGTERM for graceful shutdown.
STOPSIGNAL SIGTERM
CMD ["uvicorn", "opspilot.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-server-header", "--timeout-graceful-shutdown", "15"]
