# `cli/` — `spindle` command-line interface

Thin Typer-based CLI hitting the API. No business logic on the client side; everything goes server-side via HTTP.

Read [`../api/PLAN.md`](../api/PLAN.md) for the endpoint reference. Depends on `core` (for `Settings` and types) and a running API.

## Goal

A `spindle` binary, installable via `uv tool install` from the repo root, that supports the day-to-day workflow of submitting jobs, watching them, and inspecting workers / artifacts.

## Package

`spindle_cli`. Layout:

```
cli/
  pyproject.toml                # entrypoint: spindle = spindle_cli.main:app
  src/spindle_cli/
    __init__.py
    main.py                     # Typer app + command groups
    client.py                   # thin HTTP client wrapping the API
    formatters.py               # rich-based table/JSON output
    commands/
      __init__.py
      submit.py
      status.py
      cancel.py
      workers.py
      artifacts.py
      config.py                 # `spindle config apply <yaml>` to seed ModelConfigs
  tests/
    test_client.py
    test_formatters.py
```

## Commands

### `spindle submit`

```
spindle submit \
  --type text.generate \
  --config qwen-text-v1 \
  --priority 5 \
  --idempotency-key "..." \
  --tag content --tag idea-gen \
  --input '{"prompt": "...", "max_tokens": 1024}' \
  [--input-file payload.json] \
  [--watch]
```

`--input` is a JSON string OR `--input-file` reads from disk. `--watch` polls `GET /jobs/{id}` every 1s and exits when status is terminal.

Output (default rich table):

```
job_id   abcd-1234-...
status   queued
type     text.generate
config   qwen-text-v1
created  2026-04-26T10:14:02Z
```

`--json` emits the raw API response.

### `spindle status`

```
spindle status <job_id> [--watch] [--json] [--with-events]
```

Shows current state. `--with-events` lists the event timeline. `--watch` refreshes every 1s.

### `spindle cancel`

```
spindle cancel <job_id> [--reason "..."]
```

POSTs `/jobs/{id}/cancel`. Prints the cancel-requested confirmation.

### `spindle workers`

```
spindle workers [--node control] [--fresher-than 30s] [--json]
```

Lists registered workers from `GET /workers`. Default rich table:

```
worker_id          node     status   config         loaded_model    used/limit   last_hb
control-text-0     control  busy     qwen-text-v1   qwen-a3b        2/4          3s ago
gpu-image-0        gpu      idle     image-gen-v1   sdxl            0/1          1s ago
```

### `spindle artifacts`

```
spindle artifacts list --job <job_id>          # list artifacts for a job
spindle artifacts get <artifact_id> [-o file]  # download bytes via /artifacts/{id}/bytes
```

### `spindle config apply`

```
spindle config apply path/to/configs.yaml
```

Reads a YAML doc with one or more `ModelConfig` entries and POSTs them to a `/configs` endpoint (TODO: add to api/PLAN.md as part of this work — small admin endpoint that calls `state.upsert_config`). Used to seed configs at deploy time.

### `spindle jobs`

```
spindle jobs list --status queued --type text.generate --limit 50
```

Wraps `GET /jobs?...`.

### `spindle health`

```
spindle health
```

Hits `GET /health`. Returns non-zero exit on non-200.

## Configuration

Reads `SPINDLE_API_URL` and `SPINDLE_API_AUTH_TOKEN` from env. Falls back to `~/.config/spindle/config.toml` if present:

```toml
api_url = "http://control.local:8080"
auth_token = "..."
```

`--api-url` and `--auth-token` flags override.

## Output

Default: human-readable via `rich` (tables + colors). `--json` flag on every command for scripting. `--quiet` suppresses everything except errors.

Exit codes:
- `0` — success
- `1` — generic failure
- `2` — API unreachable
- `3` — auth failed
- `4` — not found
- `5` — invalid input

## Acceptance criteria

- [ ] `uv tool install .` from the repo root makes `spindle` available on the PATH.
- [ ] `spindle submit --type cpu.echo --input '{"message": "hi"}' --watch` round-trips and exits with status `succeeded`.
- [ ] All commands have `--json` and produce valid JSON.
- [ ] `pytest cli/` passes against a stub API (httpx mock or `respx`).
- [ ] `ruff` + `pyright` clean.

## Out of scope

- Interactive TUI (just CLI).
- Local-only commands that bypass the API (no `spindle worker run`, `spindle dispatcher run` — those are `python -m spindle_workers.X` and `python -m spindle_dispatcher`).
- Eval/replay commands (Phase 7 will add `spindle eval ...`).
- Auth flows beyond a static bearer token.
