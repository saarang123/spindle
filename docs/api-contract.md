# Spindle API — consumer contract

For agents and orchestrators building on top of Spindle. The implementation reference is [`../api/PLAN.md`](../api/PLAN.md); this doc distills it for someone consuming the API.

> The first concrete consumer is the podcast-this orchestrator. Future consumers (Bridge mini-apps, other content pipelines) read this doc to learn what they get from Spindle.

## TL;DR — what Spindle gives you

You hand Spindle:

- A job `type` (e.g. `audio.tts`, `text.rewrite`)
- A `config_id` (which backend + node should run it)
- An `input` payload

Spindle hands back:

- A `job_id` immediately
- Eventually: a terminal status (`succeeded` / `failed` / `canceled` / `dead_lettered`)
- On success: an `output` dict + one or more `ArtifactMeta` rows
- On demand: artifact bytes via a downloadable URL

You poll for status (or `--watch` via the CLI). You download artifact bytes when needed.

## Endpoints you call

### Submit a job

```
POST /jobs
Content-Type: application/json
Authorization: Bearer <token>   # if SPINDLE_API_AUTH_TOKEN is set

{
  "type": "audio.tts",
  "config_id": "audio-tts-openai-v1",
  "priority": 5,                                 // optional; default 5
  "idempotency_key": "podcast-ep-42-section-3",  // optional; safely retryable
  "timeout_seconds": 120,                        // optional; soft deadline
  "tags": ["podcast"],
  "input": {
    "text": "Backpropagation is the chain rule applied to network weights.",
    "voice": "onyx"
  }
}

→ 201 Created
{
  "job_id": "abcd-1234-…",
  "status": "queued",
  "created_at": "2026-05-22T14:00:00Z"
}
```

Idempotency: if `idempotency_key` matches an existing job, you get `200` (not `201`) with the same `job_id`. Safe to retry POST on network failure.

### Poll a job

```
GET /jobs/{job_id}
→ 200
{
  "id": "abcd-…",
  "type": "audio.tts",
  "status": "running",          // queued | leased | running | succeeded | failed | canceled | dead_lettered
  "config_id": "audio-tts-openai-v1",
  "input":  { … },
  "output": null,               // populated when succeeded
  "error":  null,               // populated when failed
  "artifacts": [],              // populated incrementally
  "retry_count": 0,
  "created_at": "…",
  "queued_at":  "…",
  "leased_at":  "…",
  "started_at": "…",
  "completed_at": null,
  "tags": ["podcast"]
}
```

Terminal statuses: `succeeded`, `failed`, `canceled`, `dead_lettered`. Once a job reaches any of these, no further changes.

### Download an artifact

```
GET /artifacts/{artifact_id}/bytes
→ 200 (binary stream)
Content-Type: audio/wav
```

For `audio.tts`, the produced artifact has `kind="audio"` and `mime_type="audio/wav"`. The `artifacts` array on the job response contains `ArtifactMeta` rows; you call `/bytes` to fetch actual content.

### Cancel a job

```
POST /jobs/{job_id}/cancel
{ "reason": "user aborted" }    // optional
→ 200 { "job_id": "…", "cancel_requested": true, "current_status": "running" }
```

Cooperative. A `queued` job transitions straight to `canceled`. A `running` job flips a flag; the worker checks on the next progress tick and aborts.

## Job types consumed by podcast-this

### `audio.tts`

**Input**:

```json
{
  "text": "…",                            // required
  "voice": "onyx",                        // optional; backend-specific id
  "options": {                            // optional, backend-specific
    "speed": 1.0,                         // openai
    "ref_audio_artifact_id": "uuid",      // f5; reference voice clone
    "ref_text": "…"                       // f5; reference transcript
  }
}
```

**Output on success**:

```json
{
  "duration_seconds": 42.3,
  "voice": "onyx",
  "backend": "openai",
  "char_count": 1234
}
```

**Artifacts**: exactly one, `kind="audio"`, `mime_type="audio/wav"`, sample rate in metadata.

**ModelConfigs available**:

| Config id | Node | Notes |
|---|---|---|
| `audio-tts-openai-v1` | control-node | Hosted; voices: alloy/echo/fable/onyx/nova/shimmer |
| `audio-tts-f5-v1` | gpu-node | Voice cloning; needs `ref_audio_artifact_id` + `ref_text` per call (or worker-configured default) |
| `audio-tts-kokoro-v1` | gpu-node | Lightweight; preset voices (am_michael, af_bella, …) |

See [`../workers/audio_tts/PLAN.md`](../workers/audio_tts/PLAN.md) for full backend details.

### `text.rewrite` (planned)

Spec lands when the worker is built. Tentative shape: accepts `{section_text, prompt_template, target_audience}`, returns rewritten string in `output.rewritten` plus token counts in `output.usage`.

### `audio.stitch` (planned)

Concatenates N WAV artifacts into one MP3 with ID3v2 chapter markers. Tentative shape: input `{artifact_ids: list[uuid], chapter_titles: list[str], metadata: {title, author, …}}`; output one MP3 artifact.

## Polling pattern

Naive but correct:

```python
import httpx, asyncio

TERMINAL = {"succeeded", "failed", "canceled", "dead_lettered"}

async def wait_for_job(job_id: str, *, poll_interval_s: float = 1.0) -> dict:
    async with httpx.AsyncClient(base_url=SPINDLE_API_URL) as client:
        while True:
            r = await client.get(f"/jobs/{job_id}")
            r.raise_for_status()
            job = r.json()
            if job["status"] in TERMINAL:
                return job
            await asyncio.sleep(poll_interval_s)
```

For batched workflows (podcast-this rewriting N sections in parallel): submit N jobs, then `asyncio.gather(*[wait_for_job(jid) for jid in jids])`.

## Retries and dead-lettering

Spindle retries on `retryable=true` failures (model timeouts, transient network errors) up to `max_retries` (default 2). After exhaustion the job becomes `dead_lettered`. Non-retryable failures (`INVALID_INPUT`, `UNSUPPORTED_JOB_TYPE`) go straight to `failed`.

If you submit with `idempotency_key`, a client-side retry won't create a duplicate even if Spindle is internally retrying.

## Error envelope

API errors:

```json
{
  "error": {
    "code": "MODEL_CONFIG_NOT_FOUND",
    "message": "config 'audio-tts-openai-v9' does not exist",
    "request_id": "uuid"
  }
}
```

`error.code` values you'll see on a failed job (from `core.errors.ErrorCode`):

| Code | Retryable | Cause |
|---|---|---|
| `INVALID_INPUT` | no | Input payload didn't match the job-type schema |
| `UNSUPPORTED_JOB_TYPE` | no | Typo in `type` |
| `MODEL_CONFIG_NOT_FOUND` | no | Wrong `config_id` |
| `MODEL_RUNTIME_ERROR` | yes | Backend error (OpenAI 5xx, F5 OOM, …) |
| `WORKER_LOST` | yes | Worker crashed mid-job |
| `DEADLINE_EXCEEDED` | no | Job ran past `deadline_at` |
| `TRANSIENT_NETWORK_ERROR` | yes | Network blip |
| `EXTERNAL_API_TIMEOUT` | yes | OpenAI / Anthropic timed out |
| `ARTIFACT_UPLOAD_FAILED` | yes | MinIO write failed |

## Auth

`SPINDLE_API_AUTH_TOKEN` is optional. If unset, the API is LAN-only and open. If set, attach `Authorization: Bearer <token>` to every request. `/health` is always open.

## Stability of this contract

Until Spindle ships v0.1, treat as draft. Specifically:

- Job-type input/output schemas (`audio.tts`, `text.rewrite`, `audio.stitch`) can change before workers for that type ship.
- The shape of `output.runtime` may grow fields.

Stable as of this doc:

- Endpoint surface: `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /artifacts/{id}/bytes`.
- Lifecycle vocabulary: `queued / leased / running / succeeded / failed / canceled / dead_lettered`.
- Error envelope shape.
- Idempotency semantics.

## See also

- [`../api/PLAN.md`](../api/PLAN.md) — full server-side endpoint reference
- [`../workers/audio_tts/PLAN.md`](../workers/audio_tts/PLAN.md) — `audio.tts` semantics
- [`../core/PLAN.md`](../core/PLAN.md) — Pydantic schemas for `Job`, `ArtifactMeta`, `ErrorPayload`, …
- [`../runtime/PLAN.md`](../runtime/PLAN.md) — how workers are launched (orchestrators don't usually care, but useful for ops)
