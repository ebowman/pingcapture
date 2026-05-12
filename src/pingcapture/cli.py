"""Command-line entry point for pingcapture."""

from __future__ import annotations

import asyncio
import logging
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
    if cfg_path.exists():
        click.echo(f"config already exists: {cfg_path}")
    else:
        cfg_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        click.echo(f"wrote config: {cfg_path}")
    click.echo(f"data dir:    {data_dir}")
    # Touch the DB to confirm we can write there.
    cfg = Config.load()
    Store(cfg.db_path).close()
    click.echo(f"db:          {cfg.db_path}")


@main.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Run the pinger + mtr scheduler, with the web console in a child process.

    The web server runs in a separate process so that heavy HTTP work (large
    JSON payloads, full-table scans) cannot block this process's asyncio loop.
    A blocked loop would starve ``icmplib.async_ping``'s socket reader and
    produce false outages — see pingcapture-vih.
    """
    cfg: Config = ctx.obj
    config_path: Path | None = ctx.meta.get("config_path")
    verbose: bool = ctx.meta.get("verbose", False)
    asyncio.run(_run(cfg, config_path=config_path, verbose=verbose))


async def _run(
    cfg: Config, *, config_path: Path | None, verbose: bool
) -> None:
    log = logging.getLogger("pingcapture")
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.db_path)
    stop = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        log.info("received signal %d, shutting down", signum)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    log.info("pingcapture %s starting (db=%s)", __version__, cfg.db_path)
    log.info("console at http://%s:%d", cfg.web_host, cfg.web_port)
    backfilled = store.backfill_hourly_buckets()
    if backfilled:
        log.info("backfilled %d hourly bucket rows", backfilled)
    try:
        await asyncio.gather(
            run_pinger(cfg, store, stop),
            run_mtr_scheduler(cfg, store, stop),
            _supervise_console(stop, config_path=config_path, verbose=verbose),
        )
    finally:
        store.close()
        log.info("pingcapture stopped")


async def _supervise_console(
    stop: asyncio.Event, *, config_path: Path | None, verbose: bool
) -> None:
    """Run ``pingcapture console`` as a child process and keep it alive.

    Stdout/stderr are inherited so launchd's log files capture both processes.
    On exit, the child is sent SIGTERM and given a few seconds to drain.
    """
    log = logging.getLogger("pingcapture.console")
    cmd = [sys.executable, "-m", "pingcapture.cli"]
    if verbose:
        cmd.append("-v")
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])
    cmd.append("console")

    backoff_s = 1.0
    while not stop.is_set():
        log.info("starting web console child: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(*cmd)
        # Wait for either the child to exit or the stop event.
        wait_child = asyncio.create_task(proc.wait())
        wait_stop = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {wait_child, wait_stop}, return_when=asyncio.FIRST_COMPLETED
        )
        if wait_stop in done:
            wait_child.cancel()
            log.info("stopping web console child (pid=%d)", proc.pid)
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                log.warning("web console did not exit in 5s, killing")
                proc.kill()
                await proc.wait()
            return
        # Child exited on its own — restart with bounded backoff.
        wait_stop.cancel()
        rc = proc.returncode
        log.warning("web console exited rc=%s, restarting in %.1fs", rc, backoff_s)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff_s)
            return  # stop was set during the backoff
        except TimeoutError:
            pass
        backoff_s = min(backoff_s * 2, 30.0)


@main.command()
@click.pass_obj
def console(cfg: Config) -> None:
    """Serve the local web console (dashboard + report)."""
    app = create_app(cfg)
    click.echo(f"console at http://{cfg.web_host}:{cfg.web_port}")
    click.echo(f"report  at http://{cfg.web_host}:{cfg.web_port}/report?lang=en&since=7d")
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")


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
