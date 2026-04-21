# pingcapture

A personal DSL reliability monitor. Captures ping / TCP / mtr data continuously, exposes a local web console, and generates bilingual (EN/DE) reports you can share with your DSL provider.

## What it does

- **Ping** rotates through 4 well-known hosts (Cloudflare, Google, Quad9, plus a DNS-resolution sanity check) every ~5 seconds
- **TCP/443** fallback every 30 seconds, to distinguish "ICMP being dropped" from "DSL is actually down"
- **mtr** every 15 minutes to a couple of destinations, with full hop-by-hop storage and path-change detection
- **SQLite** append-only storage
- **Localhost web console** with Chart.js visualizations
- **Report generator** (Markdown + HTML) in English and German
- **launchd** integration for set-and-forget operation on macOS

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pingcapture init        # writes default config + creates DB
pingcapture run         # foreground run for testing
pingcapture service install   # background launchd job
```

## Web console

```
http://127.0.0.1:8765
```

## Reports

```bash
pingcapture report --since=7d --lang=en --format=html --out=report-en.html
pingcapture report --since=7d --lang=de --format=html --out=report-de.html
```

## Develop

```bash
pytest                  # runs unit + BDD tests
ruff check src tests
mypy src
```

## Enabling mtr (macOS)

`mtr-packet` needs raw sockets, which requires the setuid bit on macOS:

```bash
sudo chown root:wheel "$(which mtr-packet)"
sudo chmod u+s "$(which mtr-packet)"
```

If this is not set, the ping/TCP capture still runs; the mtr scheduler logs a one-time hint and exits.

