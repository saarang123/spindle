"""CLI entrypoint — ``spindle-workers <subcommand>``."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

# Load .env from the cwd (and parent dirs) so secrets like OPENAI_API_KEY,
# SPINDLE_S3_*, etc. are available in os.environ for child workers.
# Pydantic-settings does this for SPINDLE_* fields but doesn't write them
# into os.environ, which the children need.
load_dotenv()

from .config import WorkersConfig, resolve_logs_dir
from .supervisor import Supervisor


cli = typer.Typer(
    name="spindle-workers",
    help="Spindle per-node worker supervisor.",
    no_args_is_help=True,
)


def _default_config_path() -> Path | None:
    env = os.environ.get("SPINDLE_RUNTIME_CONFIG")
    if env:
        return Path(env).expanduser()
    hostname = socket.gethostname()
    candidate = Path(f"configs/runtime.{hostname}.yaml")
    if candidate.exists():
        return candidate
    return None


@cli.command()
def run(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="Path to runtime YAML config."
    ),
    logs_dir: Optional[Path] = typer.Option(
        None, "--logs-dir", help="Override logs_dir from config / env."
    ),
) -> None:
    """Spawn and supervise the workers listed in CONFIG."""
    config_path = config or _default_config_path()
    if config_path is None:
        typer.secho(
            "no config path provided and no default found "
            "(set SPINDLE_RUNTIME_CONFIG, pass --config, or place "
            "configs/runtime.<hostname>.yaml in the cwd)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    cfg = WorkersConfig.from_yaml(config_path)
    resolved_logs = resolve_logs_dir(
        cli_flag=logs_dir,
        env_value=os.environ.get("SPINDLE_LOGS_DIR"),
        yaml_value=cfg.log_dir,
    )

    supervisor = Supervisor(cfg, logs_dir=resolved_logs)
    typer.echo(
        f"spindle-workers: starting {len(supervisor.children)} child(ren) "
        f"from {config_path} (logs → {resolved_logs})"
    )
    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        pass


@cli.command()
def status(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Snapshot of running children.

    v0: queries a status socket exposed by the supervisor. Not implemented yet;
    use `ps` + log files in the meantime. Stub kept so the command surface is stable.
    """
    msg = "status command not implemented yet (use ps / log files in the meantime)"
    if json_output:
        typer.echo(json.dumps({"error": msg}))
    else:
        typer.secho(msg, fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(1)


@cli.command()
def stop(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    grace: float = typer.Option(10.0, "--grace", help="Seconds before SIGKILL."),
) -> None:
    """Request graceful shutdown of a running supervisor.

    v0: stub. SIGTERM the supervisor process directly (e.g. via Ctrl-C in its
    terminal, or kill <pid>) to achieve the same effect.
    """
    typer.secho(
        "stop command not implemented yet (SIGTERM the supervisor process directly)",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(1)


@cli.command()
def logs(
    worker_id: str = typer.Argument(...),
    follow: bool = typer.Option(False, "-f", "--follow", help="Tail the log file."),
    logs_dir: Optional[Path] = typer.Option(None, "--logs-dir"),
) -> None:
    """Cat / tail a child's log file."""
    resolved = resolve_logs_dir(
        cli_flag=logs_dir,
        env_value=os.environ.get("SPINDLE_LOGS_DIR"),
        yaml_value=None,
    )
    log_path = resolved / f"{worker_id}.log"
    if not log_path.exists():
        typer.secho(f"no log file at {log_path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(4)
    if follow:
        try:
            subprocess.run(["tail", "-f", str(log_path)], check=False)
        except KeyboardInterrupt:
            pass
    else:
        typer.echo(log_path.read_text())


if __name__ == "__main__":
    cli()
