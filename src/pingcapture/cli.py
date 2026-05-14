"""Command-line entry point for pingcapture."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import signal
import sys
from pathlib import Path

import click
import uvicorn

from . import __version__
from .config import (
    DEFAULT_CONFIG_TOML,
    Config,
    default_config_path,
    default_data_dir,
    pick_free_port,
    port_is_free,
)
from .mtr import run_mtr_scheduler
from .pinger import run_pinger
from .service import LABEL, install, logs_dir, plist_path, status, uninstall
from .storage import Store
from .web.app import create_app


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Quiet noisy libs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None,
              help="Path to config TOML (default: ~/.config/pingcapture/config.toml)")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.version_option(__version__, prog_name="pingcapture")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, verbose: bool) -> None:
    """pingcapture: continuous DSL reliability monitor."""
    _setup_logging(verbose)
    ctx.obj = Config.load(config_path)
    # Stash so subcommands can spawn child processes with the same --config.
    ctx.meta["config_path"] = config_path
    ctx.meta["verbose"] = verbose


@main.command()
def init() -> None:
    """Write a default config file and create the data directory."""
    cfg_path = default_config_path()
    data_dir = default_data_dir()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    fresh_config = not cfg_path.exists()
    if fresh_config:
        cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        click.echo(f"wrote config: {cfg_path}")
    else:
        click.echo(f"config already exists: {cfg_path}")
    click.echo(f"data dir:    {data_dir}")

    cfg = Config.load()
    Store(cfg.db_path).close()
    click.echo(f"db:          {cfg.db_path}")

    # Port selection happens once, here, and is persisted to the TOML so
    # subsequent runs are predictable. We never auto-bump at run time.
    chosen_port = cfg.web_port
    if fresh_config and not port_is_free(cfg.web_host, cfg.web_port):
        alt = pick_free_port(cfg.web_host, cfg.web_port + 1)
        if alt is not None:
            old_line = f"web_port = {cfg.web_port}"
            new_line = f"web_port = {alt}"
            text = cfg_path.read_text(encoding="utf-8")
            if old_line in text:
                cfg_path.write_text(text.replace(old_line, new_line, 1), encoding="utf-8")
                click.echo(
                    f"port {cfg.web_port} was in use; chose {alt} "
                    f"(edit web_port in {cfg_path} to override)"
                )
                chosen_port = alt
            else:
                click.echo(
                    f"⚠ port {cfg.web_port} is in use but couldn't rewrite "
                    f"{cfg_path}; edit web_port manually"
                )
        else:
            click.echo(
                f"⚠ port {cfg.web_port} is in use and no nearby free port found; "
                f"edit web_port in {cfg_path}"
            )
    elif not fresh_config and not port_is_free(cfg.web_host, cfg.web_port):
        click.echo(
            f"⚠ port {cfg.web_port} appears to be in use; "
            f"edit web_port in {cfg_path} or pass --port at run time"
        )

    click.echo(f"console:     http://{cfg.web_host}:{chosen_port}")


@main.command()
@click.option("--port", type=int, default=None, help="Override web_port for this run.")
@click.option("--host", "host", type=str, default=None, help="Override web_host for this run.")
@click.pass_context
def run(ctx: click.Context, port: int | None, host: str | None) -> None:
    """Run the pinger + mtr scheduler, with the web console in a child process.

    The web server runs in a separate process so that heavy HTTP work (large
    JSON payloads, full-table scans) cannot block this process's asyncio loop.
    A blocked loop would starve ``icmplib.async_ping``'s socket reader and
    produce false outages — see pingcapture-vih.
    """
    cfg: Config = ctx.obj
    if port is not None or host is not None:
        cfg = dataclasses.replace(
            cfg,
            web_port=port if port is not None else cfg.web_port,
            web_host=host if host is not None else cfg.web_host,
        )
    config_path: Path | None = ctx.meta.get("config_path")
    verbose: bool = ctx.meta.get("verbose", False)
    asyncio.run(
        _run(cfg, config_path=config_path, verbose=verbose, port=port, host=host)
    )


def _run_pid_path(cfg: Config) -> Path:
    return cfg.db_path.parent / "run.pid"


async def _run(
    cfg: Config, *, config_path: Path | None, verbose: bool,
    port: int | None, host: str | None,
) -> None:
    log = logging.getLogger("pingcapture")
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    stop = asyncio.Event()
    restart_console = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        log.info("received signal %d, shutting down", signum)
        stop.set()

    def _handle_sigusr1() -> None:
        log.info("received SIGUSR1, bouncing console child")
        restart_console.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)
    loop.add_signal_handler(signal.SIGUSR1, _handle_sigusr1)

    pid_path = _run_pid_path(cfg)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    log.info("pingcapture %s starting (db=%s)", __version__, cfg.db_path)
    log.info("console at http://%s:%d", cfg.web_host, cfg.web_port)
    backfilled = store.backfill_hourly_buckets()
    if backfilled:
        log.info("backfilled %d hourly bucket rows", backfilled)
    try:
        await asyncio.gather(
            run_pinger(cfg, store, stop),
            run_mtr_scheduler(cfg, store, stop),
            _supervise_console(
                stop, restart_console, cfg,
                config_path=config_path, verbose=verbose,
                port=port, host=host,
            ),
        )
    finally:
        store.close()
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        log.info("pingcapture stopped")


async def _supervise_console(
    stop: asyncio.Event, restart: asyncio.Event, cfg: Config, *,
    config_path: Path | None, verbose: bool,
    port: int | None, host: str | None,
) -> None:
    """Run ``pingcapture console`` as a child process and keep it alive.

    Stdout/stderr are inherited so launchd's log files capture both processes.
    On stop, the child is sent SIGTERM and given a few seconds to drain.
    When ``restart`` is set (e.g. via SIGUSR1 -> 'pingcapture restart-console'),
    the current child is terminated and a fresh one is started immediately
    with no backoff. Backoff only applies to crashes, and is reset on clean
    exit (rc=0 or a SIGTERM/SIGINT-shaped negative rc).
    """
    log = logging.getLogger("pingcapture.console")
    effective_host = host if host is not None else cfg.web_host
    effective_port = port if port is not None else cfg.web_port
    cmd = [sys.executable, "-m", "pingcapture.cli"]
    if verbose:
        cmd.append("-v")
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])
    cmd.append("console")
    if port is not None:
        cmd.extend(["--port", str(port)])
    if host is not None:
        cmd.extend(["--host", host])

    async def _wait_port_free() -> None:
        """Poll until the child's port is bindable. The just-killed child's
        socket lingers briefly in TIME_WAIT; without this wait the next
        child trips its pre-flight port_is_free check and exits with an
        error, kicking us into a multi-second crash-loop backoff."""
        for _ in range(20):  # ~10 s total
            if port_is_free(effective_host, effective_port):
                return
            await asyncio.sleep(0.5)

    backoff_s = 1.0
    while not stop.is_set():
        log.info("starting web console child: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(*cmd)
        # Wait for: child exit, stop, or user-requested restart.
        wait_child = asyncio.create_task(proc.wait())
        wait_stop = asyncio.create_task(stop.wait())
        wait_restart = asyncio.create_task(restart.wait())
        done, _ = await asyncio.wait(
            {wait_child, wait_stop, wait_restart},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_stop in done:
            for t in (wait_child, wait_restart):
                t.cancel()
            log.info("stopping web console child (pid=%d)", proc.pid)
            await _terminate_child(proc, log)
            return
        if wait_restart in done:
            for t in (wait_child, wait_stop):
                t.cancel()
            log.info("bouncing web console child (pid=%d)", proc.pid)
            await _terminate_child(proc, log, force=True)
            restart.clear()
            backoff_s = 1.0
            continue
        # Child exited on its own.
        for t in (wait_stop, wait_restart):
            t.cancel()
        rc = proc.returncode
        if rc in (0, -signal.SIGTERM, -signal.SIGINT):
            # Clean exit — treat the next start as fresh.
            log.info("web console exited cleanly rc=%s, restarting", rc)
            backoff_s = 1.0
            await _wait_port_free()
            continue
        log.warning("web console crashed rc=%s, restarting in %.1fs", rc, backoff_s)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff_s)
            return  # stop was set during the backoff
        except TimeoutError:
            pass
        backoff_s = min(backoff_s * 2, 30.0)


async def _terminate_child(
    proc: asyncio.subprocess.Process, log: logging.Logger, *, force: bool = False
) -> None:
    """Stop the console child.

    ``force=True`` goes straight to SIGKILL — used for user-initiated bounces
    where we don't want to wait for uvicorn to drain idle browser keepalives
    (it would hold the listen port for tens of seconds). The web server is
    stateless; clients will reconnect on their own. ``force=False`` is the
    polite path used at daemon shutdown.
    """
    if force:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        log.warning("web console did not exit in 5s, killing")
        proc.kill()
        await proc.wait()


@main.command()
@click.option("--port", type=int, default=None, help="Override web_port for this run.")
@click.option("--host", "host", type=str, default=None, help="Override web_host for this run.")
@click.pass_obj
def console(cfg: Config, port: int | None, host: str | None) -> None:
    """Serve the local web console (dashboard + report)."""
    if port is not None or host is not None:
        cfg = dataclasses.replace(
            cfg,
            web_port=port if port is not None else cfg.web_port,
            web_host=host if host is not None else cfg.web_host,
        )
    # Pre-flight bind probe. uvicorn logs ERROR and exits non-zero on bind
    # failure, but the traceback is unhelpful for the common "port in use"
    # case. Probing here lets us print an actionable hint instead.
    if not port_is_free(cfg.web_host, cfg.web_port):
        click.echo(
            f"error: port {cfg.web_port} is already in use on {cfg.web_host}.",
            err=True,
        )
        click.echo(
            f"  try: pingcapture console --port {cfg.web_port + 1}",
            err=True,
        )
        click.echo(
            f"  or edit web_port in {default_config_path()}",
            err=True,
        )
        sys.exit(1)
    app = create_app(cfg)
    click.echo(f"console at http://{cfg.web_host}:{cfg.web_port}")
    click.echo(f"report  at http://{cfg.web_host}:{cfg.web_port}/report?lang=en&since=7d")
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")


@main.command("restart-console")
@click.pass_obj
def restart_console(cfg: Config) -> None:
    """Bounce the web console child of a running ``pingcapture run``.

    Signals the supervisor (SIGUSR1) to terminate its current console child
    and start a fresh one immediately — useful when you've changed code or
    static assets and want the new version live without restarting the
    whole pinger/mtr loop.
    """
    pid_path = _run_pid_path(cfg)
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        click.echo(
            f"no run.pid at {pid_path} — is 'pingcapture run' running?",
            err=True,
        )
        sys.exit(1)
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        click.echo(
            f"pid {pid} from {pid_path} is not running (stale pid file)",
            err=True,
        )
        sys.exit(1)
    click.echo(f"sent SIGUSR1 to pingcapture run (pid={pid})")


@main.command("recompute-buckets")
@click.pass_obj
def recompute_buckets(cfg: Config) -> None:
    """Recompute every materialized hourly bucket from raw pings.

    Run this after a change to the outage-detection rule, so the dashboard
    grid and report reflect the new definition for past hours. Live data
    is always classified under the current rule; this only refreshes the
    cached hourly summaries.
    """
    store = Store(cfg.db_path)
    try:
        n = store.recompute_all_hourly_buckets()
    finally:
        store.close()
    click.echo(f"recomputed {n} hourly buckets at {cfg.db_path}")


@main.group()
def service() -> None:
    """Manage the launchd background service (macOS)."""


@service.command("install")
def service_install() -> None:
    """Install and load the launchd job."""
    p = install()
    click.echo(f"installed: {p}")
    click.echo(f"label:     {LABEL}")
    click.echo(f"logs:      {logs_dir()}")


@service.command("uninstall")
def service_uninstall() -> None:
    """Unload and remove the launchd job."""
    uninstall()
    click.echo(f"uninstalled: {plist_path()}")


@service.command("status")
def service_status() -> None:
    """Show launchd job status."""
    s = status()
    click.echo(f"installed: {s.installed}")
    click.echo(f"loaded:    {s.loaded}")
    click.echo(f"pid:       {s.pid if s.pid is not None else '—'}")
    click.echo(f"last exit: {s.last_exit if s.last_exit is not None else '—'}")
    click.echo(f"plist:     {plist_path()}")
    click.echo(f"logs:      {logs_dir()}")


@service.command("logs")
@click.option("--err/--out", "err", default=False, help="Show stderr log instead of stdout")
def service_logs(err: bool) -> None:
    """Print the tail of the service log."""
    name = "pingcapture.err.log" if err else "pingcapture.out.log"
    p = logs_dir() / name
    if not p.exists():
        click.echo(f"(no log yet at {p})")
        return
    text = p.read_text(encoding="utf-8", errors="replace")
    tail = "\n".join(text.splitlines()[-200:])
    click.echo(tail)


if __name__ == "__main__":
    main()
