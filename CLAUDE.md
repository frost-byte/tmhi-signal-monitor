# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small self-hosted tool that polls a **T-Mobile Home Internet gateway**
(Arcadyan **TMO-G4AR**) for cellular signal telemetry, runs periodic
download/upload/ping/jitter speed tests, stores every reading in SQLite, and
serves a live-updating two-tab browser dashboard (Signal / Connection).

It exists because T-Mobile's current **T-Life** app no longer exposes the raw
radio metrics (RSRP / RSRQ / SINR / RSSI / band) that the retired T-Mobile
Internet app used to show. Those metrics are still available from the gateway's
**local HTTP API**, which is what this tool reads.

Single file today: `tmhi_monitor.py`. Python 3.8+, **standard library only** on
the backend, except for the speed test, which shells out to the **Ookla
Speedtest CLI** (a separate binary — see below). The dashboard pulls Chart.js
from a CDN.

## Run it

```bash
python3 tmhi_monitor.py
# then open http://localhost:8073   (Ctrl-C to stop)
python3 tmhi_monitor.py --help   # list startup flags (see Configuration below)
```

No Python dependencies to install. Must run on a machine that can reach the
gateway at `192.168.12.1`.

The speed test needs the **Ookla Speedtest CLI** binary at `SPEEDTEST_BIN`
(default `~/.local/bin/speedtest`). No root required to install it — download
the static tarball for your arch from `speedtest.net/apps/cli` and extract the
`speedtest` binary into `~/.local/bin`:

```bash
curl -sL -o /tmp/speedtest.tgz https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz
tar -xzf /tmp/speedtest.tgz -C ~/.local/bin speedtest
chmod +x ~/.local/bin/speedtest
```

If the binary is missing or fails, speed tests just produce `ok=0` rows
(same degrade-gracefully behavior as an unreachable gateway) — the rest of the
app keeps working.

## The endpoint (verified working)

```
GET http://192.168.12.1/TMI/v1/gateway?get=signal
```

- **No authentication** required for `get=signal` on this gateway/firmware.
- Returns JSON. Confirmed live against the actual G4AR on 2025 firmware.

### Real sample response (captured from a live gateway, `cid`/`gNBID` anonymized)

```json
{
  "signal": {
    "5g": {
      "antennaUsed": "External",
      "bands": ["n71"],
      "bars": 4.0,
      "cid": 12345,
      "gNBID": 999999,
      "rsrp": -96,
      "rsrq": -14,
      "rssi": -81,
      "sinr": 0
    },
    "generic": {
      "apn": "FBB.HOME",
      "hasIPv6": true,
      "registration": "registered",
      "roaming": false
    }
  }
}
```

`cid`/`gNBID` above are placeholders, not the real captured values — those two
fields plus CellMapper are enough to identify which physical tower a specific
gateway connects to, so don't paste real ones back in here. Everything else
in the sample (signal levels, band, antenna, registration) is real captured
shape/units, just not identifying on its own.

Notes on that capture: it was taken during a site survey with the **external
directional panel antenna temporarily indoors**, so the values (esp. `sinr: 0`)
are not representative of normal service — don't treat them as a baseline. A
`4g` block may also appear under `signal` on some readings; the parser already
falls back to it if `5g` is absent.

### Field reference

| Field         | Unit | Meaning                          | Rough healthy range |
|---------------|------|----------------------------------|---------------------|
| `rsrp`        | dBm  | Reference signal power (coverage)| ≥ -90 good, ≤ -105 poor |
| `rsrq`        | dB   | Reference signal quality         | ≥ -12 good, ≤ -16 poor |
| `sinr`        | dB   | Signal vs interference+noise (**headline quality metric**) | ≥ 13 good, 5–13 fair, < 5 poor |
| `rssi`        | dBm  | Total received power             | context-dependent |
| `bars`        | —    | Coarse indicator (tracks RSRP only; **ignores SINR**, so misleading on its own) | 0–5 |
| `bands`       | —    | Active band(s), e.g. `["n71"]` (low-band) or `["n41"]` (mid-band, faster) | — |
| `antennaUsed` | —    | `Internal` / `External`          | — |
| `cid`,`gNBID` | —    | Cell / gNodeB identifiers (use with CellMapper to locate the physical tower) | — |
| `registration`| —    | `registered` / other             | — |

## Architecture and the one rule that matters

The design goal is **resilience to upstream changes** (T-Mobile's gateway
firmware/schema, and separately the Ookla CLI's output shape), not feature
minimalism. There are now two parallel schema-aware boundaries, same shape:

- **`poll_gateway()`** is the only T-Mobile-schema-aware code in the app.
- **`run_speedtest()`** is the only Ookla-CLI-schema-aware code in the app.

For both:
- They fetch/execute, parse defensively, and return a *normalized* dict.
  Everything downstream (storage, HTTP API, dashboard) works on that
  normalized shape and knows nothing about the upstream JSON. **If a firmware
  update or a new Ookla CLI version renames/re-nests a field, fix it in that
  one function and nowhere else.**
- **The full raw response is always stored** in the `raw_json` column,
  regardless of whether parsing succeeded (for speed tests, this is the CLI's
  full stdout+stderr — it emits one JSON object per line, and only the final
  `"type":"result"` line is parsed). No data is ever lost to a schema change —
  you can backfill new columns from history later. Preserve this behavior.
- **Parsing never raises on expected failures.** An unreachable gateway, a
  missing/failing `speedtest` binary, or a timeout all write an `ok=0` row so
  outages appear as visible gaps, and the loop keeps running. Keep failures
  non-fatal.
- **The browser never talks to the gateway or runs the CLI directly.** It
  fetches `/data` and `/speed` from this server; only this process touches
  `192.168.12.1` or shells out to `speedtest`. This sidesteps the cross-origin
  (CORS) block you'd hit fetching the gateway from a web page. Don't move
  either into client-side JS.

### Components

- **`CONFIG_SCHEMA`** (top of file): one dict drives CLI flags, the persisted
  `config` table, and the web UI's Configuration tab. See "Configuration"
  below — don't add a new setting in only one of those three places.
- **SQLite storage**: tables `signal_history`, `speedtest_history`, and
  `config`. Writes serialized behind `_db_lock` because the background threads
  and HTTP threads share one connection (`check_same_thread=False`).
- **Background poller thread**: calls `poll_gateway()` every `POLL_SECONDS`,
  inserts a row, prints a status line.
- **Background speed test thread**: calls `run_speedtest()` every
  `speedtest_interval` (default 15 min — it's a real bandwidth-consuming
  transfer, unlike the 5s signal poll), but only when `speedtest_enabled` is
  true (checked fresh each cycle — toggling it in the Configuration tab takes
  effect on the next cycle, no restart). Guarded by `_speedtest_lock` so a
  scheduled run and a manual `/speed/run` trigger never overlap; the loser
  just no-ops rather than queuing.
- **HTTP server** (`ThreadingHTTPServer`): serves `/` (dashboard HTML),
  `/data?since=<ts>` and `/speed?since=<ts>` (JSON rows newer than a unix
  timestamp; client polls incrementally and tracks `lastTs`/`lastSpeedTs`),
  `/speed/run` (fire-and-forget trigger for an immediate speed test — refused
  with `{"started": false}` if disabled or already running),
  `/speed/status` (`{"enabled", "binary_path", "binary_found"}` — drives the
  Connection tab's state, see below), and `/speed/progress` (live in-flight
  test state, see below).
- **Live speed test progress**: `run_speedtest()` runs the CLI via
  `subprocess.Popen` (not `subprocess.run`) with `--progress=yes
  --progress-update-interval=250`, so it streams `ping`/`download`/`upload`
  lines with a live bandwidth figure and a 0-1 `progress` fraction *before*
  the final `"type":"result"` line — not just one shot at the end. Each line
  updates the module-global `_progress` dict (guarded by `_progress_lock`,
  read via `get_speedtest_progress()`), which `/speed/progress` serves as-is.
  The timeout is enforced with a `threading.Timer(...).cancel()`-guarded
  `proc.kill()` rather than `subprocess.run`'s built-in timeout, since we're
  reading lines as they arrive instead of blocking for the whole run. The
  frontend's `tickProgress()` polls this every 500ms (independent of the 5s
  `tick()`/`tickSpeed()` cadence — progress needs to feel live) and drives
  both the progress bar and, during the run, the Download/Upload/Ping/Jitter
  cells directly; when it sees `active` flip back to `false` it immediately
  calls `tickSpeed()` rather than waiting for that function's own next cycle,
  so the final stored row (and the "Run test now" button re-enabling) lands
  within ~500ms instead of up to 5s.
- **Dashboard**: three tabs, switched client-side (`.tab-btn` / `.tab-panel`,
  no reload). **Signal** tab: cyan = dB quality metrics, amber = dBm power
  metrics, dual-axis Chart.js line chart; SINR/RSRP readouts color-coded
  green/amber/red via `sinrColor`/`rsrpColor` (keep in sync with the
  thresholds table above). **Connection** tab: violet = Mbps bandwidth,
  rose = ms latency, its own dual-axis chart, plus a "Run test now" button —
  but the tab renders one of three states based on `checkSpeedStatus()`
  polling `/speed/status`: normal content (`#speed-active`), a "disabled"
  notice with a button that jumps to Configuration (`#speed-disabled-notice`),
  or a "binary not found" notice with install instructions
  (`#speed-binary-missing-notice`). The tab itself is **never hidden** —
  intentional, so there's always a place to discover why it's inactive and
  how to fix it; see "Configuration" below for the reasoning if you're
  tempted to hide it instead. Every readout has a hover tooltip (`data-tip`)
  and an info icon (`.info-btn`) that opens a shared details panel
  (`#detail-panel`); metric copy lives in the `METRIC_INFO` JS object —
  extend it there when adding a new readout. **Configuration** tab: a form
  rendered entirely from `GET /config`'s schema (`loadConfig()` in the
  HTML) — no hardcoded field list on the frontend, so a new `CONFIG_SCHEMA`
  entry appears here for free, checkbox or text/number input chosen by
  `schema.type`.

### DB schema

- **`signal_history`**: `id, ts (unix float), iso (UTC str), ok (0/1),
  connection, antenna, band, bars, rsrp, rsrq, sinr, rssi, cid, gnbid,
  raw_json`
- **`speedtest_history`**: `id, ts (unix float), iso (UTC str), ok (0/1),
  download_mbps, upload_mbps, ping_ms, jitter_ms, packet_loss, server_name,
  server_location, isp, raw_json`
- **`config`**: `key (TEXT PRIMARY KEY), value (TEXT, JSON-encoded scalar)` —
  persisted overrides written by the web UI's Configuration tab.

## Configuration

Settings live in `CONFIG_SCHEMA` (top of file) — one dict per setting with
`type` (`str` / `int` / `bool` — `bool` renders as a checkbox in the UI),
`default`, `label`, `help`, `unit`, `restart_required`, and a `validate`
function. That single schema generates three things, so adding a setting
means editing `CONFIG_SCHEMA` once, not three places:

1. **A CLI flag** (`--poll-seconds`, `--gateway-url`, etc. — `python3
   tmhi_monitor.py --help` for the full list).
2. **A row in the `config` SQLite table**, written whenever the web UI's
   Configuration tab saves a change (`POST /config`), read back into memory
   at startup and on every `Config.get()` call.
3. **A field in the web UI's Configuration tab**, rendered client-side from
   `GET /config`'s `schema` + `values` (`loadConfig()` in the dashboard JS) —
   the label, help text, and unit shown there all come from `CONFIG_SCHEMA`.

**Precedence** (lowest to highest): the `default` in `CONFIG_SCHEMA` → the
persisted `config` table (what the web UI edits, survives restarts) → a CLI
flag for that run (`Config.apply_cli_overrides()` — intentionally **not**
written back to the table, so a one-off `--poll-seconds 2` doesn't silently
become permanent).

**`db_path`** is the one setting *not* in `CONFIG_SCHEMA` / the Configuration
tab: it has to be known before the DB can even be opened to read persisted
config, so it's CLI-only (`--db-path`, default `tmhi_signal.db`), shown
read-only in the UI.

**Live reload**: `poll_gateway()`, `run_speedtest()`, and the two background
loops call `CFG.get(...)` on every cycle rather than caching a value, so
changes saved via the Configuration tab apply on the *next* poll/test cycle
with no restart. The exceptions are `http_host` and `http_port` — the HTTP
server socket is already bound at startup, so changing either is flagged
`restart_required` in both `CONFIG_SCHEMA` and the `POST /config` response,
and only takes effect after you restart the script. `http_host` defaults to
`0.0.0.0` (all interfaces, reachable from other devices on the LAN); set it
to `127.0.0.1` to restrict the dashboard to this machine only.

**Validation**: `Config.set()` casts via `type` and rejects via `validate`
(e.g. `poll_seconds >= 1`, `1 <= http_port <= 65535`) before writing to the
DB; `POST /config` reports failures per-field in `errors` rather than
rejecting the whole request, so one bad field doesn't block the rest.

**Speed testing is opt-out, not opt-in**: `speedtest_enabled` defaults to
`true` because that's what the app already did before the toggle existed —
defaulting it off would have been a silent behavior change nobody asked for.
Flip the default in `CONFIG_SCHEMA` if that's ever wrong for a fresh install.
When it's off, or the `speedtest_bin` binary can't be found, the Connection
tab **stays visible** rather than disappearing — it was tempting to hide it,
but the tab is also the one place a user finds the "why is this off, and how
do I turn it on" explanation (`#speed-disabled-notice` /
`#speed-binary-missing-notice`, driven by `GET /speed/status`). A hidden tab
can't show that. Keep this in mind before "cleaning up" the UI by hiding
inactive tabs — discoverability of the fix matters more than decluttering
here.

## Deployment

`tmhi-monitor.service` (repo root) is a systemd **user** unit — deliberately
not a system-wide `/etc/systemd/system/` unit, because installing one of
those needs `sudo`, and this environment's `sudo` has no working TTY for a
password prompt (discovered while setting this up — any root-requiring step
has to be handed to the human to run in a real terminal, not executed here).
The user-unit path needs no root at all: `systemctl --user enable --now`,
plus `loginctl enable-linger "$USER"` so it survives reboot without an active
login session. Full install steps are in `README.md`. If `WorkingDirectory`
or `ExecStart` in the unit ever drift from the actual checkout path, fix both
the repo copy and `~/.config/systemd/user/tmhi-monitor.service` — they're not
symlinked, just copied.

`Restart=on-failure` recovers crashes; it is not a substitute for the live
config-reload behavior described above, and doesn't fire just because a
setting changed.

## Known constraints and gotchas

- **Firmware drift is expected.** T-Mobile has changed these endpoints across
  revisions. This is the entire reason for the `poll_gateway()` boundary + raw
  storage. Assume the schema can shift under you.
- **Auth-gated endpoints.** `get=signal` needs no auth, but other operations
  (reboot, config writes, some stat pages) sit behind the **gateway admin
  login** (the gateway's own password, not the T-Mobile account). Any feature
  touching those needs an auth handshake first — not yet implemented.
- **Alternate/fallback endpoints** seen on TMHI gateways, for reference if the
  primary path ever 404s on this unit:
  - `http://192.168.12.1/TMI/v1/gateway?get=all` — superset (device + signal + more).
  - `http://192.168.12.1/fastmile_radio_status_web_app.cgi` — **older Nokia/"trashcan" style**, different JSON shape: signal lives at
    `cell_5G_stats_cfg[0].stat.RSRPCurrent` / `.SNRCurrent` / `.RSRQCurrent`,
    and a sentinel value of **`-32768` means "no value"** (filter it out).
  - The G4AR is Arcadyan, so the `TMI/v1` path is the right one here; the `.cgi`
    shape is documented only as a defensive fallback.
- **Reference implementation** for the full set of gateway endpoints, auth flow,
  and per-model differences: the open-source **HINT Control** app
  (`github.com/zacharee/HINTControl`). Consult it before reverse-engineering
  anything new.
- **Speed tests cost real bandwidth and time** (~15–30s each, saturating the
  link). Don't lower `SPEEDTEST_INTERVAL` casually — this isn't a lightweight
  poll like the signal check.
- **Ookla CLI output is multi-line NDJSON**, not a single JSON document: log
  lines interleave with progress, and the row you want is the *last* line
  where `"type":"result"`. `run_speedtest()` already handles this — don't
  `json.loads()` the whole stdout blob directly.
- **`speedtest --accept-license --accept-gdpr` is required** on first run (and
  harmless on every run after) or the CLI blocks waiting for interactive
  consent. Already baked into `run_speedtest()`'s invocation — don't drop it.

## Backlog (roughly prioritized) — none of this is committed yet

The current build is a deliberate v0 foundation. Build these as **layers on top
of the existing poll loop / SQLite store**; don't rearchitect to add them.

1. **Alerting** — fire on SINR dropping below a threshold, band dropping from
   mid-band (n41) to low-band (n71) only, or gateway going `ok=0` for N polls.
2. **CSV export** — endpoint or CLI flag; the SQLite file already holds
   everything, so this is a read+format.
3. **Data retention / downsampling** — prune or roll up old rows so the DB
   doesn't grow unbounded at a 5s cadence.
4. **Aiming / site-survey mode** — a fast-poll live view with peak-hold on SINR
   for physically aiming the external directional antenna; optionally
   per-location tagging to compare mount spots. (Was discussed; deferred as not
   the immediate need.)
5. **Grafana/Prometheus feed** — expose a `/metrics` endpoint for external
   dashboards instead of / alongside the built-in one.

## Working preferences

- Direct, structurally honest feedback. Push back when something doesn't add up.
- Keep the v0 philosophy: build the simplest thing that meets the immediate
  need, then iterate. "Avoid complexity" here specifically means **don't couple
  the app to T-Mobile's schema** — not "avoid features."
- Preserve the `poll_gateway()` boundary and `raw_json` capture in every change.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<optional scope>): <description>`, imperative mood, no trailing period.

Common types here: `feat`, `fix`, `docs`, `refactor`, `chore`. Add a body when
the *why* isn't obvious from the diff (e.g. a firmware quirk being worked
around). Breaking changes get a `!` after the type/scope (e.g. `feat!:`) plus
a `BREAKING CHANGE:` footer.
