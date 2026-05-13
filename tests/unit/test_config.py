"""Tests for config-level port selection helpers and the init flow."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from click.testing import CliRunner

from pingcapture.cli import main
from pingcapture.config import (
    DEFAULT_CONFIG_TOML,
    Config,
    pick_free_port,
    port_is_free,
)


@pytest.fixture
def busy_port():
    """Open a listening socket and yield its port; close on teardown."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        yield port
    finally:
        s.close()


def test_port_is_free_detects_busy(busy_port):
    assert port_is_free("127.0.0.1", busy_port) is False


def test_port_is_free_detects_free():
    # Port 0 is special; pick a likely-free high port instead.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert port_is_free("127.0.0.1", free) is True


def test_pick_free_port_skips_busy(busy_port):
    chosen = pick_free_port("127.0.0.1", busy_port)
    assert chosen is not None
    assert chosen != busy_port
    assert chosen >= busy_port


def test_pick_free_port_returns_start_when_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    assert pick_free_port("127.0.0.1", free) == free


def test_default_toml_contains_web_port():
    # Guard the string the init-rewrite logic depends on.
    assert "web_port = 8765" in DEFAULT_CONFIG_TOML


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "PINGCAPTURE_CONFIG": str(tmp_path / "config.toml"),
        "PINGCAPTURE_DATA_DIR": str(tmp_path / "data"),
    }


def test_init_writes_and_echoes_url(tmp_path: Path, monkeypatch):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output
    cfg_path = tmp_path / "config.toml"
    assert cfg_path.exists()
    # init must always tell the user where the console will live.
    assert "http://127.0.0.1:" in result.output
    # And whatever port ends up in the config must be loadable as int.
    cfg = Config.load(cfg_path)
    assert 1024 <= cfg.web_port <= 65535


def test_init_rewrites_port_when_busy(tmp_path: Path, monkeypatch):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)

    # Hold 8765 so init has to pick something else.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            s.bind(("127.0.0.1", 8765))
        except OSError:
            pytest.skip("port 8765 already held by something on this host")
        s.listen(1)

        runner = CliRunner()
        result = runner.invoke(main, ["init"])
        assert result.exit_code == 0, result.output

        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "web_port = 8765" not in text
        # The chosen port should load back through Config.load().
        cfg = Config.load(tmp_path / "config.toml")
        assert cfg.web_port != 8765
        assert cfg.web_port > 8765
        assert "was in use" in result.output
    finally:
        s.close()


def test_init_does_not_rewrite_existing_config(tmp_path: Path, monkeypatch):
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('web_port = 9999\nweb_host = "127.0.0.1"\n', encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0, result.output

    # Existing config is left intact regardless of whether 9999 is busy.
    assert cfg_path.read_text(encoding="utf-8") == (
        'web_port = 9999\nweb_host = "127.0.0.1"\n'
    )
