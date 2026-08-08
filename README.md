# TMHI Signal Monitor

A self-hosted dashboard for a **T-Mobile Home Internet** gateway. It polls the
gateway's local signal API every few seconds, optionally runs periodic
download/upload/ping/jitter speed tests, stores everything in SQLite, and
serves a live-updating web dashboard — all from a single Python file.

It exists because T-Mobile's current **T-Life** app doesn't expose the raw
radio metrics (RSRP, RSRQ, SINR, RSSI, band) that used to be visible in the
retired T-Mobile Internet app. Those numbers are still available from the
gateway's local HTTP API — this tool reads them, keeps history, and graphs
them over time.

## Requirements

- Python 3.8+ (standard library only — nothing to `pip install`)
- A device on the **same local network as the gateway** (default gateway IP:
  `192.168.12.1`)
- A browser, for the dashboard
- *Optional*: the [Ookla Speedtest CLI](https://www.speedtest.net/apps/cli),
  if you want the speed test features — see below

## Quick start

```bash
python3 tmhi_monitor.py
```

Then open **http://localhost:8073**. Ctrl-C to stop.

To see every startup flag:

```bash
python3 tmhi_monitor.py --help
```

## Running as a service (auto-restart, starts on boot)

`tmhi-monitor.service` in this repo is a systemd **user** unit — no root
required. Edit its `WorkingDirectory`/`ExecStart` paths if your checkout
lives somewhere other than this directory, then:

```bash
mkdir -p ~/.config/systemd/user
cp tmhi-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tmhi-monitor.service

# Optional but recommended: start at boot even without an active login
# session (otherwise the service only starts once you log in).
loginctl enable-linger "$USER"
```

Useful commands:

```bash
systemctl --user status tmhi-monitor.service    # is it running?
journalctl --user -u tmhi-monitor.service -f    # follow its logs
systemctl --user restart tmhi-monitor.service   # e.g. after an http_port/http_host config change
systemctl --user disable --now tmhi-monitor.service   # stop managing it
```

`Restart=on-failure` in the unit means systemd brings it back if the process
crashes; it does *not* restart it just because you edited a setting in the
Configuration tab — most settings already apply live, and the couple that
don't (listening host/port) tell you a restart is needed when you save them.

If you'd rather run it as a proper system-wide service (starts independent
of any user account, the traditional choice for a server) instead of a user
service, that needs a unit under `/etc/systemd/system/` and `sudo`, which
this README won't walk through since it depends on your system's sudo setup.

## Speed testing (optional)

Signal monitoring works out of the box with no extra installs. The
download/upload/ping/jitter speed test is a separate feature that shells out
to Ookla's official CLI, since accurately measuring real bandwidth isn't
something Python's standard library can do on its own.

If you don't install it, the dashboard's **Connection** tab will tell you so
and show you the install command — it never crashes the rest of the app. To
install it yourself ahead of time (no root required):

```bash
curl -sL -o /tmp/speedtest.tgz https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz
tar -xzf /tmp/speedtest.tgz -C ~/.local/bin speedtest
chmod +x ~/.local/bin/speedtest
```

(Grab the right archive for your OS/arch from the
[apps/cli page](https://www.speedtest.net/apps/cli) if you're not on Linux
x86_64.) If you'd rather not run speed tests at all, turn them off from the
**Configuration** tab, or start with `--speedtest-enabled false`.

## The dashboard

Four tabs, no page reloads:

- **Signal** — live SINR / RSRP / RSRQ / RSSI / band / bars / antenna /
  connection state, plus a scrolling history chart. Every readout has a
  hover tooltip and an "i" icon that opens a plain-language explanation of
  what the metric means and what a healthy value looks like.
- **Connection** — download/upload/ping/jitter, its own history chart, and a
  "Run test now" button for an on-demand test outside the schedule. While a
  test is running (scheduled or manual), a live progress bar tracks its
  current phase (ping → download → upload) with the in-progress number
  updating in real time, not just the final result. If speed testing is
  turned off, or the CLI binary isn't installed, this tab explains why and
  how to fix it instead of showing empty charts.
- **Details** — gateway model, manufacturer, hardware/firmware version,
  update status, uptime, serial, and MAC address; plus cellular network
  details that aren't signal-strength metrics (APN, roaming, IPv6 support,
  band, registration state, cell ID, gNodeB ID). Uptime resetting to a small
  number is a good sign the gateway just rebooted.
- **Configuration** — every setting below, editable from the browser. Most
  changes apply on the next poll/test cycle with no restart; a couple
  (listening address/port) are flagged as needing one.

## Configuration

Every setting is available three ways, and they compose in one order (lowest
to highest priority): a **built-in default** → whatever's **saved in the
Configuration tab** (persisted in the SQLite file, survives restarts) → a
**CLI flag**, which overrides everything for that run only and is *not*
saved — so a one-off `--poll-seconds 2` for testing doesn't quietly become
permanent.

| Setting | Flag | Default |
|---|---|---|
| Gateway URL | `--gateway-url` | `http://192.168.12.1/TMI/v1/gateway?get=signal` |
| Signal poll interval | `--poll-seconds` | `5` |
| Gateway fetch timeout | `--fetch-timeout` | `8` |
| Device info poll interval | `--device-poll-interval` | `300` (5 min) |
| Dashboard listen host | `--http-host` | `0.0.0.0` (all interfaces) |
| Dashboard listen port | `--http-port` | `8073` |
| Enable speed testing | `--speedtest-enabled` | `true` |
| Speedtest binary path | `--speedtest-bin` | `~/.local/bin/speedtest` |
| Speed test interval | `--speedtest-interval` | `900` (15 min) |
| Speed test timeout | `--speedtest-timeout` | `90` |
| SQLite file path | `--db-path` | `tmhi_signal.db` |

`--db-path` is the one CLI-only setting — it has to be known before the
database can be opened to read everything else, so it's not in the
Configuration tab.

## Data

Everything is stored in a single SQLite file (`tmhi_signal.db` by default) —
every signal reading and every speed test result, including the full raw
response from the gateway/CLI, so nothing is lost even if a future firmware
update changes the data shape underneath this tool.

## Notes

- This machine needs a network path to the gateway. If you're not directly on
  its subnet, check whether your router routes to it — see `CLAUDE.md` for
  the reasoning behind that if you're troubleshooting.
- Speed tests briefly saturate your connection (~15-30s per run). The default
  15-minute interval is deliberate; don't set it too aggressively.
