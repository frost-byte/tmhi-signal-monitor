#!/usr/bin/env python3
"""
TMHI Signal Monitor  -  v0 foundation
=====================================

Polls a T-Mobile Home Internet gateway's local signal endpoint, stores every
reading in SQLite, and serves a live-updating browser dashboard.

Design note (the whole point of v0):
  The ONLY part that knows T-Mobile's JSON shape is poll_gateway() below.
  Everything else works on a normalized dict. Every raw response is also stored
  verbatim in the `raw_json` column, so even if T-Mobile renames a field and the
  parser misses it, the underlying data is never lost -- you can backfill later.
  When a firmware update breaks something, you fix ONE function, not the app.

Requirements: Python 3.8+  (standard library only). A browser for the dashboard.
Also polls upload/download/ping/jitter via the Ookla Speedtest CLI (a separate
binary, shelled out to -- see CONFIG_SCHEMA["speedtest_bin"] below; toggle the
whole feature with CONFIG_SCHEMA["speedtest_enabled"]). Missing/failing binary
degrades to ok=0 rows, same as an unreachable gateway; it never crashes the app.
Run:          python3 tmhi_monitor.py
Then open:    http://localhost:8073
Stop:         Ctrl-C
"""

import argparse
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# CONFIG  -  one schema drives CLI flags, the `config` DB table, and the web
# UI's Configuration tab. Add a setting here and all three pick it up.
#
# Precedence (lowest to highest): "default" below  <  persisted `config` table
# (what the web UI edits, survives restarts)  <  CLI flag for this run only
# (never written back to the table -- a one-off --poll-seconds doesn't become
# permanent). db_path is the one exception: it has to be known before the DB
# can be opened, so it's CLI-only and never lives in the table.
# ----------------------------------------------------------------------------
CONFIG_SCHEMA = {
    "gateway_url": {
        "type": "str", "default": "http://192.168.12.1/TMI/v1/gateway?get=signal",
        "label": "Gateway URL",
        "help": "T-Mobile gateway signal endpoint to poll.",
        "restart_required": False,
        "validate": lambda v: isinstance(v, str) and v.startswith("http"),
    },
    "poll_seconds": {
        "type": "int", "default": 5, "unit": "s",
        "label": "Signal Poll Interval",
        "help": "How often to poll the gateway for signal readings. Takes effect on the next poll cycle.",
        "restart_required": False,
        "validate": lambda v: v >= 1,
    },
    "fetch_timeout": {
        "type": "int", "default": 8, "unit": "s",
        "label": "Gateway Fetch Timeout",
        "help": "How long to wait for the gateway to respond before giving up on a poll.",
        "restart_required": False,
        "validate": lambda v: v >= 1,
    },
    "http_host": {
        "type": "str", "default": "0.0.0.0",
        "label": "Dashboard HTTP Host",
        "help": "Address the web dashboard listens on. '0.0.0.0' = all interfaces (reachable from "
                "other devices on the network); '127.0.0.1' = this machine only.",
        "restart_required": True,
        "validate": lambda v: isinstance(v, str) and len(v) > 0,
    },
    "http_port": {
        "type": "int", "default": 8073,
        "label": "Dashboard HTTP Port",
        "help": "Port the web dashboard listens on.",
        "restart_required": True,
        "validate": lambda v: 1 <= v <= 65535,
    },
    "speedtest_enabled": {
        "type": "bool", "default": True,
        "label": "Enable Speed Testing",
        "help": "Turn periodic and manual download/upload/ping/jitter tests on or off. Takes effect "
                "on the next scheduled cycle; manual 'Run test now' is refused while off.",
        "restart_required": False,
        "validate": lambda v: isinstance(v, bool),
    },
    "speedtest_bin": {
        "type": "str", "default": os.path.expanduser("~/.local/bin/speedtest"),
        "label": "Speedtest Binary Path",
        "help": "Path to the Ookla Speedtest CLI executable.",
        "restart_required": False,
        "validate": lambda v: isinstance(v, str) and len(v) > 0,
    },
    "speedtest_interval": {
        "type": "int", "default": 900, "unit": "s",
        "label": "Speed Test Interval",
        "help": "How often to run a full download/upload/ping/jitter test. Each run saturates the "
                "link for 15-30s, so don't set this too low. Takes effect on the next cycle.",
        "restart_required": False,
        "validate": lambda v: v >= 30,
    },
    "speedtest_timeout": {
        "type": "int", "default": 90, "unit": "s",
        "label": "Speed Test Timeout",
        "help": "How long to wait for a speed test to finish before treating it as failed.",
        "restart_required": False,
        "validate": lambda v: v >= 5,
    },
}

def _cast_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)

_TYPE_CASTERS = {"str": str, "int": int, "bool": _cast_bool}

def config_schema_public():
    """CONFIG_SCHEMA without the non-serializable validate() lambdas, for the web UI."""
    return {k: {kk: vv for kk, vv in s.items() if kk != "validate"} for k, s in CONFIG_SCHEMA.items()}

# ----------------------------------------------------------------------------
# STORAGE
# ----------------------------------------------------------------------------
def db_connect(db_path):
    # check_same_thread=False: background threads + HTTP threads share the file.
    # Writes are serialized behind _db_lock below.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

_db_lock = threading.Lock()

def db_init(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            REAL NOT NULL,          -- unix epoch seconds
            iso           TEXT NOT NULL,          -- human-readable UTC
            ok            INTEGER NOT NULL,       -- 1 = gateway answered, 0 = fetch/parse failed
            connection    TEXT,                   -- registration/connection state
            antenna       TEXT,                   -- antennaUsed (Internal/External)
            band          TEXT,                   -- e.g. "n71" or "n41+n71"
            bars          REAL,
            rsrp          REAL,                   -- dBm  (coverage)
            rsrq          REAL,                   -- dB   (quality)
            sinr          REAL,                   -- dB   (usable signal vs noise -- the headline)
            rssi          REAL,                   -- dBm
            cid           INTEGER,
            gnbid         INTEGER,
            raw_json      TEXT                    -- full verbatim response, always kept
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON signal_history(ts)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS speedtest_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            iso             TEXT NOT NULL,
            ok              INTEGER NOT NULL,   -- 1 = test completed, 0 = failed/timed out
            download_mbps   REAL,
            upload_mbps     REAL,
            ping_ms         REAL,
            jitter_ms       REAL,
            packet_loss     REAL,               -- percent, when reported
            server_name     TEXT,
            server_location TEXT,
            isp             TEXT,
            raw_json        TEXT                -- full CLI stdout+stderr, always kept
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_speed_ts ON speedtest_history(ts)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL   -- JSON-encoded scalar, so ints/strings round-trip cleanly
        )
    """)
    conn.commit()

def db_insert(conn, row):
    with _db_lock:
        conn.execute("""
            INSERT INTO signal_history
              (ts, iso, ok, connection, antenna, band, bars, rsrp, rsrq, sinr, rssi, cid, gnbid, raw_json)
            VALUES
              (:ts, :iso, :ok, :connection, :antenna, :band, :bars, :rsrp, :rsrq, :sinr, :rssi, :cid, :gnbid, :raw_json)
        """, row)
        conn.commit()

def db_rows_since(conn, since_ts):
    with _db_lock:
        cur = conn.execute(
            "SELECT ts, iso, ok, connection, antenna, band, bars, rsrp, rsrq, sinr, rssi, cid, gnbid "
            "FROM signal_history WHERE ts > ? ORDER BY ts ASC LIMIT 5000",
            (since_ts,),
        )
        return [dict(r) for r in cur.fetchall()]

def db_insert_speed(conn, row):
    with _db_lock:
        conn.execute("""
            INSERT INTO speedtest_history
              (ts, iso, ok, download_mbps, upload_mbps, ping_ms, jitter_ms, packet_loss,
               server_name, server_location, isp, raw_json)
            VALUES
              (:ts, :iso, :ok, :download_mbps, :upload_mbps, :ping_ms, :jitter_ms, :packet_loss,
               :server_name, :server_location, :isp, :raw_json)
        """, row)
        conn.commit()

def db_speed_rows_since(conn, since_ts):
    with _db_lock:
        cur = conn.execute(
            "SELECT ts, iso, ok, download_mbps, upload_mbps, ping_ms, jitter_ms, packet_loss, "
            "server_name, server_location, isp "
            "FROM speedtest_history WHERE ts > ? ORDER BY ts ASC LIMIT 5000",
            (since_ts,),
        )
        return [dict(r) for r in cur.fetchall()]

def db_config_load(conn):
    with _db_lock:
        cur = conn.execute("SELECT key, value FROM config")
        rows = cur.fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}

def db_config_set(conn, key, value):
    with _db_lock:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        conn.commit()

class Config:
    """Thread-safe live settings: defaults, overlaid with whatever's persisted
    in the `config` table, overlaid with this run's CLI flags (not persisted).
    poller_loop()/speedtest_loop()/poll_gateway()/run_speedtest() read through
    this on every cycle, so web-UI edits apply without a restart."""

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.Lock()
        self._values = {k: s["default"] for k, s in CONFIG_SCHEMA.items()}
        persisted = db_config_load(conn)
        self._values.update({k: v for k, v in persisted.items() if k in CONFIG_SCHEMA})

    def get(self, key):
        with self._lock:
            return self._values[key]

    def as_dict(self):
        with self._lock:
            return dict(self._values)

    def set(self, key, raw_value):
        if key not in CONFIG_SCHEMA:
            raise KeyError(f"unknown setting: {key}")
        schema = CONFIG_SCHEMA[key]
        value = _TYPE_CASTERS[schema["type"]](raw_value)
        validator = schema.get("validate")
        if validator and not validator(value):
            raise ValueError(f"invalid value for {key}: {raw_value!r}")
        with self._lock:
            self._values[key] = value
        db_config_set(self._conn, key, value)
        return value

    def apply_cli_overrides(self, overrides):
        """overrides: {key: value or None}. None means "not passed on the CLI".
        Intentionally NOT persisted -- applies to this process only."""
        with self._lock:
            for k, v in overrides.items():
                if v is not None:
                    self._values[k] = v

CFG = None            # set in main(); a module global like the old constants were
RESOLVED_DB_PATH = None  # for display only -- db_path itself is never in Config

# ----------------------------------------------------------------------------
# THE FRAGILE BOUNDARY  -  the only T-Mobile-schema-aware code in the app.
# If a firmware update changes field names or nesting, fix it HERE and nowhere
# else. Parsing is deliberately defensive: missing fields become None rather
# than raising, and the raw response is returned untouched for storage.
# ----------------------------------------------------------------------------
def _dig(d, *path, default=None):
    """Safely walk nested dict keys; return default if any hop is missing."""
    cur = d
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur

def poll_gateway():
    """
    Returns a normalized reading dict (always includes 'ok' and 'raw_json').
    Never raises for expected failures -- a dead/unreachable gateway just
    produces an ok=0 row so gaps are visible in the history.
    """
    now = time.time()
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    base = {
        "ts": now, "iso": iso, "ok": 0,
        "connection": None, "antenna": None, "band": None, "bars": None,
        "rsrp": None, "rsrq": None, "sinr": None, "rssi": None,
        "cid": None, "gnbid": None, "raw_json": None,
    }

    try:
        req = urllib.request.Request(CFG.get("gateway_url"), headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=CFG.get("fetch_timeout")) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        base["raw_json"] = json.dumps({"_fetch_error": str(e)})
        return base

    base["raw_json"] = raw_text

    try:
        data = json.loads(raw_text)
    except Exception as e:
        base["raw_json"] = json.dumps({"_parse_error": str(e), "_body": raw_text[:2000]})
        return base

    # --- normalize. Prefer 5G; fall back to 4G if 5G block is absent. -------
    sig      = _dig(data, "signal", default={})
    g5       = _dig(sig, "5g", default={}) or {}
    g4       = _dig(sig, "4g", default={}) or {}
    generic  = _dig(sig, "generic", default={}) or {}
    primary  = g5 if g5 else g4

    bands = primary.get("bands")
    if isinstance(bands, list):
        band = "+".join(str(b) for b in bands) if bands else None
    else:
        band = bands  # already a string, or None

    base.update({
        "ok": 1,
        "connection": generic.get("registration"),
        "antenna": primary.get("antennaUsed"),
        "band": band,
        "bars": primary.get("bars"),
        "rsrp": primary.get("rsrp"),
        "rsrq": primary.get("rsrq"),
        "sinr": primary.get("sinr"),
        "rssi": primary.get("rssi"),
        "cid": primary.get("cid"),
        "gnbid": primary.get("gNBID"),
    })
    return base

# ----------------------------------------------------------------------------
# BACKGROUND POLLER
# ----------------------------------------------------------------------------
def poller_loop(conn, stop_event):
    while not stop_event.is_set():
        reading = poll_gateway()
        try:
            db_insert(conn, reading)
            status = "ok" if reading["ok"] else "NO DATA"
            print(f"[{reading['iso']}] {status}  "
                  f"band={reading['band']}  rsrp={reading['rsrp']}  "
                  f"rsrq={reading['rsrq']}  sinr={reading['sinr']}  bars={reading['bars']}")
        except Exception as e:
            print(f"[poller] insert failed: {e}")
        stop_event.wait(CFG.get("poll_seconds"))

# ----------------------------------------------------------------------------
# SPEED TEST  -  the only Ookla-CLI-schema-aware code in the app. Same rule as
# poll_gateway(): if Ookla changes their JSON, fix it HERE. Full CLI output is
# always stored in raw_json, and failures (missing binary, no network, timeout)
# produce a normal ok=0 row instead of raising.
# ----------------------------------------------------------------------------
def run_speedtest():
    now = time.time()
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    base = {
        "ts": now, "iso": iso, "ok": 0,
        "download_mbps": None, "upload_mbps": None, "ping_ms": None, "jitter_ms": None,
        "packet_loss": None, "server_name": None, "server_location": None, "isp": None,
        "raw_json": None,
    }

    try:
        proc = subprocess.run(
            [CFG.get("speedtest_bin"), "--accept-license", "--accept-gdpr", "-f", "json"],
            capture_output=True, text=True, timeout=CFG.get("speedtest_timeout"),
        )
    except Exception as e:
        base["raw_json"] = json.dumps({"_exec_error": str(e)})
        return base

    stdout = proc.stdout or ""
    base["raw_json"] = stdout + (proc.stderr or "")

    # The CLI prints one JSON object per line (progress/log lines, then a
    # final line with "type":"result"). Only the result line matters here.
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            result = obj

    if result is None:
        return base

    dl     = _dig(result, "download", default={}) or {}
    ul     = _dig(result, "upload", default={}) or {}
    ping   = _dig(result, "ping", default={}) or {}
    server = _dig(result, "server", default={}) or {}

    def bytes_to_mbps(b):
        return round(b * 8 / 1_000_000, 2) if isinstance(b, (int, float)) else None

    def round1(v):
        return round(v, 1) if isinstance(v, (int, float)) else None

    base.update({
        "ok": 1,
        "download_mbps": bytes_to_mbps(dl.get("bandwidth")),
        "upload_mbps": bytes_to_mbps(ul.get("bandwidth")),
        "ping_ms": round1(ping.get("latency")),
        "jitter_ms": round1(ping.get("jitter")),
        "packet_loss": result.get("packetLoss"),
        "server_name": server.get("name"),
        "server_location": server.get("location"),
        "isp": result.get("isp"),
    })
    return base

# ----------------------------------------------------------------------------
# BACKGROUND SPEED TEST LOOP
# ----------------------------------------------------------------------------
_speedtest_lock = threading.Lock()

def run_one_speedtest(conn):
    """Run + store a single speed test. Returns False without running if one
    is already in progress (scheduled loop and manual /speed/run share this)."""
    if not _speedtest_lock.acquire(blocking=False):
        return False
    try:
        reading = run_speedtest()
        try:
            db_insert_speed(conn, reading)
            status = "ok" if reading["ok"] else "FAILED"
            print(f"[{reading['iso']}] speedtest {status}  "
                  f"down={reading['download_mbps']}Mbps  up={reading['upload_mbps']}Mbps  "
                  f"ping={reading['ping_ms']}ms  jitter={reading['jitter_ms']}ms")
        except Exception as e:
            print(f"[speedtest] insert failed: {e}")
    finally:
        _speedtest_lock.release()
    return True

def speedtest_loop(conn, stop_event):
    while not stop_event.is_set():
        if CFG.get("speedtest_enabled"):
            run_one_speedtest(conn)
        stop_event.wait(CFG.get("speedtest_interval"))

def speedtest_binary_status():
    """Is the configured speedtest binary actually present and executable?"""
    path = CFG.get("speedtest_bin")
    return os.path.isfile(path) and os.access(path, os.X_OK)

# ----------------------------------------------------------------------------
# WEB DASHBOARD  -  served inline; talks only to this server (no CORS issues).
# The browser can't fetch the gateway directly (cross-origin), so the page
# fetches /data from here, and only this process talks to 192.168.12.1.
# ----------------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TMHI Signal Monitor</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --ink:#0d1117; --panel:#141b24; --line:#233040;
    --text:#c9d4df; --dim:#6b7c8f;
    --amber:#f5a524;   /* dBm  power  metrics */
    --cyan:#38bdf8;    /* dB   quality metrics */
    --violet:#a78bfa;  /* Mbps bandwidth metrics */
    --rose:#f472b6;    /* ms   latency metrics */
    --good:#3fb950; --warn:#d29922; --bad:#f85149;
    --mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--ink);color:var(--text);
       font-family:var(--mono);font-size:14px;line-height:1.4}
  header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
         padding:14px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;margin:0;font-weight:600}
  header .meta{color:var(--dim);font-size:12px}
  #status-dot{display:inline-block;width:9px;height:9px;border-radius:50%;
              background:var(--bad);margin-right:6px;vertical-align:middle}
  #status-dot.live{background:var(--good);box-shadow:0 0 8px var(--good)}
  .wrap{padding:20px;max-width:1100px;margin:0 auto}
  .tabs{display:flex;margin-bottom:16px;border-bottom:1px solid var(--line)}
  .tab-btn{background:none;border:none;color:var(--dim);font-family:var(--mono);
    font-size:12px;letter-spacing:.08em;text-transform:uppercase;padding:10px 4px;
    margin-right:22px;cursor:pointer;border-bottom:2px solid transparent}
  .tab-btn:hover{color:var(--text)}
  .tab-btn.active{color:var(--text);border-bottom-color:var(--cyan)}
  .tab-panel.hidden{display:none}
  .readouts{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:20px}
  .cell{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
  .cell .lab{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-bottom:6px}
  .cell .val{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
  .cell .unit{font-size:12px;color:var(--dim);margin-left:3px}
  .cell.power .val{color:var(--amber)}
  .cell.quality .val{color:var(--cyan)}
  .cell.bw .val{color:var(--violet)}
  .cell.lat .val{color:var(--rose)}
  .cell.small .val{font-size:15px;font-weight:500;color:var(--text)}
  .server-line{color:var(--dim);font-size:12px;margin:-10px 0 18px}
  .btn{background:var(--panel);border:1px solid var(--line);color:var(--text);
    border-radius:6px;padding:5px 12px;font-family:var(--mono);font-size:12px;cursor:pointer}
  .btn:hover{border-color:var(--cyan)}
  .btn:disabled{opacity:.5;cursor:default}
  .config-row{margin-bottom:16px}
  .config-label{display:block;font-size:12px;font-weight:600;color:var(--text);margin-bottom:3px}
  .config-label .restart-tag{font-weight:400;color:var(--warn);text-transform:none;letter-spacing:normal}
  .config-hint{font-size:11.5px;color:var(--dim);margin-bottom:6px;line-height:1.4}
  .config-input{width:100%;max-width:420px;background:var(--ink);border:1px solid var(--line);
    border-radius:6px;color:var(--text);font-family:var(--mono);font-size:13px;padding:7px 10px}
  .config-input:focus{outline:none;border-color:var(--cyan)}
  .notice{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:16px 18px;margin-bottom:20px}
  .notice.hidden{display:none}
  .notice-title{font-size:13px;font-weight:600;color:var(--text);margin-bottom:8px}
  .notice p{font-size:12.5px;line-height:1.6;color:var(--dim);margin:0 0 10px}
  .notice code{background:var(--ink);border:1px solid var(--line);border-radius:4px;
    padding:1px 5px;color:var(--text)}
  .notice pre{background:var(--ink);border:1px solid var(--line);border-radius:6px;
    padding:10px 12px;font-size:11.5px;line-height:1.5;overflow-x:auto;margin:0 0 10px;color:var(--text)}
  #speed-active.hidden{display:none}
  .lab{display:flex;align-items:center;gap:5px}
  [data-tip]{position:relative}
  [data-tip]:hover::after,[data-tip]:focus-within::after{
    content:attr(data-tip);position:absolute;left:0;bottom:100%;margin-bottom:8px;
    background:#1c2530;color:var(--text);border:1px solid var(--line);
    padding:6px 9px;border-radius:6px;font-size:11px;line-height:1.35;
    width:max-content;max-width:220px;z-index:20;box-shadow:0 4px 12px rgba(0,0,0,.45);
    pointer-events:none;text-transform:none;letter-spacing:normal;font-weight:400}
  .info-btn{display:inline-flex;align-items:center;justify-content:center;
    width:14px;height:14px;border-radius:50%;border:1px solid var(--dim);color:var(--dim);
    background:transparent;font:italic 10px Georgia,serif;line-height:1;cursor:pointer;padding:0}
  .info-btn:hover,.info-btn:focus{border-color:var(--cyan);color:var(--cyan);outline:none}
  .info-btn.active{border-color:var(--cyan);color:var(--cyan);background:rgba(56,189,248,.15)}
  .bars-icon{display:flex;align-items:flex-end;gap:3px;height:24px;margin-top:2px}
  .bars-icon .bar{width:6px;border-radius:2px 2px 0 0;background:var(--line)}
  .bars-icon .bar.on{background:var(--good)}
  .bars-icon .bar:nth-child(1){height:40%}
  .bars-icon .bar:nth-child(2){height:55%}
  .bars-icon .bar:nth-child(3){height:70%}
  .bars-icon .bar:nth-child(4){height:85%}
  .bars-icon .bar:nth-child(5){height:100%}
  .bars-num{font-size:11px;color:var(--dim);margin-top:4px}
  .chartbox{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
  .chart-tools{display:flex;gap:16px;align-items:center;margin-bottom:8px;flex-wrap:wrap;font-size:12px;color:var(--dim)}
  .chart-tools label{cursor:pointer;user-select:none}
  .chart-wrap{position:relative;height:320px}
  canvas{width:100%!important;height:100%!important}
  .detail-panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:14px 16px;margin-top:14px}
  .detail-panel.hidden{display:none}
  .detail-head{display:flex;justify-content:space-between;align-items:center;gap:12px;
    font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--text);
    border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:10px}
  #detail-close{background:none;border:none;color:var(--dim);font-size:18px;
    cursor:pointer;line-height:1;padding:0 4px}
  #detail-close:hover{color:var(--text)}
  .detail-body{font-size:12.5px;line-height:1.6}
  .detail-body p{margin:0 0 10px}
  .detail-body code{background:var(--ink);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
  .range-table{border-collapse:collapse;font-size:12px;margin-top:4px}
  .range-table td{padding:3px 10px 3px 0;white-space:nowrap}
  .range-table td.tag{font-weight:600;border-radius:4px;padding:1px 8px;color:#0d1117;text-align:center}
  .range-table td.tag.good{background:var(--good)}
  .range-table td.tag.warn{background:var(--warn)}
  .range-table td.tag.bad{background:var(--bad)}
  footer{color:var(--dim);font-size:11px;text-align:center;padding:16px}
  .swatch{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:middle}
</style>
</head>
<body>
<header>
  <h1>TMHI Signal Monitor</h1>
  <span class="meta"><span id="status-dot"></span><span id="status-text">connecting…</span></span>
  <span class="meta" id="ident"></span>
</header>

<div class="wrap">
  <div class="tabs">
    <button class="tab-btn active" data-tab="signal">Signal</button>
    <button class="tab-btn" data-tab="speed">Connection</button>
    <button class="tab-btn" data-tab="config">Configuration</button>
  </div>

  <section class="tab-panel" id="panel-signal">
    <div class="readouts">
      <div class="cell quality" data-tip="Signal-to-noise ratio — the single best overall quality indicator">
        <div class="lab">SINR<button class="info-btn" data-metric="sinr" aria-label="About SINR">i</button></div>
        <div class="val" id="v-sinr">–<span class="unit">dB</span></div>
      </div>
      <div class="cell power" data-tip="Signal power — reflects raw coverage strength">
        <div class="lab">RSRP<button class="info-btn" data-metric="rsrp" aria-label="About RSRP">i</button></div>
        <div class="val" id="v-rsrp">–<span class="unit">dBm</span></div>
      </div>
      <div class="cell quality" data-tip="Signal quality, accounting for interference from other cells">
        <div class="lab">RSRQ<button class="info-btn" data-metric="rsrq" aria-label="About RSRQ">i</button></div>
        <div class="val" id="v-rsrq">–<span class="unit">dB</span></div>
      </div>
      <div class="cell power" data-tip="Total received power — signal plus interference plus noise">
        <div class="lab">RSSI<button class="info-btn" data-metric="rssi" aria-label="About RSSI">i</button></div>
        <div class="val" id="v-rssi">–<span class="unit">dBm</span></div>
      </div>
      <div class="cell small" data-tip="Active 5G frequency band(s), e.g. n71">
        <div class="lab">Band<button class="info-btn" data-metric="band" aria-label="About Band">i</button></div>
        <div class="val" id="v-band">–</div>
      </div>
      <div class="cell small" data-tip="Coarse indicator based on RSRP only — ignores SINR">
        <div class="lab">Bars<button class="info-btn" data-metric="bars" aria-label="About Bars">i</button></div>
        <div class="bars-icon" id="v-bars-icon" aria-hidden="true">
          <span class="bar"></span><span class="bar"></span><span class="bar"></span><span class="bar"></span><span class="bar"></span>
        </div>
        <div class="bars-num" id="v-bars-num">–</div>
      </div>
      <div class="cell small" data-tip="Which antenna the gateway is currently using">
        <div class="lab">Antenna<button class="info-btn" data-metric="antenna" aria-label="About Antenna">i</button></div>
        <div class="val" id="v-ant">–</div>
      </div>
      <div class="cell small" data-tip="Gateway's registration status on the network">
        <div class="lab">State<button class="info-btn" data-metric="conn" aria-label="About connection state">i</button></div>
        <div class="val" id="v-conn">–</div>
      </div>
    </div>

    <div class="chartbox">
      <div class="chart-tools">
        <span><span class="swatch" style="background:#38bdf8"></span>quality axis (dB) — SINR, RSRQ</span>
        <span><span class="swatch" style="background:#f5a524"></span>power axis (dBm) — RSRP, RSSI</span>
        <label style="margin-left:auto"><input type="checkbox" id="tgl-power" checked> show power</label>
      </div>
      <div class="chart-wrap"><canvas id="chart"></canvas></div>
    </div>
  </section>

  <section class="tab-panel hidden" id="panel-speed">
    <div class="notice hidden" id="speed-disabled-notice">
      <div class="notice-title">Speed testing is disabled</div>
      <p>Periodic and manual download/upload/ping/jitter tests are turned off. Signal monitoring
      on the other tab is unaffected.</p>
      <button class="btn" id="speed-enable-btn">Enable in Configuration</button>
    </div>

    <div class="notice hidden" id="speed-binary-missing-notice">
      <div class="notice-title">Speedtest binary not found</div>
      <p>Speed testing is enabled, but no executable was found at
      <code id="speed-binary-path"></code>. Install the Ookla Speedtest CLI (no root needed):</p>
      <pre>curl -sL -o /tmp/speedtest.tgz https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz
tar -xzf /tmp/speedtest.tgz -C ~/.local/bin speedtest
chmod +x ~/.local/bin/speedtest</pre>
      <p>Or point <strong>Speedtest Binary Path</strong> at wherever it's already installed.</p>
      <button class="btn" id="speed-fix-path-btn">Go to Configuration</button>
    </div>

    <div id="speed-active">
      <div class="readouts">
        <div class="cell bw" data-tip="How fast data can be pulled from the internet">
          <div class="lab">Download<button class="info-btn" data-metric="download" aria-label="About Download">i</button></div>
          <div class="val" id="v-dl">–<span class="unit">Mbps</span></div>
        </div>
        <div class="cell bw" data-tip="How fast data can be sent to the internet">
          <div class="lab">Upload<button class="info-btn" data-metric="upload" aria-label="About Upload">i</button></div>
          <div class="val" id="v-ul">–<span class="unit">Mbps</span></div>
        </div>
        <div class="cell lat" data-tip="Round-trip time to the test server">
          <div class="lab">Ping<button class="info-btn" data-metric="ping" aria-label="About Ping">i</button></div>
          <div class="val" id="v-ping">–<span class="unit">ms</span></div>
        </div>
        <div class="cell lat" data-tip="How much the ping time varies between samples">
          <div class="lab">Jitter<button class="info-btn" data-metric="jitter" aria-label="About Jitter">i</button></div>
          <div class="val" id="v-jitter">–<span class="unit">ms</span></div>
        </div>
      </div>
      <div class="server-line" id="speed-server">no speed test data yet</div>

      <div class="chartbox">
        <div class="chart-tools">
          <span><span class="swatch" style="background:#a78bfa"></span>bandwidth axis (Mbps) — Download, Upload</span>
          <span><span class="swatch" style="background:#f472b6"></span>latency axis (ms) — Ping, Jitter</span>
          <button class="btn" id="speed-run-btn" style="margin-left:auto">Run test now</button>
          <span class="meta" id="speed-run-status"></span>
        </div>
        <div class="chart-wrap"><canvas id="chart-speed"></canvas></div>
      </div>
    </div>
  </section>

  <section class="tab-panel hidden" id="panel-config">
    <div class="chartbox" style="max-width:520px">
      <div id="config-form"></div>
      <div style="margin-top:4px;display:flex;align-items:center;gap:12px">
        <button class="btn" id="config-save-btn">Save changes</button>
        <span class="meta" id="config-status"></span>
      </div>
    </div>
    <div class="server-line" id="config-dbpath" style="margin-top:14px"></div>
  </section>

  <div class="detail-panel hidden" id="detail-panel">
    <div class="detail-head">
      <span id="detail-title">—</span>
      <button id="detail-close" aria-label="Close details">×</button>
    </div>
    <div class="detail-body" id="detail-body"></div>
  </div>
</div>

<footer>signal every POLL_SECONDS_PLACEHOLDER s · speed test every SPEEDTEST_INTERVAL_PLACEHOLDER s · history in tmhi_signal.db · this page only talks to localhost</footer>

<script>
const POLL_MS = POLL_MS_PLACEHOLDER;
let lastTs = 0;
let lastSpeedTs = 0;
let pendingSpeedTest = false;

document.querySelectorAll('.tab-btn').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active', b===btn));
    document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('hidden', p.id !== 'panel-'+btn.dataset.tab));
  });
});
document.querySelector('.tab-btn[data-tab="speed"]').addEventListener('click', checkSpeedStatus);

function sinrColor(v){ if(v==null) return "var(--dim)"; if(v>=13) return "var(--good)"; if(v>=5) return "var(--warn)"; return "var(--bad)"; }
function rsrpColor(v){ if(v==null) return "var(--dim)"; if(v>=-90) return "var(--good)"; if(v>=-105) return "var(--warn)"; return "var(--bad)"; }

const ctx = document.getElementById('chart').getContext('2d');
const mk = (label,color,axis)=>({label,borderColor:color,backgroundColor:color,
  data:[],yAxisID:axis,borderWidth:2,pointRadius:0,tension:.25,spanGaps:false});
const chart = new Chart(ctx,{
  type:'line',
  data:{datasets:[
    mk('SINR','#38bdf8','yQ'),
    mk('RSRQ','#7dd3fc','yQ'),
    mk('RSRP','#f5a524','yP'),
    mk('RSSI','#fbbf24','yP'),
  ]},
  options:{
    animation:false, parsing:false, maintainAspectRatio:false,
    interaction:{mode:'index',intersect:false},
    scales:{
      x:{type:'time'==='time'?'linear':'linear',
         ticks:{color:'#6b7c8f',maxTicksLimit:8,
                callback:v=>new Date(v*1000).toLocaleTimeString()},
         grid:{color:'#233040'}},
      yQ:{position:'left',title:{display:true,text:'dB (quality)',color:'#38bdf8'},
          ticks:{color:'#6b7c8f'},grid:{color:'#233040'}},
      yP:{position:'right',title:{display:true,text:'dBm (power)',color:'#f5a524'},
          ticks:{color:'#6b7c8f'},grid:{drawOnChartArea:false}},
    },
    plugins:{legend:{labels:{color:'#c9d4df',boxWidth:12}}}
  }
});

document.getElementById('tgl-power').addEventListener('change',e=>{
  const on=e.target.checked;
  chart.data.datasets[2].hidden=!on;
  chart.data.datasets[3].hidden=!on;
  chart.options.scales.yP.display=on;
  chart.update();
});

const ctxSpeed = document.getElementById('chart-speed').getContext('2d');
const chartSpeed = new Chart(ctxSpeed,{
  type:'line',
  data:{datasets:[
    mk('Download','#a78bfa','yBw'),
    mk('Upload','#c4b5fd','yBw'),
    mk('Ping','#f472b6','yLat'),
    mk('Jitter','#fbcfe8','yLat'),
  ]},
  options:{
    animation:false, parsing:false, maintainAspectRatio:false,
    interaction:{mode:'index',intersect:false},
    scales:{
      x:{type:'linear',
         ticks:{color:'#6b7c8f',maxTicksLimit:8,
                callback:v=>new Date(v*1000).toLocaleTimeString()},
         grid:{color:'#233040'}},
      yBw:{position:'left',beginAtZero:true,title:{display:true,text:'Mbps',color:'#a78bfa'},
          ticks:{color:'#6b7c8f'},grid:{color:'#233040'}},
      yLat:{position:'right',beginAtZero:true,title:{display:true,text:'ms',color:'#f472b6'},
          ticks:{color:'#6b7c8f'},grid:{drawOnChartArea:false}},
    },
    plugins:{legend:{labels:{color:'#c9d4df',boxWidth:12}}}
  }
});

document.getElementById('speed-run-btn').addEventListener('click', async ()=>{
  const btn = document.getElementById('speed-run-btn');
  const status = document.getElementById('speed-run-status');
  btn.disabled = true;
  try{
    const res = await fetch('/speed/run');
    const data = await res.json();
    if(data.started){
      pendingSpeedTest = true;
      status.textContent = 'running… (~15–30s)';
    }else{
      status.textContent = data.reason || 'already running';
      btn.disabled = false;
    }
  }catch(e){
    status.textContent = 'failed to start';
    btn.disabled = false;
  }
});

function goToConfigTab(){
  document.querySelector('.tab-btn[data-tab="config"]').click();
}
document.getElementById('speed-enable-btn').addEventListener('click', goToConfigTab);
document.getElementById('speed-fix-path-btn').addEventListener('click', goToConfigTab);

async function checkSpeedStatus(){
  const disabledNotice = document.getElementById('speed-disabled-notice');
  const missingNotice = document.getElementById('speed-binary-missing-notice');
  const active = document.getElementById('speed-active');
  try{
    const res = await fetch('/speed/status');
    const s = await res.json();
    disabledNotice.classList.toggle('hidden', s.enabled);
    missingNotice.classList.toggle('hidden', !s.enabled || s.binary_found);
    active.classList.toggle('hidden', !s.enabled || !s.binary_found);
    if(!s.binary_found){
      document.getElementById('speed-binary-path').textContent = s.binary_path;
    }
  }catch(e){ /* leave last-known state on screen */ }
}

function setCell(id,val,unit){
  const el=document.getElementById(id);
  if(val==null||val===''){ el.innerHTML='–'+(unit?'<span class="unit">'+unit+'</span>':''); }
  else{ el.innerHTML=val+(unit?'<span class="unit">'+unit+'</span>':''); }
}

function renderBars(value){
  const n = value==null ? 0 : Math.round(value);
  document.querySelectorAll('#v-bars-icon .bar').forEach((bar,i)=>{
    bar.classList.toggle('on', (i+1)<=n);
  });
  document.getElementById('v-bars-num').textContent = value==null ? '–' : (Math.round(value)+'/5');
}

function rangeTable(rows){
  return '<table class="range-table">'+rows.map(r=>
    '<tr><td class="tag '+r[2]+'">'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('')+'</table>';
}

const METRIC_INFO = {
  sinr: {
    title: 'SINR — Signal-to-Interference-plus-Noise Ratio',
    body: '<p>Measures how strong the usable signal is relative to interference and background '
        + 'noise, in dB. This is the single best predictor of real-world throughput and stability '
        + '— more telling than bars, and more telling than RSRP alone.</p>'
        + rangeTable([['Good','≥ 13 dB','good'],['Fair','5 – 13 dB','warn'],['Poor','&lt; 5 dB','bad']])
  },
  rsrp: {
    title: 'RSRP — Reference Signal Received Power',
    body: '<p>The power level of the reference signal, in dBm. Reflects raw coverage — how strong '
        + 'the signal is at your location — independent of interference.</p>'
        + rangeTable([['Good','≥ -90 dBm','good'],['Fair','-90 to -105 dBm','warn'],['Poor','≤ -105 dBm','bad']])
  },
  rsrq: {
    title: 'RSRQ — Reference Signal Received Quality',
    body: '<p>Signal quality in dB, factoring in interference from other cells sharing the same '
        + 'channel resources. Complements RSRP by capturing congestion/interference RSRP alone misses.</p>'
        + rangeTable([['Good','≥ -12 dB','good'],['Fair','-12 to -16 dB','warn'],['Poor','≤ -16 dB','bad']])
  },
  rssi: {
    title: 'RSSI — Received Signal Strength Indicator',
    body: '<p>Total received power in dBm across the whole channel — signal plus interference plus '
        + "noise combined. Because it isn't isolated to just the reference signal, it's the least "
        + 'specific of the four metrics; read it alongside RSRP/SINR rather than on its own.</p>'
  },
  band: {
    title: 'Band',
    body: '<p>The active 5G NR frequency band, e.g. <code>n71</code>. Lower-numbered "low bands" '
        + '(like n71, ~600 MHz) travel farther and penetrate walls well, but carry less capacity. '
        + '"Mid bands" (like n41, ~2.5 GHz) trade some range and penetration for significantly '
        + 'higher throughput. If a location shows only a low band, an external directional antenna '
        + 'often helps most by improving RSRP/SINR on that band, or by unlocking a mid-band signal '
        + "that's otherwise too weak to hold.</p>"
  },
  bars: {
    title: 'Bars',
    body: '<p>A coarse 0–5 indicator derived mainly from RSRP. It <strong>ignores SINR</strong>, so '
        + "it's possible to see full bars while quality is actually poor due to interference or "
        + 'noise. Treat bars as a rough at-a-glance signal only — SINR and RSRP are the metrics '
        + 'that matter.</p>'
  },
  antenna: {
    title: 'Antenna',
    body: "<p><strong>Internal</strong> — using the gateway's built-in antenna. "
        + '<strong>External</strong> — using an attached directional/panel antenna, typically aimed '
        + 'at a specific tower to improve signal.</p>'
  },
  conn: {
    title: 'Connection State',
    body: "<p>The gateway's registration status on the T-Mobile network — normally "
        + '<code>registered</code>. Anything else, or the status dot going red, indicates the '
        + 'gateway has lost its connection.</p>'
  },
  download: {
    title: 'Download Speed',
    body: '<p>How fast data can be pulled from the internet to this connection, in Mbps '
        + '(megabits per second), measured with a real transfer to a nearby test server. This is '
        + 'the number that drives streaming quality, download times, and general browsing feel.</p>'
  },
  upload: {
    title: 'Upload Speed',
    body: '<p>How fast data can be sent from this connection to the internet, in Mbps. Matters for '
        + 'video calls, cloud backups, and uploading files — fixed wireless connections like this '
        + 'one are often asymmetric, with upload well below download.</p>'
  },
  ping: {
    title: 'Ping (Latency)',
    body: '<p>Round-trip time to the test server, in milliseconds — how long a request takes to '
        + 'get a response. Lower is better; it matters most for anything real-time (calls, gaming, '
        + 'remote desktops) rather than for raw throughput.</p>'
  },
  jitter: {
    title: 'Jitter',
    body: '<p>How much the ping time varies between samples, in milliseconds. High jitter means '
        + "the connection is inconsistent even when average latency looks fine — it's often what "
        + 'makes a call choppy even when the ping number itself looks okay.</p>'
  }
};

document.querySelectorAll('.info-btn').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    const info = METRIC_INFO[btn.dataset.metric];
    if(!info) return;
    document.getElementById('detail-title').textContent = info.title;
    document.getElementById('detail-body').innerHTML = info.body;
    document.getElementById('detail-panel').classList.remove('hidden');
    document.querySelectorAll('.info-btn').forEach(b=>b.classList.toggle('active', b===btn));
    document.getElementById('detail-panel').scrollIntoView({behavior:'smooth', block:'nearest'});
  });
});
document.getElementById('detail-close').addEventListener('click', ()=>{
  document.getElementById('detail-panel').classList.add('hidden');
  document.querySelectorAll('.info-btn').forEach(b=>b.classList.remove('active'));
});

async function tick(){
  try{
    const res = await fetch('/data?since='+lastTs);
    const rows = await res.json();
    const dot=document.getElementById('status-dot');
    const stxt=document.getElementById('status-text');

    if(rows.length){
      for(const r of rows){
        chart.data.datasets[0].data.push({x:r.ts,y:r.sinr});
        chart.data.datasets[1].data.push({x:r.ts,y:r.rsrq});
        chart.data.datasets[2].data.push({x:r.ts,y:r.rsrp});
        chart.data.datasets[3].data.push({x:r.ts,y:r.rssi});
        lastTs = r.ts;
      }
      // keep the window manageable (last ~720 points ≈ 1h at 5s)
      for(const ds of chart.data.datasets){ if(ds.data.length>720) ds.data.splice(0,ds.data.length-720); }
      chart.update();

      const last = rows[rows.length-1];
      const live = last.ok===1;
      dot.classList.toggle('live',live);
      stxt.textContent = live ? 'live · '+last.iso : 'gateway not answering · '+last.iso;

      setCell('v-sinr', last.sinr, 'dB');
      setCell('v-rsrp', last.rsrp, 'dBm');
      setCell('v-rsrq', last.rsrq, 'dB');
      setCell('v-rssi', last.rssi, 'dBm');
      setCell('v-band', last.band);
      renderBars(last.bars);
      setCell('v-ant',  last.antenna);
      setCell('v-conn', last.connection);
      document.getElementById('v-sinr').style.color=sinrColor(last.sinr);
      document.getElementById('v-rsrp').style.color=rsrpColor(last.rsrp);

      const id=[];
      if(last.gnbid!=null) id.push('gNB '+last.gnbid);
      if(last.cid!=null) id.push('CID '+last.cid);
      document.getElementById('ident').textContent=id.join(' · ');
    }
  }catch(e){
    document.getElementById('status-dot').classList.remove('live');
    document.getElementById('status-text').textContent='dashboard can’t reach monitor';
  }
}

async function tickSpeed(){
  try{
    const res = await fetch('/speed?since='+lastSpeedTs);
    const rows = await res.json();
    if(rows.length){
      for(const r of rows){
        chartSpeed.data.datasets[0].data.push({x:r.ts,y:r.download_mbps});
        chartSpeed.data.datasets[1].data.push({x:r.ts,y:r.upload_mbps});
        chartSpeed.data.datasets[2].data.push({x:r.ts,y:r.ping_ms});
        chartSpeed.data.datasets[3].data.push({x:r.ts,y:r.jitter_ms});
        lastSpeedTs = r.ts;
      }
      // keep the window manageable (last 1000 tests)
      for(const ds of chartSpeed.data.datasets){ if(ds.data.length>1000) ds.data.splice(0,ds.data.length-1000); }
      chartSpeed.update();

      const last = rows[rows.length-1];
      setCell('v-dl', last.download_mbps, 'Mbps');
      setCell('v-ul', last.upload_mbps, 'Mbps');
      setCell('v-ping', last.ping_ms, 'ms');
      setCell('v-jitter', last.jitter_ms, 'ms');

      const parts=[];
      if(last.server_name) parts.push(last.server_name);
      if(last.server_location) parts.push(last.server_location);
      if(last.isp) parts.push(last.isp);
      document.getElementById('speed-server').textContent =
        (last.ok===1 ? parts.join(' · ') : 'last test failed') + ' · ' + last.iso;

      if(pendingSpeedTest){
        pendingSpeedTest = false;
        document.getElementById('speed-run-btn').disabled = false;
        document.getElementById('speed-run-status').textContent = 'done · '+last.iso;
      }
    }
  }catch(e){ /* signal tick() already surfaces a dashboard-offline state */ }
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadConfig(){
  try{
    const res = await fetch('/config');
    const data = await res.json();
    const form = document.getElementById('config-form');
    form.innerHTML = '';
    for(const [key, schema] of Object.entries(data.schema)){
      const val = data.values[key];
      const restartTag = schema.restart_required ? ' <span class="restart-tag">(restart required)</span>' : '';
      const unit = schema.unit ? ' ('+escapeHtml(schema.unit)+')' : '';
      const row = document.createElement('div');
      row.className = 'config-row';
      let input;
      if(schema.type === 'bool'){
        input = '<label class="config-label" style="display:flex;align-items:center;gap:8px;cursor:pointer">'
          + '<input class="config-input" type="checkbox" id="cfg-'+key+'" data-key="'+key+'" '+(val?'checked':'')+'> '
          + escapeHtml(schema.label)+unit+restartTag+'</label>';
        row.innerHTML = input + '<div class="config-hint">'+escapeHtml(schema.help)+'</div>';
      }else{
        input = '<input class="config-input" id="cfg-'+key+'" type="'+(schema.type==='int'?'number':'text')+'" '
          + 'value="'+escapeHtml(val)+'" data-key="'+key+'">';
        row.innerHTML =
          '<label class="config-label" for="cfg-'+key+'">'+escapeHtml(schema.label)+unit+restartTag+'</label>'
          + '<div class="config-hint">'+escapeHtml(schema.help)+'</div>' + input;
      }
      form.appendChild(row);
    }
    document.getElementById('config-dbpath').textContent =
      'Database file: '+data.db_path+' (set with --db-path at startup, not editable here)';
  }catch(e){
    document.getElementById('config-status').textContent = 'failed to load configuration';
  }
}

document.getElementById('config-save-btn').addEventListener('click', async ()=>{
  const status = document.getElementById('config-status');
  const payload = {};
  document.querySelectorAll('.config-input').forEach(input=>{
    if(input.type === 'checkbox') payload[input.dataset.key] = input.checked;
    else if(input.type === 'number') payload[input.dataset.key] = Number(input.value);
    else payload[input.dataset.key] = input.value;
  });
  status.textContent = 'saving…';
  try{
    const res = await fetch('/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    const errKeys = Object.keys(data.errors || {});
    if(errKeys.length){
      status.textContent = 'errors — ' + errKeys.map(k=>k+': '+data.errors[k]).join('; ');
    }else if(data.restart_required && data.restart_required.length){
      status.textContent = 'saved — restart the script for: ' + data.restart_required.join(', ');
    }else{
      status.textContent = 'saved';
    }
    loadConfig();
  }catch(e){
    status.textContent = 'save failed';
  }
});

document.querySelector('.tab-btn[data-tab="config"]').addEventListener('click', loadConfig);

// On first load, pull recent history so the chart isn't empty.
(async function bootstrap(){
  try{
    const res=await fetch('/data?since=0');
    const rows=await res.json();
    // keep only the last 720 for the initial view
    const recent = rows.slice(-720);
    for(const r of recent){
      chart.data.datasets[0].data.push({x:r.ts,y:r.sinr});
      chart.data.datasets[1].data.push({x:r.ts,y:r.rsrq});
      chart.data.datasets[2].data.push({x:r.ts,y:r.rsrp});
      chart.data.datasets[3].data.push({x:r.ts,y:r.rssi});
      lastTs=r.ts;
    }
    chart.update();
  }catch(e){}
  tick();
  setInterval(tick, POLL_MS);
})();

(async function bootstrapSpeed(){
  try{
    const res=await fetch('/speed?since=0');
    const rows=await res.json();
    const recent = rows.slice(-1000);
    for(const r of recent){
      chartSpeed.data.datasets[0].data.push({x:r.ts,y:r.download_mbps});
      chartSpeed.data.datasets[1].data.push({x:r.ts,y:r.upload_mbps});
      chartSpeed.data.datasets[2].data.push({x:r.ts,y:r.ping_ms});
      chartSpeed.data.datasets[3].data.push({x:r.ts,y:r.jitter_ms});
      lastSpeedTs=r.ts;
    }
    chartSpeed.update();
  }catch(e){}
  tickSpeed();
  setInterval(tickSpeed, POLL_MS);
})();

loadConfig();
checkSpeedStatus();
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    conn = None  # set on the server instance

    def log_message(self, *args):
        pass  # silence default per-request logging

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = (DASHBOARD_HTML
                    .replace("POLL_MS_PLACEHOLDER", str(CFG.get("poll_seconds") * 1000))
                    .replace("POLL_SECONDS_PLACEHOLDER", str(CFG.get("poll_seconds")))
                    .replace("SPEEDTEST_INTERVAL_PLACEHOLDER", str(CFG.get("speedtest_interval"))))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/data"):
            since = 0.0
            if "since=" in self.path:
                try:
                    since = float(self.path.split("since=", 1)[1].split("&", 1)[0])
                except ValueError:
                    since = 0.0
            rows = db_rows_since(self.server.conn, since)
            self._send(200, json.dumps(rows).encode("utf-8"), "application/json")
        elif self.path.startswith("/speed/run"):
            if not CFG.get("speedtest_enabled"):
                body = {"started": False, "reason": "speed testing is disabled in Configuration"}
            elif _speedtest_lock.locked():
                body = {"started": False, "reason": "a speed test is already running"}
            else:
                threading.Thread(target=run_one_speedtest, args=(self.server.conn,), daemon=True).start()
                body = {"started": True}
            self._send(200, json.dumps(body).encode("utf-8"), "application/json")
        elif self.path.startswith("/speed/status"):
            body = {
                "enabled": CFG.get("speedtest_enabled"),
                "binary_path": CFG.get("speedtest_bin"),
                "binary_found": speedtest_binary_status(),
            }
            self._send(200, json.dumps(body).encode("utf-8"), "application/json")
        elif self.path.startswith("/speed"):
            since = 0.0
            if "since=" in self.path:
                try:
                    since = float(self.path.split("since=", 1)[1].split("&", 1)[0])
                except ValueError:
                    since = 0.0
            rows = db_speed_rows_since(self.server.conn, since)
            self._send(200, json.dumps(rows).encode("utf-8"), "application/json")
        elif self.path.startswith("/config"):
            body = {
                "values": CFG.as_dict(),
                "schema": config_schema_public(),
                "db_path": RESOLVED_DB_PATH,
            }
            self._send(200, json.dumps(body).encode("utf-8"), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/config"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                incoming = json.loads(raw or b"{}")
            except Exception:
                self._send(400, json.dumps({"error": "invalid JSON body"}).encode("utf-8"), "application/json")
                return
            if not isinstance(incoming, dict):
                self._send(400, json.dumps({"error": "expected a JSON object"}).encode("utf-8"), "application/json")
                return

            updated, errors = {}, {}
            for key, value in incoming.items():
                try:
                    updated[key] = CFG.set(key, value)
                except Exception as e:
                    errors[key] = str(e)

            restart_required = sorted(k for k in updated if CONFIG_SCHEMA[k]["restart_required"])
            body = {"updated": updated, "errors": errors, "restart_required": restart_required}
            self._send(200, json.dumps(body).encode("utf-8"), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

def parse_args():
    p = argparse.ArgumentParser(description="TMHI Signal Monitor")
    p.add_argument(
        "--db-path", default="tmhi_signal.db",
        help="SQLite file path (default: tmhi_signal.db). Has to be known before persisted config "
             "can be loaded, so unlike everything else here, it's CLI-only -- not stored in the "
             "config table, not editable from the web UI.",
    )
    for key, schema in CONFIG_SCHEMA.items():
        flag = "--" + key.replace("_", "-")
        caster = _TYPE_CASTERS[schema["type"]]
        p.add_argument(
            flag, type=caster, default=None,
            help=f"Override '{schema['label']}' for this run only (not persisted; "
                 f"current default: {schema['default']!r}). {schema['help']}",
        )
    return p.parse_args()

def main():
    args = parse_args()

    conn = db_connect(args.db_path)
    db_init(conn)

    global CFG, RESOLVED_DB_PATH
    RESOLVED_DB_PATH = args.db_path
    CFG = Config(conn)
    CFG.apply_cli_overrides({key: getattr(args, key) for key in CONFIG_SCHEMA})

    stop_event = threading.Event()
    poller = threading.Thread(target=poller_loop, args=(conn, stop_event), daemon=True)
    poller.start()
    speedtester = threading.Thread(target=speedtest_loop, args=(conn, stop_event), daemon=True)
    speedtester.start()

    server = ThreadingHTTPServer((CFG.get("http_host"), CFG.get("http_port")), Handler)
    server.conn = conn

    print(f"TMHI Signal Monitor running.")
    print(f"  Gateway   : {CFG.get('gateway_url')}")
    print(f"  Poll      : every {CFG.get('poll_seconds')}s")
    print(f"  Speedtest : every {CFG.get('speedtest_interval')}s via {CFG.get('speedtest_bin')}")
    print(f"  Storage   : {RESOLVED_DB_PATH}")
    print(f"  Listening : {CFG.get('http_host')}:{CFG.get('http_port')}")
    print(f"  Dashboard : http://localhost:{CFG.get('http_port')}   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        stop_event.set()
        server.shutdown()
        conn.close()

if __name__ == "__main__":
    main()
