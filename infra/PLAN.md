# `infra/` — Docker compose, Dockerfiles, Makefile, repo bootstrap

Ties the system together. Compose runs the **control-node services**: Mongo, Redis, ClickHouse (Phase 8), API, control-side dispatcher. GPU-node services run native (no Docker — CUDA/MPS passthrough is too painful for v0).

Depends on phases 0–4 to actually start anything; this PLAN.md describes what to build alongside them.

## Goal

1. `docker/compose.yaml` for the control node.
2. `docker/Dockerfile.api` and `docker/Dockerfile.dispatcher`.
3. `Makefile` at repo root with the daily-driver targets.
4. `pyproject.toml` at repo root configuring the uv workspace and shared tooling.
5. GitHub Actions workflow `.github/workflows/ci.yml` running lint + type + test.
6. Pre-commit config.
7. Bootstrap script for seeding `ModelConfig` entries.

## Layout

```
infra/
  PLAN.md                       # this file
  seed/
    configs/
      cpu_echo.yaml             # seed config for cpu.echo
      qwen_text.yaml.example
      image_gen.yaml.example
      video_i2v.yaml.example

docker/
  compose.yaml
  Dockerfile.api
  Dockerfile.dispatcher
  mongo/
    init/                       # mongo init scripts (create user, indexes — or leave to app)
  redis/
    redis.conf                  # bind, requirepass file directive, persistence
  clickhouse/
    schema.sql                  # phase 8
    materialized_views.sql      # phase 8

.github/
  workflows/
    ci.yml

Makefile
pyproject.toml                  # workspace root
.pre-commit-config.yaml
```

## `pyproject.toml` (workspace root)

```toml
[project]
name = "spindle"
version = "0.1.0"
description = "Local job fabric for heterogeneous generative ML workloads."
requires-python = ">=3.12"

[tool.uv]
package = false                 # root is a workspace, not an installable

[tool.uv.workspace]
members = ["core", "api", "dispatcher", "workers", "cli"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "N", "S", "ASYNC", "RUF"]
ignore = ["S101"]               # asserts allowed in tests

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S", "ASYNC"]

[tool.ruff.format]
quote-style = "double"

[tool.pyright]
include = ["core/src", "api/src", "runtime/src", "workers/src", "cli/src"]
strict = ["core/src"]
pythonVersion = "3.12"
typeCheckingMode = "standard"

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
  "integration: requires docker / external services",
]
```

Each subpackage (`core/pyproject.toml`, etc.) declares its own `[project]` with name `spindle-core`, `spindle-api`, etc., and lists its deps. uv resolves them all together.

## `docker/compose.yaml`

```yaml
name: spindle

services:
  mongo:
    image: mongo:7
    restart: unless-stopped
    ports: ["${SPINDLE_MONGO_BIND:-0.0.0.0:27017}:27017"]
    environment:
      MONGO_INITDB_ROOT_USERNAME: ${SPINDLE_MONGO_USER:-spindle}
      MONGO_INITDB_ROOT_PASSWORD_FILE: /run/secrets/mongo_pw
      MONGO_INITDB_DATABASE: ${SPINDLE_MONGO_DB:-spindle}
    volumes:
      - mongo-data:/data/db
    secrets: [mongo_pw]
    healthcheck:
      test: ["CMD", "mongosh", "--quiet", "--eval", "db.runCommand({ping:1}).ok"]
      interval: 10s
      timeout: 3s
      retries: 5

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    ports: ["${SPINDLE_REDIS_BIND:-0.0.0.0:6379}:6379"]
    command: ["redis-server", "/usr/local/etc/redis/redis.conf"]
    volumes:
      - redis-data:/data
      - ./redis/redis.conf:/usr/local/etc/redis/redis.conf:ro
    secrets: [redis_pw]
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "$$(cat /run/secrets/redis_pw)", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  clickhouse:                                      # phase 8
    image: clickhouse/clickhouse-server:24.8-alpine
    restart: unless-stopped
    ports: ["${SPINDLE_CLICKHOUSE_BIND:-0.0.0.0:8123}:8123"]
    volumes:
      - clickhouse-data:/var/lib/clickhouse
      - ./clickhouse/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro
    profiles: [telemetry]                         # opt-in until phase 8

  api:
    build:
      context: ..
      dockerfile: docker/Dockerfile.api
    restart: unless-stopped
    ports: ["${SPINDLE_API_PORT:-8080}:8080"]
    env_file: [../.env]
    depends_on:
      mongo: { condition: service_healthy }
      redis: { condition: service_healthy }
    command: ["uv", "run", "uvicorn", "spindle_api.main:app",
              "--host", "0.0.0.0", "--port", "8080"]

  dispatcher:
    build:
      context: ..
      dockerfile: docker/Dockerfile.dispatcher
    restart: unless-stopped
    env_file: [../.env]
    depends_on:
      api: { condition: service_started }
    volumes:
      - /tmp/spindle-workers:/tmp/spindle-workers       # registry directory
      - /tmp:/tmp                                        # for ipc sockets
    command: ["uv", "run", "python", "-m", "spindle_dispatcher"]

volumes:
  mongo-data:
  redis-data:
  clickhouse-data:

secrets:
  mongo_pw:
    file: ../secrets/mongo_pw
  redis_pw:
    file: ../secrets/redis_pw
```

`secrets/mongo_pw` and `secrets/redis_pw` are gitignored. Bootstrap creates them with `openssl rand -base64 32`.

## Dockerfiles

`docker/Dockerfile.api`:

```dockerfile
FROM python:3.12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY core/ core/
COPY api/ api/
RUN uv sync --frozen --package spindle-api --no-dev

ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["uvicorn", "spindle_api.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

`docker/Dockerfile.dispatcher`: same shape, swap `api` → `dispatcher`, no `EXPOSE`.

## `Makefile`

```makefile
.PHONY: help up down logs ps test lint format typecheck worker bootstrap clean

help:
	@grep -E '^[a-z_-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "%-15s %s\n", $$1, $$2}'

bootstrap:                  ## generate secrets, copy .env.example, install deps
	@mkdir -p secrets
	@[ -f secrets/mongo_pw ] || openssl rand -base64 32 > secrets/mongo_pw
	@[ -f secrets/redis_pw ] || openssl rand -base64 32 > secrets/redis_pw
	@[ -f .env ] || cp .env.example .env
	uv sync --all-packages

up:                         ## start control-plane services
	docker compose -f docker/compose.yaml up -d
	@echo "API → http://localhost:$${SPINDLE_API_PORT:-8080}"

down:                       ## stop services (preserves volumes)
	docker compose -f docker/compose.yaml down

logs:                       ## tail compose logs
	docker compose -f docker/compose.yaml logs -f --tail=200

ps:                         ## list compose services
	docker compose -f docker/compose.yaml ps

worker:                     ## run cpu_echo worker on host
	uv run python -m spindle_workers.cpu_echo

dispatcher:                 ## run dispatcher on host (alternative to docker)
	uv run python -m spindle_dispatcher

api:                        ## run api on host
	uv run uvicorn spindle_api.main:app --reload

test:                       ## run all tests (unit only — no docker)
	uv run pytest -m "not integration"

test-int:                   ## run integration tests (needs docker)
	uv run pytest -m integration

lint:                       ## ruff check
	uv run ruff check .

format:                     ## ruff format + fix
	uv run ruff format .
	uv run ruff check --fix .

typecheck:                  ## pyright
	uv run pyright

ci:                         ## what CI runs
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
	uv run pytest -m "not integration"

clean:                      ## remove caches + venv
	rm -rf .venv .pytest_cache .ruff_cache .pyright_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
```

## GitHub Actions

`.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint-type-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with: { enable-cache: true }
      - run: uv sync --all-packages
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pyright
      - run: uv run pytest -m "not integration"

  integration:
    runs-on: ubuntu-latest
    services:
      mongo: { image: mongo:7, ports: ["27017:27017"] }
      redis: { image: redis:7-alpine, ports: ["6379:6379"] }
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with: { enable-cache: true }
      - run: uv sync --all-packages
      - run: uv run pytest -m integration
        env:
          SPINDLE_MONGO_URL: mongodb://localhost:27017
          SPINDLE_REDIS_URL: redis://localhost:6379/0
```

## Pre-commit

`.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/RobertCraigie/pyright-python
    rev: v1.1.380
    hooks:
      - id: pyright
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
```

## Bootstrap

`make bootstrap` should be runnable on a fresh clone and produce a working dev env (modulo Docker being installed). Steps:
1. Generate secrets.
2. Copy `.env.example` → `.env` if missing.
3. `uv sync --all-packages`.
4. Print next-step instructions ("now run `make up && make worker`").

## GitHub repo init (when ready)

```bash
gh repo create spindle --private --source=. --remote=origin --description="local job fabric for heterogeneous generative ML"
git add .
git commit -m "initial scaffold"
git push -u origin main
```

Keep `--private` until the project is in shape we want to show. Drop to `--public` later.

## Acceptance criteria

- [ ] `make bootstrap && make up` brings up Mongo + Redis + API on a fresh clone.
- [ ] `make worker` starts the `cpu_echo` worker on the host and it registers via the API.
- [ ] `spindle submit --type cpu.echo --input '{"message":"hi"}' --watch` round-trips to `succeeded`.
- [ ] CI passes on a clean branch.
- [ ] No secrets committed (verified by detect-secrets).

## Out of scope

- Production-grade compose (e.g., resource limits, log rotation, observability stack).
- Kubernetes / Helm.
- Cross-platform Dockerfiles for ARM/x86 multi-arch (build on dev machine; `--platform` later).
- Auto-scaling of workers.
