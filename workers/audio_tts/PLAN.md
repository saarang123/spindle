# `workers/audio_tts/` — text-to-speech worker

Spindle's first real (non-echo) worker. Consumes `audio.tts` jobs and produces 24 kHz mono WAV artifacts. Backed by a pluggable TTS layer with three backends: OpenAI (hosted, CPU-pool friendly), F5-TTS (local, GPU, voice cloning), and Kokoro (local, lightweight, preset voices).

Read [`../PLAN.md`](../PLAN.md) for the `WorkerBase` design; this doc covers only the audio_tts-specific bits.

## Goal

1. One worker class (`AudioTtsWorker`) that handles `audio.tts` jobs regardless of backend.
2. Three backend implementations (`OpenAITTS`, `F5TTS`, `KokoroTTS`) behind a shared `BaseTTS` interface.
3. Backend selected per-process by `SPINDLE_TTS_BACKEND` env var. Each backend = one ModelConfig.
4. Migrate the existing TTS code from podcast-this (see `~/Documents/podcast-this/cli/podcast/tts/`); the interface is unchanged.

## Package

Lives inside `workers/`.

```
workers/src/spindle_workers/audio_tts/
  __init__.py
  worker.py             # AudioTtsWorker(WorkerBase)
  main.py               # `python -m spindle_workers.audio_tts`
  backends/
    __init__.py         # lazy backend loader keyed by SPINDLE_TTS_BACKEND
    base.py             # BaseTTS, Voice, SAMPLE_RATE
    _util.py            # chunk_text, concat_wav, samples_to_wav
    openai.py           # OpenAITTS
    f5.py               # F5TTS
    kokoro.py           # KokoroTTS
```

Optional dependency extras in `workers/pyproject.toml`:

```toml
[project.optional-dependencies]
audio_tts        = ["openai>=1.40"]
audio_tts_f5     = ["openai>=1.40", "f5-tts", "numpy>=1.24"]
audio_tts_kokoro = ["openai>=1.40", "kokoro>=0.7", "numpy>=1.24"]
audio_tts_all    = ["openai>=1.40", "f5-tts", "kokoro>=0.7", "numpy>=1.24"]
```

Each hosting node installs only the extras it needs.

## `audio.tts` job type

### Input

```json
{
  "text": "string to synthesize",
  "voice": "onyx",                       // optional; backend-specific id; null = backend default
  "options": {                           // optional, backend-specific
    "speed": 1.0,                        // openai
    "ref_audio_artifact_id": "uuid",     // f5; reference voice clone source
    "ref_text": "..."                    // f5; transcript of the reference
  }
}
```

### Output

```json
{
  "duration_seconds": 42.3,
  "voice": "onyx",
  "backend": "openai",
  "char_count": 1234
}
```

Plus exactly one artifact:

```python
ArtifactMeta(
    kind=ArtifactKind.AUDIO,
    mime_type="audio/wav",
    duration_seconds=42.3,
    metadata={
        "sample_rate": 24000,
        "channels": 1,
        "backend": "openai",
        "voice": "onyx",
    },
)
```

WAV bytes upload to `ArtifactStore` at key `{job_id}/audio.wav`.

## ModelConfig examples

One ModelConfig per backend. Seeded via `spindle config apply`.

```yaml
# configs/seed/audio_tts.yaml
configs:
  - id: audio-tts-openai-v1
    name: "OpenAI TTS (tts-1-hd)"
    version: "v1"
    job_types: ["audio.tts"]
    preferred_node: control-node
    runtime_backend: "openai"
    model_ref: "tts-1-hd"
    params: { default_voice: "onyx" }
    is_active: true

  - id: audio-tts-f5-v1
    name: "F5-TTS local (default)"
    version: "v1"
    job_types: ["audio.tts"]
    preferred_node: gpu-node
    runtime_backend: "f5"
    model_ref: "F5-TTS"
    params: {}
    is_active: true

  - id: audio-tts-kokoro-v1
    name: "Kokoro-82M local"
    version: "v1"
    job_types: ["audio.tts"]
    preferred_node: gpu-node
    runtime_backend: "kokoro"
    model_ref: "kokoro-82m"
    params: { lang_code: "a", default_voice: "am_michael" }
    is_active: true
```

## Worker process structure

One process per backend, per Spindle's "one worker = one config" rule:

| Backend | Process count | Concurrency limit | Notes |
|---|---|---|---|
| `openai` | 1 per replica (default 4–5 replicas) | 8 | Pure I/O; asyncio multiplexes safely. Replicas raise the parallel ceiling. |
| `f5` | 1 | 1 | One model in GPU VRAM; inference is GPU-serial. |
| `kokoro` | 1 | 1 | Same; lightweight enough to not warrant replication. |

Replica count + concurrency are set at deploy time via the runtime supervisor's YAML (see [`../../runtime/PLAN.md`](../../runtime/PLAN.md)) and via `WorkerConfig.concurrency_limit` (env or per-worker YAML).

## `AudioTtsWorker`

```python
class AudioTtsWorker(WorkerBase):
    capabilities = ["audio.tts"]

    def __init__(self, config: WorkerConfig, deps: WorkerDeps):
        super().__init__(config, deps)
        backend_name = os.environ["SPINDLE_TTS_BACKEND"]
        self._backend_name = backend_name
        self._tts = self._load_backend(backend_name, config.params)

    @staticmethod
    def _load_backend(name: str, params: dict) -> "BaseTTS":
        match name:
            case "openai":
                from .backends.openai import OpenAITTS
                return OpenAITTS(model=params.get("model", "tts-1-hd"))
            case "f5":
                from .backends.f5 import F5TTS
                return F5TTS(
                    model_name=params.get("model", "F5-TTS"),
                    default_ref_audio=params.get("default_ref_audio"),
                    default_ref_text=params.get("default_ref_text"),
                )
            case "kokoro":
                from .backends.kokoro import KokoroTTS
                return KokoroTTS(lang_code=params.get("lang_code", "a"))
            case other:
                raise ValueError(f"Unknown TTS backend: {other!r}")

    async def execute(self, job: Job, ctx: JobContext) -> JobResult:
        text = job.input["text"]
        voice = job.input.get("voice")
        options = job.input.get("options", {})

        ctx.cancel.raise_if_set()
        await ctx.progress.report(phase="synthesizing", message=f"{len(text)} chars")

        # Run synthesize in a thread so the event loop keeps heartbeats + cancel-poll alive.
        wav_bytes = await asyncio.to_thread(
            self._tts.synthesize, text, voice, **options
        )

        ctx.cancel.raise_if_set()
        duration_s = _wav_duration_seconds(wav_bytes)

        artifact = await ctx.artifacts.write(
            key="audio.wav",
            data=wav_bytes,
            kind=ArtifactKind.AUDIO,
            mime_type="audio/wav",
            duration_seconds=duration_s,
            metadata={
                "sample_rate": self._tts.sample_rate,
                "channels": 1,
                "backend": self._backend_name,
                "voice": voice or "default",
            },
        )

        return JobResult(
            output={
                "duration_seconds": duration_s,
                "voice": voice or "default",
                "backend": self._backend_name,
                "char_count": len(text),
            },
            artifacts=[artifact],
            runtime={"execution_ms": int(duration_s * 1000)},
        )
```

## Migration from podcast-this

The `BaseTTS` + `OpenAITTS` + `F5TTS` + `KokoroTTS` code currently lives at `~/Documents/podcast-this/cli/podcast/tts/`. Migration steps:

1. Move `base.py`, `_util.py`, `openai.py`, `f5.py`, `kokoro.py` into `spindle/workers/src/spindle_workers/audio_tts/backends/`.
2. Adjust relative imports (`from .base import ...` becomes `from ..base import ...` in some places; double-check).
3. Delete `podcast-this/cli/podcast/tts/` after the move. Update `podcast-this/cli/pyproject.toml` to drop the openai dep (it now lives in Spindle).
4. Update podcast-this README pluggability section to point at Spindle.

The `BaseTTS` interface (and the three backend classes) is unchanged. Only the calling layer changes.

## Cancellation behavior

Mid-synthesis cancellation is best-effort:
- Between `chunk_text` chunks (OpenAI): cancellation can interrupt before the next chunk request.
- Inside a single F5 / Kokoro inference call: no interruption (the backend's `infer` runs to completion). The next call after a cancel will respect the flag.

This is a known limitation. Acceptable for short sections (< 60s of audio). If long synthesis becomes common, add a chunked-synthesis path that returns control between chunks.

## Acceptance criteria

- [ ] `uv run pytest workers/tests/test_audio_tts.py` passes (mocked OpenAI client; F5 / Kokoro tests skipped without their extras installed).
- [ ] `spindle submit --type audio.tts --config audio-tts-openai-v1 --input '{"text":"hello"}' --watch` round-trips and exits `succeeded` with an audio artifact in MinIO that decodes as a valid 24 kHz mono WAV.
- [ ] All three backends can be started under the runtime supervisor without colliding (separate IPC sockets, separate registry entries).
- [ ] Missing or mismatched `SPINDLE_TTS_BACKEND` / `SPINDLE_WORKER_CONFIG_ID` raises a clear error before the worker connects to the API.
- [ ] Cancellation between chunks works for `openai`; between calls for `f5` / `kokoro` (documented limitation).
- [ ] `ruff` + `pyright` clean.

## Out of scope (this worker)

- Streaming TTS (partial audio while still synthesizing).
- ElevenLabs / Cartesia / other hosted backends — add as new backend files when needed.
- Audio post-processing (normalization, EQ). The pipeline's `audio.stitch` worker handles concat + mp3 encode later.
- Voice-cloning UX beyond `ref_audio_artifact_id` in the input. A configurator UI for managing reference voices lives elsewhere.
