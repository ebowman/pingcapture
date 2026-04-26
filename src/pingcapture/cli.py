"""Command-line entry point for pingcapture."""

from __future__ import annotations

import asyncio
import logging
import signal
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
@click.pass_obj
def run(cfg: Config) -> None:
    """Run the pinger + mtr scheduler in the foreground."""
    asyncio.run(_run(cfg))


async def _run(cfg: Config) -> None:
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
    try:
        await asyncio.gather(
            run_pinger(cfg, store, stop),
            run_mtr_scheduler(cfg, store, stop),
        )
    finally:
        store.close()
        log.info("pingcapture stopped")


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
