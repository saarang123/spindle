# `dispatcher/` — host-level scheduler

One process per node. Reserves jobs from per-config Redis streams, makes scheduling decisions, leases atomically via `StateStore`, and dispatches to local workers via Unix socket. Also runs the lease sweeper and cancellation propagator.

Read [`../ARCHITECTURE.md`](../ARCHITECTURE.md) sections 3 (Components → dispatcher), 4 (Data flow), and 6 (Key decisions). Depends on [`../core/PLAN.md`](../core/PLAN.md) (Phase 1 must be done).

## Goal

Run as `python -m spindle_dispatcher` on each node. The dispatcher knows which `ModelConfig`s belong to its node (from the config registry, filtered by `preferred_node == settings.node`, optionally narrowed by `SPINDLE_DISPATCHER_CONFIGS`). It loops: reserve from those configs' queues, score, lease, dispatch to local worker. Independently, it runs background sweepers.

## Package

`spindle_dispatcher`. Layout:

```
dispatcher/
  pyproject.toml
  src/spindle_dispatcher/
    __init__.py
    main.py                     # entrypoint (`python -m spindle_dispatcher`)
    dispatcher.py               # Dispatcher class — orchestrates the tick + sweepers
    tick.py                     # ReserveScoreLeaseDispatch one-pass logic
    scoring.py                  # score_job_for_worker(job, worker, ctx) → float
    sweepers/
      __init__.py
      lease_sweeper.py          # find_expired_leases → requeue or dead-letter
      deadline_sweeper.py       # find_overdue_jobs → fail with DEADLINE_EXCEEDED
      recovery.py               # startup: re-enqueue queued-but-not-in-queue jobs
      cancel_propagator.py      # find cancel_requested → signal worker IPC
    workers_local/
      __init__.py
      registry.py               # in-memory map of config_id → ipc_socket
      ipc_client.py             # Unix-socket JSON RPC client
    config_loader.py            # load model configs + filter to this node
    backoff.py                  # retry backoff policy
  tests/
    test_tick.py                # unit, mocked store + queue
    test_scoring.py
    test_lease_sweeper.py
    test_recovery.py
    test_ipc_client.py          # against a stub Unix-socket server
    conftest.py
```

## Tick loop

The core loop runs every `SPINDLE_DISPATCHER_TICK_MS` (default 200ms) or fires immediately when a previous tick reserved a job (so a busy host doesn't sleep when there's work):

```python
async def tick(ctx: DispatcherContext) -> bool:
    """Returns True if a job was dispatched this tick."""
    if ctx.host_in_flight() >= ctx.host_concurrency_cap:
        return False

    active_configs = await ctx.list_active_configs_for_this_node()
    if not active_configs:
        return False

    reserved = await ctx.queue.reserve(
        config_ids=[c.id for c in active_configs],
        consumer=ctx.dispatcher_id,
        count=4,                     # batch a few; pick the best
        block_ms=ctx.tick_block_ms,
    )
    if not reserved:
        return False

    # Resolve to Job rows (some may have been canceled / superseded)
    jobs = []
    for r in reserved:
        job = await ctx.state.get_job(r.job_id)
        if job is None or job.status != JobStatus.QUEUED:
            await ctx.queue.ack(r.config_id, r.reservation_id)  # drop, can't run
            continue
        jobs.append((job, r))

    if not jobs:
        return False

    # Score each (job, available_worker) pair
    candidates = []
    for job, reservation in jobs:
        worker = ctx.local_workers.find_available_for(job.config_id)
        if worker is None:
            await ctx.queue.nack(reservation.config_id, reservation.reservation_id)
            continue
        score = score_job_for_worker(job, worker, ctx)
        candidates.append((score, job, worker, reservation))

    if not candidates:
        return False

    candidates.sort(key=lambda c: -c[0])
    score, job, worker, reservation = candidates[0]

    lease = Lease(
        id=uuid4(),
        job_id=job.id,
        worker_id=worker.id,
        expires_at=now() + timedelta(seconds=ctx.lease_ttl_seconds),
    )
    leased = await ctx.state.acquire_lease(job.id, worker.id, lease.id, lease.expires_at)
    if leased is None:
        # Race: someone else got it. Nack and try next tick.
        await ctx.queue.nack(reservation.config_id, reservation.reservation_id)
        return False

    try:
        await ctx.local_workers.dispatch(worker, leased, lease)
    except IpcError:
        # Worker socket is dead; revert lease, nack queue.
        await ctx.state.transition(
            job.id, expected_from=JobStatus.LEASED, to=JobStatus.QUEUED,
            patch={"assigned_worker_id": None, "lease_id": None, "lease_expires_at": None},
        )
        await ctx.queue.nack(reservation.config_id, reservation.reservation_id)
        ctx.local_workers.mark_unhealthy(worker.id)
        return False

    await ctx.queue.ack(reservation.config_id, reservation.reservation_id)
    # release any other reservations we batched but didn't use
    for _, _, _, r in candidates[1:]:
        await ctx.queue.nack(r.config_id, r.reservation_id)
    return True
```

The dispatcher process owns one event loop. The tick runs serially per loop; concurrency comes from many local workers each handling their own jobs after dispatch.

## Scoring function

Phase-3 simple version. Add bells later.

```python
def score_job_for_worker(job: Job, worker: Worker, ctx) -> float:
    score = 0.0
    score += job.priority * 10                        # priority dominates
    if worker.config_id == job.config_id:
        score += 50                                    # warm config bonus
    if job.requested_node and worker.node == job.requested_node:
        score += 20
    if worker.last_error is not None and ctx.recently(worker.last_error_at, seconds=60):
        score -= 30
    score -= worker.concurrency_used * 5              # prefer less-busy worker
    return score
```

Tunable later: latency-weighted, cost-weighted, fairness across configs.

## Local worker registry + IPC

Workers register themselves with the dispatcher at startup by writing a tiny pidfile-and-socket descriptor to a known directory (default `/var/run/spindle/workers/` or `/tmp/spindle-workers/`):

```
/tmp/spindle-workers/control-text-0.json
{
  "worker_id": "control-text-0",
  "config_id": "qwen-text-v1",
  "ipc_socket": "/tmp/spindle-worker-text-0.sock",
  "concurrency_limit": 4,
  "started_at": "..."
}
```

Dispatcher tails this directory at startup and via inotify (or polling fallback) for hot-add/remove. The registry is in-memory; persistence comes from Mongo via worker heartbeats which the API records.

**IPC protocol** (Unix socket, length-prefixed JSON):

```jsonc
// dispatcher → worker
{ "op": "run", "job_id": "...", "lease_id": "...", "input": {...}, "config_id": "...",
  "deadline_at": "...", "lease_expires_at": "..." }

// worker → dispatcher (immediate ack — confirms acceptance, nothing more)
{ "ok": true }

// dispatcher → worker (cancellation)
{ "op": "cancel", "job_id": "..." }

// worker → dispatcher
{ "ok": true }
```

After the immediate `ok`, the worker reports lifecycle to the API directly (start/progress/complete/fail), not back to the dispatcher. The dispatcher's only job is hand-off; it does not track per-job execution.

## Sweepers

Each sweeper runs in its own asyncio task on a fixed cadence. Independent of the tick loop.

### Lease sweeper

Every 5 seconds:

```python
expired = await state.find_expired_leases(now=now(), limit=50)
for job in expired:
    if job.retry_count < job.max_retries:
        await state.transition(
            job.id,
            expected_from=[JobStatus.LEASED, JobStatus.RUNNING],
            to=JobStatus.QUEUED,
            patch={"assigned_worker_id": None, "lease_id": None,
                   "lease_expires_at": None, "retry_count": job.retry_count + 1},
        )
        # backoff: enqueue with delay computed by backoff.next_delay(retry_count)
        delay_seconds = backoff_policy.next_delay(job.retry_count + 1)
        await schedule_enqueue(job.config_id, job.id, after_seconds=delay_seconds, priority=job.priority)
        await state.record_event(JobEvent(type=JobEventType.RETRYING, job_id=job.id, ...))
    else:
        await state.transition(
            job.id, expected_from=[JobStatus.LEASED, JobStatus.RUNNING],
            to=JobStatus.DEAD_LETTERED,
            patch={"error": ErrorPayload(code=ErrorCode.WORKER_LOST, message="lease expired",
                                         retryable=False)},
        )
```

`schedule_enqueue` with delay: for v0, just record a future-`queued_at` and let the dispatcher skip jobs with `queued_at > now()`. Or implement properly via Redis sorted-set delayed queue if needed. **Phase 3 v0: ignore the delay, immediate re-enqueue.** Note this in the test.

### Deadline sweeper

Every 5 seconds:

```python
overdue = await state.find_overdue_jobs(now=now(), limit=50)
for job in overdue:
    # Tell the worker to abort, then mark failed.
    await cancel_propagator.signal(job)  # best-effort IPC cancel
    await state.transition(
        job.id, expected_from=[JobStatus.LEASED, JobStatus.RUNNING],
        to=JobStatus.FAILED,
        patch={"error": ErrorPayload(code=ErrorCode.DEADLINE_EXCEEDED, retryable=False, ...)}
    )
```

### Cancel propagator

Every 2 seconds, scan jobs the dispatcher's local workers are holding (`assigned_worker_id` in our worker set) where `cancel_requested=true`:

```python
for job in cancellable_jobs:
    worker = local_workers.get(job.assigned_worker_id)
    if worker:
        await ipc_client.send(worker.ipc_socket, {"op": "cancel", "job_id": str(job.id)})
```

Workers also poll the API's `cancel_status` endpoint as a backup; the IPC signal is the fast path.

### Recovery sweep (startup only)

Once at boot:
- Find jobs with `status=queued` whose `config_id` is on this node and which are NOT in the queue (depth-by-id check or just re-enqueue idempotently with a marker).
- Re-enqueue them so they get picked up.

Mongo isn't a queue, so the safe play is **idempotent re-enqueue**: enqueue every queued-on-this-node job. The queue is at-least-once anyway; duplicates are dropped at lease time when status is no longer `queued`.

## Configurability

Env vars (defined in `.env.example`):

- `SPINDLE_NODE` — required. Identifies this node.
- `SPINDLE_DISPATCHER_CONFIGS` — comma-separated `config_id`s to serve. Empty = serve all configs whose `preferred_node == SPINDLE_NODE`.
- `SPINDLE_DISPATCHER_TICK_MS` — main loop cadence. Default 200.
- `SPINDLE_DISPATCHER_LEASE_TTL_SECONDS` — initial lease length. Default 60.
- `SPINDLE_DISPATCHER_HOST_CONCURRENCY` — total in-flight cap across all configs on this host.

## Acceptance criteria

- [ ] `uv run pytest dispatcher/` passes with mocked `StateStore` + `JobQueue`.
- [ ] `uv run python -m spindle_dispatcher` boots against compose infra and runs idle (no jobs in queue) without errors.
- [ ] When a test fixture enqueues a job referencing a config registered to this node, the dispatcher reserves, leases, and writes to a stub Unix-socket listener within 1s.
- [ ] When the stub socket is unavailable, the dispatcher reverts the lease and nacks the queue message.
- [ ] Lease sweeper requeues an expired-lease job (when retries available) or dead-letters it (when exhausted), verified with time-mocked tests.
- [ ] Recovery sweep re-enqueues a `queued`-but-missing-from-queue job at startup.
- [ ] No imports from `spindle_api`, `spindle_workers`, `spindle_cli`.
- [ ] `ruff` + `pyright` clean.

## Out of scope

- Smart scheduling beyond the simple scoring function (image-before-video, fairness, etc. — later).
- Hot model swap orchestration.
- Push dispatch (vs. polling) — workers always wait for IPC from dispatcher; dispatcher always polls the queue.
- Cross-node coordination (each dispatcher is independent; only Redis + Mongo are shared state).
- Telemetry emission to ClickHouse (Phase 8).
