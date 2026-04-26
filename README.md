# pingcapture

I have a flaky DSL line. My provider's first response is always "we can't see anything wrong on our end." This tool collects evidence.

It runs in the background, pings a few well-known hosts every five seconds, opens a TCP connection every thirty seconds, traces the route every fifteen minutes, and writes everything to a local SQLite file. There's a localhost dashboard for poking around, and a report generator that produces a clean PDF-printable HTML or Markdown summary in English or German.

It is a personal tool, not a product. There is no auth, no cloud, no telemetry, no alerting — just capture, view, and report.

## Why probe three different ways

ICMP latency is the cleanest signal of link health, but ISPs sometimes deprioritize ICMP, and "no ping reply" is not always "no internet." So:

- **ICMP** every 5 s, rotating across `1.1.1.1`, `8.8.8.8`, `9.9.9.9`, and `cloudflare.com`. The DNS name is in there to catch the case where IPs work but resolution doesn't.
- **TCP/443** every 30 s. If TCP succeeds while ICMP fails, the path is up and someone upstream is dropping pings. If both fail, the link is genuinely broken.
- **mtr** every 15 minutes. When something does go wrong, mtr tells you whether the failure is one hop into your provider's network or out near the destination — much more useful evidence than "the internet was slow."

Outages are declared when at least three consecutive probes (any kind, any target) fail. Single-target hiccups don't count, since the next rotation samples a different host.

## Install (macOS)

```bash
git clone https://github.com/ebowman/pingcapture
cd pingcapture
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pingcapture init
```

For mtr to work, `mtr-packet` needs raw sockets, which on macOS means setuid:

```bash
sudo chown root:wheel "$(which mtr-packet)"
sudo chmod u+s "$(which mtr-packet)"
```

If you skip this step the rest still works; the mtr scheduler logs a one-time hint and exits.

## Run

Foreground (for testing):

```bash
pingcapture run
```

Background, persistent across reboots:

```bash
pingcapture service install
pingcapture service status
pingcapture service logs           # tail of stdout
pingcapture service logs --err     # tail of stderr
pingcapture service uninstall
```

## Look at the data

```bash
pingcapture console     # http://127.0.0.1:8765
```

You get a status pill (UP / DEGRADED / DOWN), uptime over a configurable window, a latency timeseries with one line per target, per-target percentiles, an outage table, the most recent fifty events, and any mtr path changes.

## Make a report for the provider

The report lives at `/report` in the same web app. Open the dashboard, click "Report", switch language and time window with the toolbar at the top, then ⌘P → "Save as PDF" to send to your provider. The toolbar disappears when printing. The German version is hand-written, not machine-translated.

```
http://127.0.0.1:8765/report?lang=en&since=7d
http://127.0.0.1:8765/report?lang=de&since=30d
```

## Configure

`pingcapture init` writes `~/.config/pingcapture/config.toml` with sensible defaults. The data lives at `~/Library/Application Support/pingcapture/pingcapture.sqlite`. Override either with `PINGCAPTURE_CONFIG` and `PINGCAPTURE_DATA_DIR` if you want.

## Test

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

The test suite includes `pytest-bdd` scenarios for the user-facing behaviors (outage detection thresholds, uptime calculation, bilingual report rendering) and unit tests for the storage, analytics, mtr parser, async pinger loop, i18n key completeness, and web API.

## License

MIT.
