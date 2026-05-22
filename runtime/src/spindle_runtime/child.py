"""ChildProcess — owns one subprocess + its restart loop."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import RestartPolicy


@dataclass
class ChildStatus:
    name: str
    worker_id: str
    pid: int | None
    started_at: float | None
    uptime_s: float | None
    restart_count: int
    last_exit_code: int | None
    is_running: bool


class ChildProcess:
    """Manages a single worker subprocess.

    Spawns it, captures stdout/stderr to a per-child log file + supervisor
    stderr tee, restarts per the supplied ``RestartPolicy`` when it exits.
    The supervisor owns the lifecycle decision (when to stop entirely).
    """

    def __init__(
        self,
        *,
        name: str,
        worker_id: str,
        module: str,
        env: dict[str, str],
        logs_dir: Path,
        restart: RestartPolicy,
        ipc_socket_dir: Path = Path("/tmp"),
    ) -> None:
        self.name = name
        self.worker_id = worker_id
        self.module = module
        self._env = dict(env)
        self._restart = restart
        self._logs_dir = Path(logs_dir)
        self._log_file = self._logs_dir / f"{worker_id}.log"
        self._ipc_socket = ipc_socket_dir / f"spindle-worker-{worker_id}.sock"

        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._restart_count = 0
        self._last_exit_code: int | None = None
        self._started_at: float | None = None

    def _env_for_child(self) -> dict[str, str]:
        env = {**self._env}
        env["SPINDLE_WORKER_ID"] = self.worker_id
        env["SPINDLE_WORKER_IPC_SOCKET"] = str(self._ipc_socket)
        env["SPINDLE_LOGS_DIR"] = str(self._logs_dir)
        return env

    async def run(self) -> None:
        """Spawn → wait → restart-per-policy loop. Returns on permanent stop."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        attempt = 0

        while not self._stopping:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", self.module,
                env=self._env_for_child(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._started_at = time.monotonic()

            tee_task = asyncio.create_task(self._tee_logs(self._proc.stdout))
            try:
                exit_code = await self._proc.wait()
            finally:
                await tee_task

            self._last_exit_code = exit_code
            self._started_at = None

            if self._stopping:
                return
            if self._restart.policy == "never":
                return
            if self._restart.policy == "on_failure" and exit_code == 0:
                return

            attempt += 1
            self._restart_count += 1

            if (
                self._restart.max_consecutive_failures
                and attempt > self._restart.max_consecutive_failures
            ):
                return

            delay = self._restart.backoff_s[
                min(attempt - 1, len(self._restart.backoff_s) - 1)
            ]
            await asyncio.sleep(delay)

    async def _tee_logs(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        prefix = f"[{self.worker_id}] ".encode()
        try:
            with self._log_file.open("ab") as f:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    f.write(line)
                    f.flush()
                    try:
                        os.write(2, prefix + line)
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise

    async def stop(self, grace_seconds: float) -> None:
        """Set the stop flag, SIGTERM the child, SIGKILL after grace."""
        self._stopping = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    def snapshot(self) -> ChildStatus:
        proc = self._proc
        is_running = proc is not None and proc.returncode is None
        pid = proc.pid if is_running else None
        uptime = (
            time.monotonic() - self._started_at
            if (self._started_at and is_running)
            else None
        )
        return ChildStatus(
            name=self.name,
            worker_id=self.worker_id,
            pid=pid,
            started_at=self._started_at,
            uptime_s=uptime,
            restart_count=self._restart_count,
            last_exit_code=self._last_exit_code,
            is_running=is_running,
        )
