# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small self-hosted tool that polls a **T-Mobile Home Internet gateway**
(Arcadyan **TMO-G4AR**) for cellular signal telemetry, stores every reading in
SQLite, and serves a live-updating browser dashboard.

It exists because T-Mobile's current **T-Life** app no longer exposes the raw
radio metrics (RSRP / RSRQ / SINR / RSSI / band) that the retired T-Mobile
Internet app used to show. Those metrics are still available from the gateway's
**local HTTP API**, which is what this tool reads.

Single file today: `tmhi_monitor.py`. Python 3.8+, **standard library only** on
the backend. The dashboard pulls Chart.js from a CDN.

## Run it

```bash
python3 tmhi_monitor.py
# then open http://localhost:8073   (Ctrl-C to stop)
```

No dependencies to install. Must run on a machine that can reach the gateway at
`192.168.12.1`.

## The endpoint (verified working)

```
GET http://192.168.12.1/TMI/v1/gateway?get=signal
```

- **No authentication** required for `get=signal` on this gateway/firmware.
- Returns JSON. Confirmed live against the actual G4AR on 2025 firmware.

### Real sample response (captured from the live gateway)

```json
{
  "signal": {
    "5g": {
      "antennaUsed": "External",
      "bands": ["n71"],
      "bars": 4.0,
      "cid": 302,
      "gNBID": 1083128,
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

The design goal is **resilience to T-Mobile changing their schema/firmware**,
not feature minimalism. Keep this boundary intact:

- **`poll_gateway()` is the only schema-aware code in the app.** It fetches,
  parses defensively, and returns a *normalized* dict. Everything downstream
  (storage, HTTP API, dashboard) works on that normalized shape and knows
  nothing about T-Mobile's JSON. **If a firmware update renames or re-nests a
  field, fix it here and nowhere else.**
- **The full raw response is always stored** in the `raw_json` column,
  regardless of whether parsing succeeded. This means no data is ever lost to a
  schema change — you can backfill new columns from history later. Preserve this
  behavior.
- **Parsing never raises on expected failures.** An unreachable gateway or bad
  body writes an `ok=0` row so outages appear as visible gaps, and the poller
  keeps running. Keep failures non-fatal.
- **The browser never talks to the gateway directly.** It fetches `/data` from
  this server; only this process talks to `192.168.12.1`. This sidesteps the
  cross-origin (CORS) block you'd hit fetching the gateway from a web page —
  the gateway doesn't send CORS headers. Don't move the gateway fetch into
  client-side JS.

### Components

- **Config block** (top of file): `GATEWAY_URL`, `POLL_SECONDS`, `HTTP_PORT`,
  `DB_PATH`, `FETCH_TIMEOUT`.
- **SQLite storage**: table `signal_history`. Writes serialized behind
  `_db_lock` because the poller thread and HTTP threads share one connection
  (`check_same_thread=False`).
- **Background poller thread**: calls `poll_gateway()` every `POLL_SECONDS`,
  inserts a row, prints a status line.
- **HTTP server** (`ThreadingHTTPServer`): serves `/` (dashboard HTML) and
  `/data?since=<ts>` (JSON rows newer than a unix timestamp; client polls
  incrementally and tracks `lastTs`).
- **Dashboard**: instrument-panel styling. Cyan = dB quality metrics, amber =
  dBm power metrics, mapped to a dual-axis Chart.js line chart. SINR and RSRP
  readouts are color-coded green/amber/red using the thresholds in the table
  above (see `sinrColor` / `rsrpColor` in the HTML — keep these in sync with any
  documented threshold changes).

### DB schema (`signal_history`)

`id, ts (unix float), iso (UTC str), ok (0/1), connection, antenna, band,
bars, rsrp, rsrq, sinr, rssi, cid, gnbid, raw_json`

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
6. **Config file or CLI args** — move the config block to `--flags` or a small
   config file once more than a couple of knobs exist.

## Working preferences

- Direct, structurally honest feedback. Push back when something doesn't add up.
- Keep the v0 philosophy: build the simplest thing that meets the immediate
  need, then iterate. "Avoid complexity" here specifically means **don't couple
  the app to T-Mobile's schema** — not "avoid features."
- Preserve the `poll_gateway()` boundary and `raw_json` capture in every change.
