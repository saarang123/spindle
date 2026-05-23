# AGENTS.md — using Spindle from another project / agent

This file is for **anything outside this repo** that wants to use Spindle as a model-serving fabric: agents, orchestrators (e.g. `podcast-this`), Claude / Codex sessions, scripts. Read this and you should know enough to submit jobs, poll for results, download artifacts, and add new model configs.

If you're modifying Spindle itself (the API, runtime, workers), read [`CLAUDE.md`](./CLAUDE.md) or the component-level `PLAN.md` files instead.

---

## TL;DR — what Spindle gives you

You give Spindle:
- A **job type** (e.g. `audio.tts`, `text.rewrite`)
- A **`config_id`** identifying which model + backend + node should run it
- An **input** payload

Spindle gives you back:
- A `job_id` immediately
- Eventually: a terminal `status` (`succeeded` / `failed` / `canceled` / `dead_lettered`)
- On success: an `output` dict + one or more `ArtifactMeta` rows (artifact bytes downloadable by URL)

Spindle handles: durability across restarts, atomic lease/dispatch across N nodes, retry on transient failures, artifact uploads + lineage. It does NOT do workflow / DAG orchestration — that's your job upstream.

---

## Endpoints you call as a consumer

Default URL: `http://<control-node>:8080`. Optional bearer token via `Authorization: Bearer <token>` if `SPINDLE_API_AUTH_TOKEN` is set on the API.

### 1. Submit a job

```http
POST /jobs
Content-Type: application/json

{
  "type": "audio.tts",
  "config_id": "audio-tts-openai-v1",
  "priority": 5,                              // optional, default 5
  "idempotency_key": "podcast-ep-42-section-3", // optional but recommended
  "timeout_seconds": 120,                     // optional
  "tags": ["podcast"],                        // optional
  "input": {
    "text": "Hello world.",
    "voice": "onyx"
  }
}

→ 201 Created
{ "job_id": "uuid", "status": "queued", "created_at": "..." }
```

`idempotency_key` makes retries safe: if you re-POST with the same key, you get the same `job_id` back (200, not 201). Use it for any client-side retry loop.

### 2. Poll a job

```http
GET /jobs/{job_id}

→ 200
{
  "id": "...",
  "status": "running",     // queued | leased | running | succeeded | failed | canceled | dead_lettered
  "output": null,          // populated when succeeded
  "error": null,           // populated when failed
  "artifacts": [],         // populated incrementally
  "assigned_worker_id": "audio-tts-openai-0",
  "created_at": "...", "queued_at": "...", "leased_at": "...",
  "started_at": "...", "completed_at": "..."
}
```

Terminal statuses (don't poll past these): `succeeded`, `failed`, `canceled`, `dead_lettered`.

Naive polling pattern (Python):

```python
import asyncio, httpx

TERMINAL = {"succeeded", "failed", "canceled", "dead_lettered"}

async def wait_for_job(client, job_id, *, interval_s=1.0):
    while True:
        r = await client.get(f"/jobs/{job_id}")
        r.raise_for_status()
        job = r.json()
        if job["status"] in TERMINAL:
            return job
        await asyncio.sleep(interval_s)
```

### 3. Download an artifact

```http
GET /artifacts/{artifact_id}/bytes

→ 200 (binary stream)
Content-Type: audio/wav   (or whatever the artifact's mime is)
```

Artifact IDs come from the `artifacts` array on the job response. The API streams from `ArtifactStore.get(uri)` — you don't need to know where the bytes actually live (could be MinIO on another node).

### 4. Seed a ModelConfig (admin / setup)

Before you can submit a job with `config_id=X`, that ModelConfig must exist. One-time per model:

```http
POST /configs

{
  "id": "audio-tts-openai-v1",
  "name": "OpenAI TTS (tts-1-hd)",
  "version": "v1",
  "job_types": ["audio.tts"],
  "preferred_node": "control-node",
  "runtime_backend": "openai",
  "model_ref": "tts-1-hd",
  "params": {"default_voice": "onyx"},
  "is_active": true
}

→ 200 { "ok": true, "config_id": "audio-tts-openai-v1" }
```

Idempotent upsert. `GET /configs/{id}` to read back.

---

## Job types currently supported

### `audio.tts` — text → 24 kHz mono WAV

**Input:**

```json
{
  "text": "string to synthesize",          // required
  "voice": "onyx",                          // optional, backend-specific
  "options": {                              // optional, backend-specific
    "speed": 1.0,                           // openai only
    "ref_text": "..."                       // f5 only (transcript of voice clone source)
  }
}
```

**Output:**

```json
{
  "voice": "onyx",
  "backend": "openai",
  "char_count": 185,
  "duration_seconds": 11.25
}
```

**Artifact:** one, `kind="audio"`, `mime_type="audio/wav"`. Sample rate 24 kHz, mono, 16-bit PCM.

**Backends + ModelConfigs to use:**

| `config_id` | Backend | Node | Notes |
|---|---|---|---|
| `audio-tts-openai-v1` | OpenAI hosted API | control-node | Voices: alloy, echo, fable, onyx, nova, shimmer. ~0.5 s wall time per ~50 chars. Needs `OPENAI_API_KEY` in worker env. |
| `audio-tts-kokoro-v1` | Kokoro-82M (local) | gpu-node | Voices: am_michael, am_adam, af_bella, af_sarah, bf_emma, bf_isabella, bm_george, bm_lewis. Lightweight model. |
| `audio-tts-f5-v1` | F5-TTS (local) | gpu-node | Voice cloning. Default uses f5-tts bundled reference. Custom via `voice="/path/to/ref.wav"` + `options.ref_text`. |

### `text.rewrite` — *not yet implemented*

Planned shape: `input.text` + `input.prompt_template` → `output.rewritten`. Worker arrives with the next push.

### `audio.stitch` — *not yet implemented*

Planned shape: input `artifact_ids: list[uuid]` + chapter titles → one MP3 artifact with ID3v2 chapter markers.

---

## Adding a new model

Two paths depending on whether the runtime backend already exists.

### Path 1: same backend, new model variant

Just seed a new `ModelConfig` with the same `runtime_backend` and a new `model_ref`. Example: adding `tts-1` alongside `tts-1-hd`:

```bash
curl -X POST http://localhost:8080/configs -H 'Content-Type: application/json' -d '{
  "id": "audio-tts-openai-tts1",
  "name": "OpenAI TTS (tts-1, faster/cheaper)",
  "version": "v1",
  "job_types": ["audio.tts"],
  "preferred_node": "control-node",
  "runtime_backend": "openai",
  "model_ref": "tts-1",
  "params": {"default_voice": "onyx", "model": "tts-1"},
  "is_active": true
}'
```

The existing `OpenAITtsWorker` already reads `params.model` (or falls back to its hardcoded default). No code change. Just submit jobs with `config_id="audio-tts-openai-tts1"`.

### Path 2: new backend entirely

You need a new concrete worker class + entry point. Walk through it:

1. **Add a backend class** under `workers/spindle_workers/audio_tts/backends/<name>.py` implementing `BaseTTS`:

   ```python
   from .base import BaseTTS, Voice

   class NewTTS(BaseTTS):
       sample_rate = 24_000
       def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes: ...
       def list_voices(self) -> list[Voice]: ...
   ```

2. **Add a worker subpackage** under `workers/spindle_workers/audio_tts/<name>/` with three files:

   ```python
   # __init__.py
   from .worker import NewTtsWorker
   __all__ = ["NewTtsWorker"]
   ```

   ```python
   # worker.py
   from ..backends.new import NewTTS
   from ..worker import AudioTtsWorker
   class NewTtsWorker(AudioTtsWorker):
       backend_name = "new"
       def _make_backend(self):
           return NewTTS()
   ```

   ```python
   # __main__.py
   import asyncio
   from .worker import NewTtsWorker
   def main():
       asyncio.run(NewTtsWorker.boot())
   if __name__ == "__main__":
       main()
   ```

3. **Add an entry to the runtime YAML** on whichever node should host it:

   ```yaml
   - name: audio-tts-new
     module: spindle_workers.audio_tts.new
     replicas: 1
     env:
       SPINDLE_WORKER_CONFIG_ID: audio-tts-new-v1
   ```

4. **Seed the ModelConfig** via `POST /configs` (as above).

5. **Restart the runtime** on that node. Worker boots, registers, dispatcher routes `audio.tts` jobs with matching `config_id` to it.

For new job *types* entirely (`image.generate`, `text.rewrite`, etc.) you'd add a sibling top-level package under `workers/spindle_workers/` matching the existing `audio_tts/` shape. The base class is reused (`WorkerBase` in `workers/spindle_workers/base/`), only the worker subclass + backend differ.

---

## Errors

API error envelope:

```json
{ "error": { "code": "MODEL_CONFIG_NOT_FOUND", "message": "config 'foo' …", "request_id": "..." } }
```

Job failures carry the same `ErrorPayload` shape under `job["error"]`. Codes you'll see:

| Code | Retryable? | Cause |
|---|---|---|
| `INVALID_INPUT` | no | Input didn't match the job-type schema |
| `UNSUPPORTED_JOB_TYPE` | no | Typo in `type` |
| `MODEL_CONFIG_NOT_FOUND` | no | Wrong `config_id` or it's not active |
| `MODEL_RUNTIME_ERROR` | yes | Backend failed (OpenAI 5xx, GPU OOM, …) — Spindle retries up to `max_retries` |
| `WORKER_LOST` | yes | Worker crashed mid-job, lease expired |
| `DEADLINE_EXCEEDED` | no | Job ran past `deadline_at` |
| `TRANSIENT_NETWORK_ERROR` | yes | Network blip |

---

## Operational gotchas

- **OpenAI worker needs `OPENAI_API_KEY`** in the runtime's environment. The runtime auto-loads `.env` from the cwd, so adding it to `<spindle-root>/.env` is enough.
- **F5 needs ffmpeg + libavutil** on the system. `apt install ffmpeg libavutil-dev` on Linux; bundled on macOS via brew.
- **Kokoro needs Python 3.12 or 3.13** (its transitive `spacy` doesn't ship 3.14 wheels yet). Use `WorkerSpec.python:` to point at a dedicated venv if your control / GPU node runs newer Python.
- **Mongo + Redis must be reachable from every node**. The runtime's dispatcher talks directly to them — not through the API. Bind Mongo + Redis to the LAN IP (or a Tailscale IP) on the control node and verify from the GPU node with `nc -z <host> 27017` / `6379`.
- **MinIO co-located with its writers**. Workers upload to whichever endpoint `SPINDLE_S3_ENDPOINT` resolves to on their host. The API on the control node should point at the same MinIO so artifact downloads stream correctly across nodes.

---

## See also

- [`docs/api-contract.md`](./docs/api-contract.md) — same content as this file but focused on the API surface
- [`README.md`](./README.md) — repo intro + quickstart
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — how the components fit together
- [`api/PLAN.md`](./api/PLAN.md) — full API endpoint reference (incl. deferred endpoints)
- [`workers/PLAN.md`](./workers/PLAN.md) — WorkerBase contract for adding new worker kinds
- [`runtime/PLAN.md`](./runtime/PLAN.md) — supervisor + dispatcher design
