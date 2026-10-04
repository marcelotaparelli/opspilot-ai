#!/bin/sh
set -eu
uv lock --check
uv sync --locked
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest -m 'not integration'
uv run --locked pytest -m integration
docker compose config --quiet
docker build --tag opspilot-ai:phase1 .
