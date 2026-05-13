# pingcapture

A local-only tool that watches a residential internet connection and produces evidence good enough to hand to a provider's support desk.

## Why

My DSL line is flaky. My provider's first response is always "we can't see anything wrong on our end." Anecdotes don't move that conversation forward. Continuous probes, an outage timeline, and per-hop trace data do. There is no auth, no cloud, no telemetry, no alerting — just capture, view, and report.

## What it captures

Three probe types, on staggered schedules:

- **ICMP** every 5 s, rotating across `1.1.1.1`, `8.8.8.8`, `9.9.9.9`, and `cloudflare.com`. The DNS name is there to catch the case where IPs work but resolution doesn't.
- **TCP/443** every 30 s. If TCP succeeds while ICMP fails, the path is up and someone upstream is dropping pings. If both fail, something user-visible is broken.
- **mtr** every 15 min. When something goes wrong, mtr tells you whether the failure is one hop into your provider's network or out near the destination — much more useful evidence than "the internet was slow."

A failure streak is reported as an outage only when it contains at least one **TCP** failure. ICMP-only streaks (usually upstream ICMP rate-limiting or QoS deprioritization) show up on the timeline as flicker but don't get counted as user-visible outages. That distinction matters because the report is meant to be evidence — over-claiming outages weakens the case for real ones.

## What you see

Run `pingcapture run` in the foreground or `pingcapture service install` to keep it going across reboots, then point a browser at `http://127.0.0.1:8765`.

The dashboard has:

- A status pill (UP / DEGRADED / DOWN), refreshed every 10 s.
- An **hourly outage grid**: one cell per UTC hour over the selected range (7d / 30d / 90d), severity-colored green → yellow → orange → red → dark red, with a tooltip per cell. Time-of-day patterns jump out.
- A **latency-over-time** chart with a selectable window (1h / 6h / 24h / 7d). Under one hour shows raw probes; wider windows show a downsampled p50 line with a p95 fill band so the chart stays readable without losing tail behavior.
- Three at-a-glance numbers: uptime, outage count + total duration, buffer-bloat score (latency stdev).
- Per-target percentile tables, the last fifty events, recent outages with their affected targets, and any mtr path changes.

Window selections persist in the URL (`?w=1&d=30`), so a reload or a copy-paste link restores the same view.

## The report

A printer-friendly version of the dashboard lives at `/report`. Open the console, click "Report", pick a language and time range from the toolbar at the top, then ⌘P → "Save as PDF" and send the file to your provider. The toolbar hides when printing. The German version is hand-written, not machine-translated.

```
http://127.0.0.1:8765/report?lang=en&since=7d
http://127.0.0.1:8765/report?lang=de&since=30d
```

## How it's put together

Two processes share one SQLite database (WAL mode, separate readers and one writer):

- **Pinger process** runs the ICMP/TCP probes and the mtr scheduler in an asyncio loop. Nothing else lives there — heavy work would block `icmplib.async_ping`'s socket reader and produce false timeouts.
- **Web console process** (a supervised child) serves the FastAPI app at `127.0.0.1:8765`. It only reads from the DB.

The hourly outage grid reads from a materialized `hourly_buckets` table that the pinger updates as probes come in. A 90-day grid is a 2160-row indexed range scan, not a full-table aggregation.

## Install (macOS)

```bash
git clone https://github.com/ebowman/pingcapture
cd pingcapture
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pingcapture init
```

For mtr, `mtr-packet` needs raw sockets, which on macOS means setuid:

```bash
sudo chown root:wheel "$(which mtr-packet)"
sudo chmod u+s "$(which mtr-packet)"
```

If you skip this the rest still works; the mtr scheduler logs a one-time hint and exits.

## Run

Foreground:

```bash
pingcapture run
```

Background, persistent across reboots:

```bash
pingcapture service install
pingcapture service status
pingcapture service logs           # tail stdout
pingcapture service logs --err     # tail stderr
pingcapture service uninstall
```

## Configure

`pingcapture init` writes `~/.config/pingcapture/config.toml` with defaults that work without editing, and prints the URL the console will live at. If the default port (`8765`) is already taken, `init` scans upward and persists the first free port it finds into the TOML — so subsequent runs are predictable. The DB lives at `~/Library/Application Support/pingcapture/pingcapture.sqlite`. Override either with `PINGCAPTURE_CONFIG` or `PINGCAPTURE_DATA_DIR`.

To change the port for a single run without editing the config, pass `--port` (also `--host`): `pingcapture run --port 8766` or `pingcapture console --port 8766`. If the configured port is in use at startup, the console exits with an actionable hint instead of a uvicorn traceback.

## Test

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

The suite includes `pytest-bdd` scenarios for the user-facing behaviors (outage detection thresholds, uptime calculation, bilingual report rendering) and unit tests for storage, analytics, mtr parsing, the async pinger loop, i18n key coverage, and the web API.

## License

MIT.
