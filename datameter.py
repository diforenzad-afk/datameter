#!/usr/bin/env python3
"""
datameter — a friendly vnstat TUI dashboard.

Zero third-party dependencies (stdlib curses only).

  * plan gauge for the billing cycle (configurable start day, e.g. the 18th)
  * today's usage gauge vs a daily limit
  * averages: per "on" hour, per day (7 d), per week, projected cycle
  * vertical bar charts for the last 24 hours and the last 7 days
  * user-settable limits and alert thresholds (settings screen)
  * non-obnoxious "tock" sound (car turn-signal style) + desktop notification
  * start/stop the vnstat daemon from inside the UI

Usage
  datameter              # launch the TUI   (wrapper in ~/.local/bin)
  datameter --once       # print a text summary and exit (cron/waybar friendly)
  datameter --help
"""

from __future__ import annotations

import argparse
import curses
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from datetime import date, datetime, timedelta
from pathlib import Path

APP = "datameter"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("DATAMETER_CONFIG") or (HERE / "config.json"))
STATE_PATH = Path(os.environ.get("DATAMETER_STATE") or (HERE / ".alert-state.json"))
SOUND_PATH = HERE / "tock.wav"

DEFAULT_CONFIG = {
    "interface": "wlp4s0",
    "cycle_start_day": 18,        # Iliad 300 GB plan resets on the 18th
    "cycle_limit_gb": 300.0,
    "daily_limit_gb": 10.0,
    "alert_thresholds_pct": [80, 100],
    "sound_enabled": True,
    "sound_volume": 0.35,
    "notifications": True,
    "refresh_seconds": 10,
}

# --------------------------------------------------------------------------- #
# config / state
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def fmt_bytes(n: float, decimals: int = 1) -> str:
    n = float(n)
    for u in UNITS:
        if abs(n) < 1024 or u == UNITS[-1]:
            return f"{n:.{decimals}f} {u}"
        n /= 1024
    return f"{n:.{decimals}f} PiB"


def gb(n: float) -> float:
    return n / (1024 ** 3)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --- segmented bar (chunky blocks, clearly separated) ---------------------- #

EIGHTHS = "░▏▎▍▌▋▊▉█"          # index 0..8, partial blocks for smooth tips
BAR_CELL_W = 2                   # each segment is 2 chars wide


def _seg_cell(f: float, width: int = BAR_CELL_W) -> str:
    """One 2-char segment, partially filled at the tip."""
    f = clamp(f, 0.0, 1.0)
    total = f * width
    full = int(total)
    rem = total - full
    out = "█" * full
    if full < width:
        out += EIGHTHS[int(round(rem * 8))]
    return out.ljust(width, "░")[:width]


def seg_bar(pct: float, cells: int) -> str:
    """A bar made of `cells` separated segments, e.g.  ██ ██ ░░ ░░"""
    if cells <= 0:
        return ""
    parts = []
    for i in range(cells):
        lo, hi = 100 * i / cells, 100 * (i + 1) / cells
        parts.append(_seg_cell((pct - lo) / (hi - lo)))
    return " ".join(parts)


def bar_cells_for_width(width: int) -> int:
    # each cell occupies BAR_CELL_W chars + 1 space
    return max(6, (width - 4) // (BAR_CELL_W + 1))


# --------------------------------------------------------------------------- #
# vnstat access
# --------------------------------------------------------------------------- #


def list_interfaces() -> list[str]:
    try:
        j = json.loads(run(["vnstat", "--json"]).stdout)
        return [i["name"] for i in j.get("interfaces", [])]
    except Exception:
        return []


def vnstat_snapshot(iface: str) -> dict:
    cp = run(["vnstat", "--json", "-i", iface])
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "vnstat failed")
    j = json.loads(cp.stdout)
    for i in j.get("interfaces", []):
        if i["name"] == iface:
            return i["traffic"]
    raise RuntimeError(f"interface {iface!r} not present in vnstat database")


def _d(entry: dict) -> date:
    dd = entry["date"]
    return date(dd["year"], dd["month"], dd["day"])


def _bytes(entry: dict) -> int:
    return int(entry.get("rx", 0)) + int(entry.get("tx", 0))


def cycle_window(cfg: dict, today: date | None = None) -> tuple[date, date, int, int]:
    """Return (start, end, total_days, elapsed_days) for the billing cycle."""
    today = today or date.today()
    day = clamp(int(cfg["cycle_start_day"]), 1, 28)

    def make(y, m, d):
        return date(y, m, min(d, 28))

    start = make(today.year, today.month, day)
    if start > today:
        py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = make(py, pm, day)
    ey, em = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = make(ey, em, day)
    total_days = (end - start).days
    elapsed = (today - start).days + 1
    return start, end, total_days, elapsed


def compute_stats(iface: str, cfg: dict) -> dict:
    t = vnstat_snapshot(iface)
    today = date.today()
    now = time.time()

    hours = t.get("hour", [])
    hour_vals = [_bytes(e) for e in hours]
    on_hours = len(hours)
    avg_hour = (sum(hour_vals) / on_hours) if on_hours else 0.0

    days = t.get("day", [])
    day_vals = [_bytes(e) for e in days]
    avg_day = (sum(day_vals) / len(day_vals)) if day_vals else 0.0
    avg_week = avg_day * 7

    today_total = 0
    for e in days:
        if _d(e) == today:
            today_total = _bytes(e)
            break

    five = t.get("fiveminute", [])
    last24 = sum(_bytes(e) for e in five if now - int(e.get("timestamp", 0)) <= 86400)

    start, end, total_days, elapsed = cycle_window(cfg)
    cycle_used = sum(_bytes(e) for e in days if _d(e) >= start)
    projected_cycle = avg_day * total_days

    # series for the charts
    # hourly: last 24 wall-clock hours (0 for hours the PC was off)
    hours_map = {(_d(e), int(e["time"]["hour"])): _bytes(e) for e in hours}
    now_dt = datetime.now().replace(minute=0, second=0, microsecond=0)
    hour_values, hour_labels = [], []
    for i in range(23, -1, -1):
        hh = now_dt - timedelta(hours=i)
        hour_values.append(hours_map.get((hh.date(), hh.hour), 0))
        hour_labels.append(f"{hh.hour:02d}")

    dseries = days[-7:]
    day_values = [_bytes(e) for e in dseries]
    day_labels = [_d(e).strftime("%a")[:2] for e in dseries]

    return {
        "today_total": today_total,
        "last24": last24,
        "avg_hour": avg_hour,
        "avg_day": avg_day,
        "avg_week": avg_week,
        "on_hours": on_hours,
        "projected_cycle": projected_cycle,
        "cycle_used": cycle_used,
        "cycle_start": start,
        "cycle_end": end,
        "cycle_days": total_days,
        "cycle_elapsed": elapsed,
        "hour_values": hour_values,
        "hour_labels": hour_labels,
        "day_values": day_values,
        "day_labels": day_labels,
        "total": int(t.get("total", {}).get("rx", 0)) + int(t.get("total", {}).get("tx", 0)),
    }


# --------------------------------------------------------------------------- #
# sound + notifications
# --------------------------------------------------------------------------- #


def generate_tock(path: Path = SOUND_PATH) -> None:
    """Synthesise a short, quiet 'tock' (modern car turn-signal style)."""
    sr = 44100
    n = int(sr * 0.055)
    frames = bytearray()
    for k in range(n):
        ts = k / sr
        tone = (math.sin(2 * math.pi * 1050 * ts) * 0.65
                + math.sin(2 * math.pi * 520 * ts) * 0.35)
        env = math.exp(-ts * 85)
        click = math.sin(2 * math.pi * 2600 * ts) * math.exp(-ts * 400) * 0.35
        attack = min(1.0, ts / 0.0012)
        s = clamp((tone * env + click) * attack * 0.6, -1.0, 1.0)
        frames += struct.pack("<h", int(s * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def _player_cmd(volume: float) -> list[str] | None:
    volume = clamp(volume, 0.0, 1.0)
    if shutil.which("pw-play"):
        return ["pw-play", f"--volume={volume:.2f}", str(SOUND_PATH)]
    if shutil.which("paplay"):
        return ["paplay", f"--volume={int(volume * 65536)}", str(SOUND_PATH)]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-volume", str(int(volume * 100)), str(SOUND_PATH)]
    if shutil.which("aplay"):
        return ["aplay", "-q", str(SOUND_PATH)]
    return None


def play_tock(times: int = 3, volume: float = 0.35) -> None:
    if not SOUND_PATH.exists():
        try:
            generate_tock()
        except OSError:
            return
    cmd = _player_cmd(volume)
    if cmd is None:
        return

    def _worker():
        for i in range(times):
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
            except Exception:
                return
            if i < times - 1:
                time.sleep(0.26)

    threading.Thread(target=_worker, daemon=True).start()


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            run(["notify-send", "-a", APP, "-u", "normal", title, body], timeout=5)
        except Exception:
            pass


def fire_alert(title: str, body: str, cfg: dict) -> None:
    if cfg.get("sound_enabled", True):
        play_tock(3, float(cfg.get("sound_volume", 0.35)))
    if cfg.get("notifications", True):
        notify(title, body)


def check_alerts(stats: dict, cfg: dict) -> list[str]:
    """Fire any newly crossed thresholds. Returns a list of messages."""
    state = load_state()
    start = stats["cycle_start"]
    today = date.today()
    messages: list[str] = []

    if state.get("cycle_key") != start.isoformat():
        state = {"cycle_key": start.isoformat(), "cycle": [],
                 "day_key": today.isoformat(), "day": []}
    if state.get("day_key") != today.isoformat():
        state["day_key"] = today.isoformat()
        state["day"] = []

    thresholds = sorted(float(x) for x in cfg.get("alert_thresholds_pct", [80, 100]))

    cycle_limit = float(cfg["cycle_limit_gb"]) * 1024 ** 3
    cycle_pct = (stats["cycle_used"] / cycle_limit * 100) if cycle_limit else 0
    new = [th for th in thresholds if cycle_limit and cycle_pct >= th
           and f"cycle:{th:g}" not in state["cycle"]]
    if new:
        state["cycle"].extend(f"cycle:{th:g}" for th in new)
        msg = (f"Cycle at {cycle_pct:.0f}% (≥ {max(new):g}%) — "
               f"{gb(stats['cycle_used']):.1f} / {cfg['cycle_limit_gb']:.0f} GB")
        fire_alert(f"{APP}: cycle {max(new):g}%", msg, cfg)
        messages.append(msg)

    daily_limit = float(cfg["daily_limit_gb"]) * 1024 ** 3
    day_pct = (stats["today_total"] / daily_limit * 100) if daily_limit else 0
    new = [th for th in thresholds if daily_limit and day_pct >= th
           and f"day:{th:g}" not in state["day"]]
    if new:
        state["day"].extend(f"day:{th:g}" for th in new)
        msg = (f"Today at {day_pct:.0f}% (≥ {max(new):g}%) — "
               f"{gb(stats['today_total']):.1f} / {cfg['daily_limit_gb']:.0f} GB")
        fire_alert(f"{APP}: today {max(new):g}%", msg, cfg)
        messages.append(msg)

    save_state(state)
    return messages


# --------------------------------------------------------------------------- #
# vnstat daemon control
# --------------------------------------------------------------------------- #


def daemon_status() -> tuple[str, str]:
    try:
        active = run(["systemctl", "is-active", "vnstat"]).stdout.strip() or "unknown"
    except Exception:
        active = "unknown"
    try:
        enabled = run(["systemctl", "is-enabled", "vnstat"]).stdout.strip() or "unknown"
    except Exception:
        enabled = "unknown"
    return active, enabled


def daemon_toggle(action: str) -> tuple[bool, str]:
    try:
        cp = run(["systemctl", action, "vnstat"], timeout=20)
    except Exception as exc:
        return False, str(exc)
    if cp.returncode == 0:
        return True, f"vnstat {'stopped' if action == 'stop' else 'started'}"
    err = (cp.stderr or cp.stdout).strip()
    if any(k in err.lower() for k in ("authentication", "interactive", "polkit")):
        return False, "permission denied — install the polkit rule (see tui/README.md)"
    return False, err or f"systemctl {action} failed"


# --------------------------------------------------------------------------- #
# TUI
# --------------------------------------------------------------------------- #

C_DEFAULT, C_OK, C_WARN, C_BAD, C_ACCENT, C_DIM = 1, 2, 3, 4, 5, 6

SECTIONS = [
    ("PLAN", ["interface", "cycle_start_day", "cycle_limit_gb", "daily_limit_gb"]),
    ("ALERTS", ["alert_thresholds_pct"]),
    ("SOUND & NOTIFICATIONS", ["sound_enabled", "sound_volume", "notifications"]),
    ("ADVANCED", ["refresh_seconds"]),
]

FIELD_META = {
    "interface": ("str", "Interface", "vnstat interface to read"),
    "cycle_start_day": ("int", "Cycle start day", "day of month the plan resets"),
    "cycle_limit_gb": ("float", "Cycle limit (GB)", "data allowance per cycle"),
    "daily_limit_gb": ("float", "Daily limit (GB)", "soft daily budget"),
    "alert_thresholds_pct": ("list", "Alert thresholds %", "warn at these % of a limit"),
    "sound_enabled": ("bool", "Sound", "play the tock alert"),
    "sound_volume": ("float", "Volume", "0.0 - 1.0"),
    "notifications": ("bool", "Notifications", "desktop notification"),
    "refresh_seconds": ("int", "Refresh (s)", "auto-refresh interval"),
}
FIELD_ORDER = [k for _, keys in SECTIONS for k in keys]


def axis_unit(mx: float) -> tuple[float, str]:
    if mx >= 1024 ** 3:
        return 1024 ** 3, "GiB"
    if mx >= 1024 ** 2:
        return 1024 ** 2, "MiB"
    return 1024, "KiB"


class App:
    def __init__(self, stdscr, cfg: dict):
        self.stdscr = stdscr
        self.cfg = cfg
        self.stats: dict | None = None
        self.error: str | None = None
        self.message = ""
        self.alerts: list[str] = []
        self.last_refresh = 0.0
        self.settings_sel = 0
        self.editing = False
        self.edit_buf = ""
        self.running = True
        self.view = "dash"          # dash | settings | help
        self._dirty = True
        self._last_min = None

    # -- data ------------------------------------------------------------- #

    def refresh(self):
        try:
            self.stats = compute_stats(self.cfg["interface"], self.cfg)
            self.error = None
            new = check_alerts(self.stats, self.cfg)
            if new:
                self.alerts = (new + self.alerts)[:4]
        except Exception as exc:
            self.error = str(exc)
        self.last_refresh = time.time()
        self._dirty = True

    # -- primitives ------------------------------------------------------- #

    def put(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w or not text:
            return
        try:
            self.stdscr.addstr(y, x, text[: max(0, w - x)], attr)
        except curses.error:
            pass

    def bar_attr(self, pct: float) -> int:
        if pct >= 100:
            return curses.color_pair(C_BAD) | curses.A_BOLD
        if pct >= 80:
            return curses.color_pair(C_WARN) | curses.A_BOLD
        return curses.color_pair(C_OK)

    def init_colors(self):
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
        curses.init_pair(C_BAD, curses.COLOR_RED, bg)
        curses.init_pair(C_ACCENT, curses.COLOR_CYAN, bg)
        curses.init_pair(C_DIM, curses.COLOR_BLUE, bg)

    # -- main loop -------------------------------------------------------- #

    def run(self):
        curses.curs_set(0)
        self.stdscr.keypad(True)
        self.stdscr.timeout(100)          # blocks up to 100 ms, then returns -1
        try:
            curses.set_escdelay(50)       # parse arrow-key escape sequences
        except (AttributeError, curses.error):
            pass
        self.init_colors()
        self.refresh()
        while self.running:
            if (time.time() - self.last_refresh) >= float(self.cfg["refresh_seconds"]):
                self.refresh()
            cur_min = time.strftime("%H:%M")
            if self._dirty or cur_min != self._last_min:
                self.draw()
                self._dirty = False
                self._last_min = cur_min
            try:
                ch = self.stdscr.getch()
            except curses.error:
                ch = -1
            if ch != -1:
                self.handle_key(ch)
                self._dirty = True

    def draw(self):
        self.stdscr.clear()
        h, w = self.stdscr.getmaxyx()
        if self.view == "settings":
            self.draw_settings(h, w)
        elif self.view == "help":
            self.draw_help(h, w)
        else:
            self.draw_dashboard(h, w)
            self.draw_footer(h, w)
        self.stdscr.refresh()

    # -- header ----------------------------------------------------------- #

    def draw_header(self, w, subtitle=""):
        active, enabled = daemon_status()
        if active == "active":
            dot, dot_attr = "●", curses.color_pair(C_OK) | curses.A_BOLD
            hint = "[d] stop daemon"
        else:
            dot, dot_attr = "○", curses.color_pair(C_BAD) | curses.A_BOLD
            hint = "[d] start daemon"
        clock = time.strftime("%H:%M")
        inner = max(10, w - 2)
        self.put(0, 0, "╭" + "─" * inner + "╮", curses.color_pair(C_ACCENT))
        title = f" {APP} " + (f"· {subtitle} " if subtitle else "")
        right = f" when: {clock} "
        self.put(0, 2, title, curses.color_pair(C_ACCENT) | curses.A_BOLD)
        self.put(0, max(2, w - len(right) - 3), right, curses.A_DIM)
        self.put(1, 0, "│", curses.color_pair(C_ACCENT))
        self.put(1, 2, f"vnstat ", curses.A_DIM)
        self.put(1, 9, dot, dot_attr)
        self.put(1, 11, f"{active}/{enabled}", curses.A_DIM)
        self.put(1, max(2, w - len(hint) - 3), hint, curses.A_BOLD)
        self.put(1, w - 1, "│", curses.color_pair(C_ACCENT))
        self.put(2, 0, "╰" + "─" * inner + "╯", curses.color_pair(C_ACCENT))
        return 3

    # -- dashboard -------------------------------------------------------- #

    def draw_dashboard(self, h, w):
        y = self.draw_header(w)
        if self.stats is None:
            self.put(y + 1, 2, f"no data: {self.error}", curses.color_pair(C_BAD))
            return
        s, cfg = self.stats, self.cfg

        y += 1
        cells = bar_cells_for_width(w - 4)
        cycle_limit = float(cfg["cycle_limit_gb"]) * 1024 ** 3
        cycle_pct = (s["cycle_used"] / cycle_limit * 100) if cycle_limit else 0
        daily_limit = float(cfg["daily_limit_gb"]) * 1024 ** 3
        day_pct = (s["today_total"] / daily_limit * 100) if daily_limit else 0

        # ---- CYCLE ----
        self.put(y, 2, "CYCLE", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, 9, f"{s['cycle_start']} → {s['cycle_end']}   day {s['cycle_elapsed']}/{s['cycle_days']}")
        y += 1
        self.put(y, 2, seg_bar(cycle_pct, cells), self.bar_attr(cycle_pct))
        y += 1
        expected = cycle_limit * s["cycle_elapsed"] / max(1, s["cycle_days"])
        on_track = s["cycle_used"] <= expected * 1.05
        pace = "● ON TRACK" if on_track else "▲ AHEAD OF BUDGET"
        pace_attr = curses.color_pair(C_OK) if on_track else curses.color_pair(C_WARN)
        readout = f"{gb(s['cycle_used']):.1f} / {cfg['cycle_limit_gb']:.0f} GB   {cycle_pct:5.1f}%"
        self.put(y, 2, readout, self.bar_attr(cycle_pct))
        proj = f"projected {gb(s['projected_cycle']):.1f} GB"
        self.put(y, 2 + len(readout) + 4, proj, 0)
        self.put(y, 2 + len(readout) + 4 + len(proj) + 3, pace, pace_attr | curses.A_BOLD)
        y += 2

        # ---- TODAY ----
        self.put(y, 2, "TODAY", curses.A_BOLD | curses.color_pair(C_ACCENT))
        y += 1
        self.put(y, 2, seg_bar(day_pct, cells), self.bar_attr(day_pct))
        y += 1
        self.put(y, 2, f"{gb(s['today_total']):.2f} / {cfg['daily_limit_gb']:.0f} GB   {day_pct:5.1f}%",
                 self.bar_attr(day_pct))
        y += 2

        # ---- AVERAGES | ALERTS (two columns) ----
        left_x, right_x = 2, max(46, w // 2 + 2)
        self.put(y, left_x, "AVERAGES", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, right_x, "ALERTS", curses.A_BOLD | curses.color_pair(C_ACCENT))
        y += 1
        rows = [
            (f"per on-hour   {fmt_bytes(s['avg_hour']):>10}", f"(over {s['on_hours']} h)"),
            (f"per day       {fmt_bytes(s['avg_day']):>10}", "(last 7 d)"),
            (f"per week      {fmt_bytes(s['avg_week']):>10}", ""),
            (f"projected     {fmt_bytes(s['projected_cycle']):>10}", "(per cycle)"),
        ]
        th = ",".join(f"{float(x):g}" for x in cfg.get("alert_thresholds_pct", []))
        alert_rows = [
            f"daily limit {cfg['daily_limit_gb']:.0f} GB  warn at {th}%",
            f"cycle limit {cfg['cycle_limit_gb']:.0f} GB  warn at {th}%",
            f"sound {'on' if cfg['sound_enabled'] else 'off'}"
            f"   notify {'on' if cfg['notifications'] else 'off'}",
            "press  s  to change limits",
        ]
        for i in range(len(rows)):
            txt, dim = rows[i]
            self.put(y + i, left_x, txt, 0)
            if dim:
                self.put(y + i, left_x + len(txt) + 1, dim, curses.A_DIM)
            self.put(y + i, right_x, alert_rows[i],
                     curses.A_DIM if i == 3 else 0)
        y += len(rows) + 1

        # ---- charts (only if the terminal is tall enough) ----
        if h - y - 2 >= 8:
            y = self.draw_days_chart(y, w, s, daily_limit)
        if h - y - 2 >= 7:
            y = self.draw_hours_chart(y, w, s)

        if self.alerts and y < h - 2:
            self.put(y, 2, "• " + self.alerts[0], curses.color_pair(C_WARN))

    def draw_days_chart(self, y, w, s, daily_limit):
        vals, labels = s["day_values"], s["day_labels"]
        if not vals:
            return y
        mx = max(vals) or 1
        div, unit = axis_unit(mx)
        self.put(y, 2, f"LAST {len(vals)} DAYS ({unit})", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, 24, f"peak {mx / div:.1f} {unit}", curses.A_DIM)
        y += 1
        height = 5
        barw = 5
        colw = barw + 2
        x0 = 11
        for row in range(height):
            level = height - row
            self.put(y + row, 2, f"{mx * level / height / div:>7.1f}", curses.A_DIM)
            self.put(y + row, 10, "│", curses.A_DIM)
            for ci, v in enumerate(vals):
                bh = int(round(v / mx * height + 1e-9))
                if level <= bh:
                    attr = curses.color_pair(C_BAD) if daily_limit and v > daily_limit else curses.color_pair(C_OK)
                    self.put(y + row, x0 + ci * colw, "█" * barw, attr | curses.A_BOLD)
        yr = y + height
        self.put(yr, 10, "└" + "─" * (len(vals) * colw), curses.A_DIM)
        for ci, lab in enumerate(labels):
            self.put(yr + 1, x0 + ci * colw, lab.center(barw), curses.A_DIM)
        return yr + 2

    def draw_hours_chart(self, y, w, s):
        vals, labels = s["hour_values"], s["hour_labels"]
        if not vals:
            return y
        vals, labels = vals[-min(len(vals), 24):], labels[-min(len(labels), 24):]
        mx = max(vals) or 1
        div, unit = axis_unit(mx)
        self.put(y, 2, "LAST 24 HOURS", curses.A_BOLD | curses.color_pair(C_ACCENT))
        self.put(y, 24, f"peak {mx / div:.1f} {unit}", curses.A_DIM)
        y += 1
        height = 4
        barw = 2
        colw = barw + 1
        x0 = 11
        for row in range(height):
            level = height - row
            self.put(y + row, 2, f"{mx * level / height / div:>7.1f}", curses.A_DIM)
            self.put(y + row, 10, "│", curses.A_DIM)
            for ci, v in enumerate(vals):
                bh = int(round(v / mx * height + 1e-9))
                if level <= bh:
                    self.put(y + row, x0 + ci * colw, "█" * barw,
                             curses.color_pair(C_ACCENT) | curses.A_BOLD)
        yr = y + height
        self.put(yr, 10, "└" + "─" * (len(vals) * colw), curses.A_DIM)
        for ci, lab in enumerate(labels):
            if ci % 3 == 0 or ci == len(labels) - 1:
                self.put(yr + 1, x0 + ci * colw, lab.center(barw), curses.A_DIM)
        return yr + 2

    # -- settings --------------------------------------------------------- #

    def draw_settings(self, h, w):
        self.put(0, 0, "╭" + "─" * max(10, w - 2) + "╮", curses.color_pair(C_ACCENT))
        self.put(0, 2, f" {APP} · settings ", curses.color_pair(C_ACCENT) | curses.A_BOLD)
        self.put(1, 0, "│", curses.color_pair(C_ACCENT))
        self.put(1, 2, f"config: {CONFIG_PATH}", curses.A_DIM)
        self.put(2, 0, "├" + "─" * max(10, w - 2) + "┤", curses.color_pair(C_ACCENT))

        val_x, help_x = 28, 48
        y = 4
        sel_key = FIELD_ORDER[self.settings_sel]
        for section, keys in SECTIONS:
            self.put(y, 3, section, curses.A_BOLD | curses.color_pair(C_ACCENT))
            y += 1
            for key in keys:
                typ, label, help_ = FIELD_META[key]
                val = self.cfg.get(key)
                if typ == "list":
                    vstr = ",".join(str(x) for x in val)
                elif typ == "bool":
                    vstr = "on" if val else "off"
                elif typ == "float":
                    vstr = f"{float(val):g}"
                else:
                    vstr = str(val)
                selected = key == sel_key
                attr = curses.A_REVERSE if selected else 0
                marker = "▸" if selected else " "
                self.put(y, 3, marker, curses.color_pair(C_ACCENT) if selected else curses.A_DIM)
                self.put(y, 5, f"{label:<20}", attr)
                if selected and self.editing:
                    self.put(y, val_x, f"[{self.edit_buf}█]", curses.color_pair(C_ACCENT) | curses.A_BOLD)
                else:
                    self.put(y, val_x, f"{vstr:<16}", attr)
                self.put(y, help_x, help_[: max(0, w - help_x - 2)], curses.A_DIM)
                y += 1
            y += 1
        self.put(h - 1, 0, (" ↑/↓ move · ←/→ change · Enter edit · t test tock · "
                            "s save & back · Esc back ").ljust(w - 1), curses.A_REVERSE)
        if self.message:
            self.put(h - 2, 3, self.message[: w - 4], curses.color_pair(C_ACCENT))

    # -- help ------------------------------------------------------------- #

    def draw_help(self, h, w):
        self.put(0, 0, "╭" + "─" * max(10, w - 2) + "╮", curses.color_pair(C_ACCENT))
        self.put(0, 2, f" {APP} · help ", curses.color_pair(C_ACCENT) | curses.A_BOLD)
        y = 2
        lines = [
            ("KEYS", ""),
            ("  s", "open settings: limits, alert thresholds, sound"),
            ("  d", "start / stop the vnstat daemon"),
            ("  t", "test the tock sound"),
            ("  r", "refresh now"),
            ("  ?", "this help"),
            ("  q", "quit"),
            ("", ""),
            ("ALERTS", ""),
            ("", "Alerts fire when usage crosses a threshold, expressed as a"),
            ("", "percentage of the daily limit and of the cycle limit."),
            ("", "Example: limits 10 GB/day and 300 GB/cycle with thresholds 80,100"),
            ("", "tocks at  8 GB and 10 GB today, and at 240 GB and 300 GB this cycle."),
            ("", ""),
            ("", "Each threshold fires once per day / per cycle (no nagging)."),
            ("", "Sound is the generated tock.wav; notifications use notify-send."),
            ("", ""),
            ("", "Set them in  s → ALERTS  (alert thresholds %)   and the limits"),
            ("", "right above it in  s → PLAN."),
        ]
        for i, (a, b) in enumerate(lines):
            if i > h - 3:
                break
            if a and not b:
                self.put(y, 3, a, curses.A_BOLD | curses.color_pair(C_ACCENT))
            elif a:
                self.put(y, 5, a, curses.A_BOLD | curses.color_pair(C_ACCENT))
                self.put(y, 9, b, 0)
            else:
                self.put(y, 5, b, 0)
            y += 1
        self.put(h - 1, 0, " any key to go back ".ljust(w - 1), curses.A_REVERSE)

    # -- footer ----------------------------------------------------------- #

    def draw_footer(self, h, w):
        active, _ = daemon_status()
        daemon_key = f"[d] stop daemon" if active == "active" else "[d] start daemon"
        keys = f" [s] settings │ {daemon_key} │ [t] test sound │ [r] refresh │ [?] help │ [q] quit "
        self.put(h - 1, 0, keys.ljust(w - 1), curses.A_REVERSE)
        if self.message:
            self.put(h - 2, 2, self.message[: w - 3], curses.color_pair(C_ACCENT))

    # -- input ------------------------------------------------------------ #

    def handle_key(self, ch):
        if self.view == "help":
            self.view = "dash"
            return
        if self.view == "settings":
            self.handle_settings_key(ch)
            return
        if ch == ord("q"):
            self.running = False
        elif ch == ord("?"):
            self.view = "help"
        elif ch == ord("r"):
            self.refresh()
            self.message = "refreshed"
        elif ch == ord("s"):
            self.view = "settings"
            self.settings_sel = 0
            self.editing = False
        elif ch == ord("t"):
            self.message = "♪ tock"
            play_tock(3, float(self.cfg.get("sound_volume", 0.35)))
        elif ch == ord("d"):
            active, _ = daemon_status()
            action = "stop" if active == "active" else "start"
            ok, msg = daemon_toggle(action)
            self.message = ("✓ " if ok else "✗ ") + msg
            self.refresh()

    def handle_settings_key(self, ch):
        if self.editing:
            if ch in (10, 13, curses.KEY_ENTER):
                self.commit_edit()
            elif ch == 27:
                self.editing = False
                self.edit_buf = ""
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.edit_buf = self.edit_buf[:-1]
            elif 32 <= ch < 127:
                self.edit_buf += chr(ch)
            return

        if ch == 27:
            self.view = "dash"
        elif ch in (ord("q"),):
            self.running = False
        elif ch in (curses.KEY_UP, ord("k")):
            self.settings_sel = (self.settings_sel - 1) % len(FIELD_ORDER)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.settings_sel = (self.settings_sel + 1) % len(FIELD_ORDER)
        elif ch in (curses.KEY_LEFT, ord("-")):
            self.adjust(-1)
        elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self.adjust(1)
        elif ch in (10, 13, curses.KEY_ENTER):
            key = FIELD_ORDER[self.settings_sel]
            typ = FIELD_META[key][0]
            if typ in ("str", "list"):
                self.editing = True
                if typ == "list":
                    self.edit_buf = ",".join(str(x) for x in self.cfg.get(key, []))
                else:
                    self.edit_buf = str(self.cfg.get(key, ""))
            else:
                self.adjust(1)
        elif ch == ord("t"):
            self.message = "♪ tock"
            play_tock(3, float(self.cfg.get("sound_volume", 0.35)))
        elif ch == ord("s"):
            save_config(self.cfg)
            self.message = "settings saved"
            self.view = "dash"

    def adjust(self, delta):
        key = FIELD_ORDER[self.settings_sel]
        typ = FIELD_META[key][0]
        if typ == "bool":
            self.cfg[key] = not bool(self.cfg.get(key))
        elif typ == "int":
            self.cfg[key] = int(self.cfg.get(key, 0)) + delta
        elif typ == "float":
            step = 0.05 if key == "sound_volume" else 1.0
            self.cfg[key] = round(float(self.cfg.get(key, 0)) + delta * step, 3)
        if key == "cycle_start_day":
            self.cfg[key] = clamp(int(self.cfg[key]), 1, 28)
        if key == "refresh_seconds":
            self.cfg[key] = clamp(int(self.cfg[key]), 2, 3600)
        if key == "sound_volume":
            self.cfg[key] = clamp(float(self.cfg[key]), 0.0, 1.0)
        if key in ("cycle_limit_gb", "daily_limit_gb"):
            self.cfg[key] = max(0.0, float(self.cfg[key]))
        if typ in ("bool", "int", "float"):
            save_config(self.cfg)

    def commit_edit(self):
        key = FIELD_ORDER[self.settings_sel]
        typ = FIELD_META[key][0]
        buf = self.edit_buf.strip()
        try:
            if typ == "str":
                self.cfg[key] = buf
            elif typ == "list":
                self.cfg[key] = sorted({float(x) for x in buf.replace(" ", "").split(",") if x})
        except ValueError:
            self.message = "invalid value"
        else:
            save_config(self.cfg)
            self.message = "saved"
        self.editing = False
        self.edit_buf = ""


# --------------------------------------------------------------------------- #
# --once summary
# --------------------------------------------------------------------------- #


def print_summary(cfg: dict) -> int:
    try:
        s = compute_stats(cfg["interface"], cfg)
    except Exception as exc:
        print(f"{APP}: error: {exc}")
        return 1
    active, enabled = daemon_status()
    limit = float(cfg["cycle_limit_gb"]) * 1024 ** 3
    pct = (s["cycle_used"] / limit * 100) if limit else 0
    print(f"{APP} — interface {cfg['interface']} (vnstat daemon: {active}/{enabled})")
    print(f"cycle      : {s['cycle_start']} → {s['cycle_end']} "
          f"(day {s['cycle_elapsed']}/{s['cycle_days']})")
    print(f"cycle used : {gb(s['cycle_used']):.2f} / {cfg['cycle_limit_gb']:.0f} GB ({pct:.1f}%)")
    print(f"projected  : {gb(s['projected_cycle']):.2f} GB")
    print(f"today      : {gb(s['today_total']):.2f} / {cfg['daily_limit_gb']:.0f} GB")
    print(f"last 24h   : {fmt_bytes(s['last24'])}")
    print(f"avg/on-hour: {fmt_bytes(s['avg_hour'])}")
    print(f"avg/day    : {fmt_bytes(s['avg_day'])}")
    print(f"avg/week   : {fmt_bytes(s['avg_week'])}")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="friendly vnstat TUI dashboard")
    ap.add_argument("--once", action="store_true", help="print a summary and exit")
    ap.add_argument("--check", action="store_true",
                    help="evaluate alert thresholds, notify, then exit (cron/timer)")
    ap.add_argument("--interface", help="override interface")
    ap.add_argument("--generate-sound", action="store_true",
                    help="(re)generate tock.wav and exit")
    args = ap.parse_args(argv)

    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
    cfg = load_config()
    if args.interface:
        cfg["interface"] = args.interface

    if args.generate_sound:
        generate_tock()
        print(f"wrote {SOUND_PATH}")
        return 0

    if shutil.which("vnstat") is None:
        print(f"{APP}: vnstat is not installed", file=sys.stderr)
        return 1

    if args.once:
        return print_summary(cfg)

    if args.check:
        try:
            stats = compute_stats(cfg["interface"], cfg)
        except Exception as exc:
            print(f"{APP}: error: {exc}", file=sys.stderr)
            return 1
        for msg in check_alerts(stats, cfg):
            print(msg)
        return 0

    curses.wrapper(lambda scr: App(scr, cfg).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
