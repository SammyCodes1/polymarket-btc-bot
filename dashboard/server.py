"""Local web dashboard for the Polymarket BTC Up/Down bot.

Run:  python dashboard/server.py            (http://127.0.0.1:8787)
      python dashboard/server.py --open     (also opens the browser)

The dashboard only READS the bot's files (data/state.json, trades.csv, decisions.csv, bot.log)
and controls the bot as a separate process. It never imports bot.py and never touches trading logic.

Security model
- Binds to 127.0.0.1 by default. Binding to another host requires DASHBOARD_TOKEN.
- Secrets (private key, relayer/API keys, LIVE_TRADING_CONFIRM, wallet address) are never returned.
- Only a whitelist of strategy/risk keys can be edited. DRY_RUN=false needs a typed confirmation.
- Control endpoints need JSON bodies, a matching Host/Origin (blocks CSRF / DNS rebinding) and,
  if DASHBOARD_TOKEN is set, the X-Dashboard-Token header.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import psutil
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

# -----------------------------------------------------------------------------
# Paths and settings
# -----------------------------------------------------------------------------
DASH_DIR = Path(__file__).resolve().parent
BOT_DIR = Path(os.getenv("BOT_DIR", DASH_DIR.parent)).resolve()
BOT_PY = BOT_DIR / "bot.py"
ENV_FILE = Path(os.getenv("DASHBOARD_ENV_FILE", BOT_DIR / ".env")).resolve()
STATIC_DIR = DASH_DIR / "static"
IS_WIN = os.name == "nt"
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_MONEY"   # only compared, never returned
UI_LIVE_PHRASE = "GO LIVE"                                  # typed in the UI to switch DRY_RUN off
STOP_GRACE_SECONDS = 45

# Keys whose values must never leave the server.
SECRET_KEYS = {"POLYMARKET_PRIVATE_KEY", "POLYMARKET_WALLET_ADDRESS", "POLYMARKET_RELAYER_API_KEY",
               "POLYMARKET_RELAYER_API_KEY_ADDRESS", "LIVE_TRADING_CONFIRM", "CRYPTOPANIC_API_KEY",
               "NEWSAPI_KEY", "LLM_API_KEY", "DASHBOARD_TOKEN", "POLYMARKET_BUILDER_API_KEY",
               "POLYMARKET_BUILDER_API_SECRET", "POLYMARKET_BUILDER_API_PASSPHRASE"}
ENV_EXAMPLE = BOT_DIR / ".env.example"
KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Starter risk settings shown on the first-run screen (defaults come from .env.example).
STARTER_KEYS = ["MAX_POSITION_USD", "MAX_TRADES_PER_DAY", "DAILY_LOSS_LIMIT_PCT", "STOP_LOSS_PCT",
                "MAX_DRAWDOWN_PCT", "FAV_MIN_PRICE", "FAV_MAX_PRICE"]

# Read-only keys shown in the UI (defaults mirror bot.py's Config).
DISPLAY_DEFAULTS: dict[str, Any] = {
    "STRATEGY": "favorite", "FAV_DURATIONS": "15m", "MAX_POSITION_PCT": 0.02, "KELLY_FRACTION": 0.25,
    "MAX_TOTAL_BTC_EXPOSURE_PCT": 0.05, "MAX_NET_DIRECTION_PCT": 0.04, "MAX_CONSECUTIVE_LOSSES": 4,
    "COOLDOWN_AFTER_LOSS_SECONDS": 600, "MAX_SPREAD": 0.03, "ARB_ENABLED": True, "USE_NEWS_IN_MODEL": False,
    "LOOP_SECONDS": 5.0, "SCAN_SECONDS": 20.0, "KILL_SWITCH_FILE": "KILL", "PAPER_BANKROLL_USD": 500.0,
}

# Editable whitelist: key -> (type, min, max, default, label, help)
EDITABLE: dict[str, dict[str, Any]] = {
    "FAV_MIN_PRICE":       dict(type="float", min=0.50, max=0.99, step=0.01, default=0.85, group="Strategy",
                                label="Favorite min price", help="Buy only when the favorite's mid is at least this (0.50–0.99)."),
    "FAV_MAX_PRICE":       dict(type="float", min=0.50, max=0.99, step=0.01, default=0.95, group="Strategy",
                                label="Favorite max price", help="…and below this. Ask must also be ≤ this."),
    "FAV_MIN_SECS_LEFT":   dict(type="int", min=0, max=900, step=30, default=600, group="Strategy",
                                label="Min seconds left", help="Window must have more than this many seconds left."),
    "FAV_MAX_SECS_LEFT":   dict(type="int", min=0, max=900, step=30, default=900, group="Strategy",
                                label="Max seconds left", help="…and at most this many (15m window = 900 s)."),
    "FAV_EDGE_EST":        dict(type="float", min=-0.05, max=0.10, step=0.005, default=0.015, group="Strategy",
                                label="Assumed edge over mid", help="Edge assumed for sizing and the after-fee check. Higher = looser."),
    "FAV_MIN_LEAD_USD":    dict(type="float", min=0.0, max=500.0, step=5.0, default=0.0, group="Strategy",
                                label="Min BTC lead ($)", help="Skip entries unless BTC is at least this many $ past the strike in the bet's direction. 0 = off. Skips if the strike or a fresh BTC price is missing."),
    "MAX_POSITION_USD":    dict(type="float", min=1.0, max=1000.0, step=0.1, default=10.0, group="Risk",
                                label="Max $ per trade", help="Hard cap on each trade's cost."),
    "MAX_TRADES_PER_DAY":  dict(type="int", min=0, max=200, step=1, default=20, group="Risk",
                                label="Max trades per day", help="Entries per UTC day (0 blocks all entries)."),
    "MAX_TOTAL_EXPOSURE_PCT": dict(type="float", min=0.0, max=1.0, step=0.05, default=0.0, group="Risk",
                                label="Max total exposure", help="Skip a new entry if open positions + resting orders + the new stake would exceed this share of the bankroll. 0 = off. If the 5-share minimum order alone exceeds it, the entry is skipped."),
    "STALE_FEED_KILL_SECONDS": dict(type="int", min=0, max=600, step=5, default=0, group="Risk",
                                label="Stale-feed kill (s)", help="Pause NEW entries when the BTC price feed or market scan is older than this many seconds; resumes automatically. Exits are not affected. 0 = off."),
    "MAX_OPEN_POSITIONS":  dict(type="int", min=1, max=10, step=1, default=2, group="Risk",
                                label="Max open positions", help="Concurrent positions."),
    "TAKE_PROFIT_MIN_BID": dict(type="float", min=0.50, max=1.00, step=0.01, default=0.97, group="Exits",
                                label="Take-profit bid", help="Sell when the bid reaches this. 1.00 effectively holds to resolution."),
    "STOP_LOSS_PCT":       dict(type="float", min=0.05, max=1.00, step=0.05, default=0.60, group="Exits",
                                label="Stop loss", help="Fraction of cost lost before selling (0.60 = −60%)."),
    "DAILY_LOSS_LIMIT_PCT": dict(type="float", min=0.01, max=1.00, step=0.01, default=0.05, group="Risk",
                                 label="Daily loss limit", help="Halt for the UTC day after losing this fraction of the day's start bankroll."),
    "WEEKLY_LOSS_LIMIT_PCT": dict(type="float", min=0.0, max=1.00, step=0.01, default=0.0, group="Risk",
                                 label="Weekly loss limit", help="Halt new entries for the rest of the UTC week (Mon-Sun) after losing this fraction of the week's start bankroll. 0 = off."),
    "MAX_DRAWDOWN_PCT":    dict(type="float", min=0.01, max=1.00, step=0.01, default=0.10, group="Risk",
                                label="Max drawdown", help="Halt when bankroll falls this far below its peak."),
    "DRY_RUN":             dict(type="bool", default=True, group="Mode",
                                label="Paper mode (DRY_RUN)", help="Off = real money. Needs typed confirmation."),
}

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


# -----------------------------------------------------------------------------
# .env helpers
# -----------------------------------------------------------------------------
def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if " #" in v:                       # inline comment
        v = v.split(" #", 1)[0].rstrip()
    return v


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def _write_private(path: Path, text: str) -> None:
    """Atomic write, owner-only permissions where the OS supports it (chmod 600)."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def write_env_values(updates: dict[str, str]) -> None:
    """Replace KEY=... lines in place (keeping comments/order), append missing keys. Atomic.
    A missing .env is created from .env.example so the file keeps its comments."""
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    elif ENV_EXAMPLE.exists():
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    done: set[str] = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if m and m.group(1) in updates:
            k = m.group(1)
            if k in done:
                lines[i] = "# " + line + "   # (duplicate, disabled by dashboard)"
            else:
                lines[i] = f"{k}={updates[k]}"
                done.add(k)
    missing = [k for k in updates if k not in done]
    if missing:
        lines.append("")
        lines.append("# --- set by dashboard ---")
        lines.extend(f"{k}={updates[k]}" for k in missing)
    # No backup copy on purpose: the key must live only in .env. Remove one left by older versions.
    ENV_FILE.with_name(ENV_FILE.name + ".bak").unlink(missing_ok=True)
    _write_private(ENV_FILE, "\n".join(lines) + "\n")


def _as_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _coerce(raw: Optional[str], default: Any) -> Any:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        if isinstance(default, bool):
            return _as_bool(raw)
        if isinstance(default, int):
            return int(float(raw))
        if isinstance(default, float):
            return float(raw)
    except ValueError:
        return default
    return raw


def data_dir() -> Path:
    env = read_env()
    d = os.getenv("DATA_DIR") or env.get("DATA_DIR") or str(BOT_DIR / "data")
    p = Path(d)
    if not p.is_absolute():
        p = BOT_DIR / p
    return p


def dashboard_token() -> str:
    return os.getenv("DASHBOARD_TOKEN") or read_env().get("DASHBOARD_TOKEN", "")


def mode_info(env: Optional[dict] = None) -> dict:
    env = env if env is not None else read_env()
    dry = _coerce(env.get("DRY_RUN"), True)
    armed = env.get("LIVE_TRADING_CONFIRM", "") == LIVE_CONFIRM_PHRASE
    has_key = bool(env.get("POLYMARKET_PRIVATE_KEY"))
    if dry:
        mode = "PAPER"
    else:
        mode = "LIVE" if armed else "LIVE_NOT_ARMED"
    return {"mode": mode, "dry_run": dry, "live_armed": armed, "wallet_key_configured": has_key,
            "env_file_exists": ENV_FILE.exists()}


def mask_address(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}" if a and len(a) > 12 else ("set" if a else "")


def example_defaults() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_EXAMPLE.exists():
        for line in ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _ENV_LINE.match(line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = _unquote(m.group(2))
    return out


def wallet_info(env: Optional[dict] = None) -> dict:
    env = env if env is not None else read_env()
    key, addr = env.get("POLYMARKET_PRIVATE_KEY", ""), env.get("POLYMARKET_WALLET_ADDRESS", "")
    return {"env_file_exists": ENV_FILE.exists(), "key_set": bool(key), "key_valid": bool(KEY_RE.match(key)) if key else False,
            "address_set": bool(addr), "address_masked": mask_address(addr),
            "needs_setup": (not ENV_FILE.exists()) or not key or not addr}


# -----------------------------------------------------------------------------
# Secret redaction for anything text-like we return (log lines, stderr)
# -----------------------------------------------------------------------------
_REDACT_RE = re.compile(r"(?i)((?:private[_ ]?key|secret|api[_ ]?key|token|password|passphrase)\s*[=:]\s*)(\S+)")


def redact(text: str, env: Optional[dict] = None) -> str:
    env = env if env is not None else read_env()
    for k in SECRET_KEYS:
        v = env.get(k, "")
        if v and len(v) >= 6:
            text = text.replace(v, "[redacted]")
            if v.lower().startswith("0x"):
                text = text.replace(v[2:], "[redacted]")
    return _REDACT_RE.sub(r"\1[redacted]", text)


# -----------------------------------------------------------------------------
# File readers
# -----------------------------------------------------------------------------
def read_json(path: Path) -> Optional[dict]:
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, PermissionError, OSError):
            time.sleep(0.05)     # caught the bot mid-write
    return None


def tail_lines(path: Path, n: int, max_bytes: int = 4_000_000) -> list[str]:
    """Last n lines of a (possibly huge) text file without reading it all."""
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block, data = 65536, b""
        pos = size
        while pos > 0 and data.count(b"\n") <= n and size - pos < max_bytes:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]          # first line is probably partial
    return lines[-n:]


def csv_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            return next(csv.reader(f), [])
    except FileNotFoundError:
        return []


def read_csv_tail(path: Path, n: int) -> list[dict]:
    header = csv_header(path)
    if not header:
        return []
    lines = [ln for ln in tail_lines(path, n + 1) if ln.strip()]
    rows = []
    for rec in csv.reader(io.StringIO("\n".join(lines))):
        if rec == header:
            continue
        rows.append({h: (rec[i] if i < len(rec) else "") for i, h in enumerate(header)})
    return rows[-n:]


def read_csv_all(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except ValueError:
        return None


def _ts(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# -----------------------------------------------------------------------------
# Process control
# -----------------------------------------------------------------------------
_proc_lock = threading.Lock()
_op_state: dict[str, Any] = {"op": None, "since": 0.0, "message": ""}


def pid_file() -> Path:
    return data_dir() / "bot.pid"


def _is_bot_process(p: psutil.Process) -> bool:
    try:
        cmd = p.cmdline()
    except (psutil.Error, OSError):
        return False
    if not cmd or "python" not in Path(cmd[0]).name.lower():
        return False
    for arg in cmd[1:]:
        if arg.lower().endswith("bot.py"):
            ap = Path(arg)
            if not ap.is_absolute():
                try:
                    ap = Path(p.cwd()) / ap
                except (psutil.Error, OSError):
                    continue
            try:
                if os.path.normcase(str(ap.resolve())) == os.path.normcase(str(BOT_PY)):
                    return True
            except OSError:
                continue
    return False


def find_bot_processes() -> list[psutil.Process]:
    """Top-level bot.py processes for THIS bot folder (a Windows venv launcher + its child count as one)."""
    matches: dict[int, psutil.Process] = {}
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if "python" in (p.info.get("name") or "").lower() and _is_bot_process(p):
                matches[p.pid] = p
        except (psutil.Error, OSError):
            continue
    roots = []
    for pid, p in matches.items():
        try:
            if p.ppid() not in matches:
                roots.append(p)
        except psutil.Error:
            continue
    return roots


def process_status() -> dict:
    procs = find_bot_processes()
    info = read_json(pid_file()) or {}
    if isinstance(info, int):  # plain-number pid file (bot started outside the dashboard)
        info = {"pid": info}
    elif not isinstance(info, dict):
        info = {}
    out: dict[str, Any] = {"running": bool(procs), "pids": [p.pid for p in procs], "pid": None,
                           "started_at": None, "uptime_s": None, "args": [], "started_by": None,
                           "op": _op_state["op"], "op_message": _op_state["message"]}
    if procs:
        p = procs[0]
        try:
            out["pid"] = p.pid
            out["started_at"] = p.create_time()
            out["uptime_s"] = max(0.0, time.time() - p.create_time())
            cmd = p.cmdline()
            i = next((k for k, a in enumerate(cmd) if a.lower().endswith("bot.py")), len(cmd))
            out["args"] = cmd[i + 1:]
        except psutil.Error:
            pass
        out["started_by"] = "dashboard" if info.get("pid") in out["pids"] else "external"
    dd = data_dir()
    log = dd / "bot.log"
    out["last_log_at"] = log.stat().st_mtime if log.exists() else None
    out["kill_file_present"] = kill_file_present()
    err = dd / "run_stderr.log"
    if not procs and err.exists() and err.stat().st_size:
        out["last_stderr"] = redact("\n".join(tail_lines(err, 6)))[-1500:]
    return out


def kill_file_paths() -> list[Path]:
    name = read_env().get("KILL_SWITCH_FILE") or "KILL"
    return [BOT_DIR / name, data_dir() / name]


def kill_file_present() -> bool:
    return any(p.exists() for p in kill_file_paths())


def venv_python() -> str:
    cands = [BOT_DIR / ".venv" / "Scripts" / "python.exe", BOT_DIR / ".venv" / "bin" / "python"]
    for c in cands:
        if c.exists():
            return str(c)
    return sys.executable


def start_bot() -> dict:
    with _proc_lock:
        if find_bot_processes():
            raise HTTPException(409, "The bot is already running.")
        if kill_file_present():
            raise HTTPException(409, "A KILL file is present. The bot would halt and flatten immediately. "
                                     "Use 'Clear halt' first.")
        if not BOT_PY.exists():
            raise HTTPException(500, f"bot.py not found at {BOT_PY}")
        dd = data_dir()
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "STOP").unlink(missing_ok=True)
        env = os.environ.copy()
        file_env = read_env()
        if ENV_FILE != (BOT_DIR / ".env").resolve():
            env.update(file_env)       # non-default .env (tests): pass values through; bot's load_dotenv won't override
        env.pop("DASHBOARD_TOKEN", None)
        env["DATA_DIR"] = str(dd)
        env["PYTHONUNBUFFERED"] = "1"
        err = open(dd / "run_stderr.log", "w", encoding="utf-8")
        kwargs: dict[str, Any] = dict(cwd=str(BOT_DIR), env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=err)
        if IS_WIN:
            # Own process group + no console: survives the dashboard window closing.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([venv_python(), str(BOT_PY)], **kwargs)
        err.close()
        info = {"pid": proc.pid, "started_at": time.time(), "started_by": "dashboard",
                "python": venv_python(), "mode": mode_info(file_env)["mode"]}
        pid_file().write_text(json.dumps(info), encoding="utf-8")
    # Catch instant crashes (e.g. live without LIVE_TRADING_CONFIRM).
    time.sleep(1.5)
    if proc.poll() is not None:
        tail = redact("\n".join(tail_lines(dd / "run_stderr.log", 8)))
        raise HTTPException(500, f"bot exited immediately (code {proc.returncode}). {tail[-800:]}")
    return {"ok": True, "pid": proc.pid}


def _kill_tree(p: psutil.Process) -> None:
    try:
        kids = p.children(recursive=True)
    except psutil.Error:
        kids = []
    for k in kids + [p]:
        try:
            k.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(kids + [p], timeout=5)


def force_kill() -> dict:
    procs = find_bot_processes()
    for p in procs:
        _kill_tree(p)
    (data_dir() / "STOP").unlink(missing_ok=True)
    pid_file().unlink(missing_ok=True)
    return {"ok": True, "killed": [p.pid for p in procs]}


def request_graceful_stop() -> list[psutil.Process]:
    procs = find_bot_processes()
    if not procs:
        return []
    data_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / "STOP").write_text(str(time.time()), encoding="utf-8")
    if not IS_WIN:
        for p in procs:
            try:
                p.send_signal(signal.SIGTERM)
            except psutil.Error:
                pass
    return procs


def stop_bot_blocking(grace: float = STOP_GRACE_SECONDS) -> dict:
    procs = request_graceful_stop()
    if not procs:
        return {"ok": True, "was_running": False}
    deadline = time.time() + grace
    while time.time() < deadline and find_bot_processes():
        time.sleep(0.5)
    forced = False
    if find_bot_processes():
        force_kill()
        forced = True
    (data_dir() / "STOP").unlink(missing_ok=True)
    pid_file().unlink(missing_ok=True)
    return {"ok": True, "was_running": True, "forced": forced}



# --- VPS patch: bot is managed by systemd (polybot.service); never spawn a second copy ---
def start_bot() -> dict:
    with _proc_lock:
        if find_bot_processes():
            raise HTTPException(409, "The bot is already running.")
        if kill_file_present():
            raise HTTPException(409, "A KILL file is present. Use 'Clear halt' first.")
        (data_dir() / "STOP").unlink(missing_ok=True)
        subprocess.run(["sudo", "-n", "systemctl", "start", "polybot"], check=False)
    time.sleep(2)
    if not find_bot_processes():
        raise HTTPException(500, "bot did not start; check data/bot.log")
    return {"ok": True}


def stop_bot_blocking(grace: float = STOP_GRACE_SECONDS) -> dict:
    was = bool(find_bot_processes())
    subprocess.run(["sudo", "-n", "systemctl", "stop", "polybot"], check=False, timeout=60)
    (data_dir() / "STOP").unlink(missing_ok=True)
    return {"ok": True, "was_running": was, "forced": False}


def _run_op(name: str, fn) -> None:
    if _op_state["op"]:
        raise HTTPException(409, f"Busy: {_op_state['op']} in progress.")
    _op_state.update(op=name, since=time.time(), message="")

    def worker() -> None:
        try:
            res = fn()
            _op_state["message"] = f"{name}: done" + (" (force-killed after grace period)" if isinstance(res, dict) and res.get("forced") else "")
        except HTTPException as e:
            _op_state["message"] = f"{name} failed: {e.detail}"
        except Exception as e:  # noqa: BLE001
            _op_state["message"] = f"{name} failed: {e}"
        finally:
            _op_state["op"] = None
    threading.Thread(target=worker, daemon=True).start()


# -----------------------------------------------------------------------------
# Market data for open positions (public, read-only)
# -----------------------------------------------------------------------------
_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": "pm-btc-bot-dashboard"})
_book_cache: dict[str, tuple[float, dict]] = {}


def clob_url() -> str:
    return read_env().get("CLOB_URL") or "https://clob.polymarket.com"


def gamma_url() -> str:
    return read_env().get("GAMMA_URL") or "https://gamma-api.polymarket.com"


def book_quote(token_id: str) -> dict:
    now = time.time()
    hit = _book_cache.get(token_id)
    if hit and now - hit[0] < 3:
        return hit[1]
    q: dict[str, Any] = {"bid": None, "ask": None, "source": None, "error": None}
    try:
        r = _HTTP.get(f"{clob_url()}/book", params={"token_id": token_id}, timeout=4)
        if r.status_code == 200:
            b = r.json()
            bids = [float(x["price"]) for x in b.get("bids", []) if float(x.get("size", 0)) > 0]
            asks = [float(x["price"]) for x in b.get("asks", []) if float(x.get("size", 0)) > 0]
            q.update(bid=max(bids) if bids else None, ask=min(asks) if asks else None, source="clob")
        else:
            q["error"] = f"book HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        q["error"] = f"book: {type(e).__name__}"
    _book_cache[token_id] = (now, q)
    return q


def gamma_outcome(slug: str, side: str) -> Optional[dict]:
    key = f"g:{slug}:{side}"
    now = time.time()
    hit = _book_cache.get(key)
    if hit and now - hit[0] < 10:
        return hit[1]
    res = None
    try:
        r = _HTTP.get(f"{gamma_url()}/events", params={"slug": slug}, timeout=4)
        ev = r.json()
        m = ev[0]["markets"][0]
        prices = [float(x) for x in json.loads(m["outcomePrices"])]
        outcomes = [o.lower() for o in json.loads(m["outcomes"])]
        res = {"price": prices[outcomes.index(side.lower())], "closed": bool(m.get("closed"))}
    except Exception:  # noqa: BLE001
        res = None
    _book_cache[key] = (now, res)
    return res


def enrich_positions(state: dict) -> list[dict]:
    out = []
    for key, p in (state.get("positions") or {}).items():
        p = dict(p)
        p["key"] = key
        shares = float(p.get("shares") or 0)
        cost = float(p.get("cost_usd") or 0)
        q = book_quote(str(p.get("token_id", ""))) if p.get("token_id") else {"bid": None, "ask": None, "error": "no token"}
        p["bid"], p["ask"], p["quote_error"] = q.get("bid"), q.get("ask"), q.get("error")
        mark, mark_src = q.get("bid"), "best bid"
        if mark is None:
            g = gamma_outcome(p.get("slug", ""), p.get("side", "UP"))
            if g:
                mark, mark_src = g["price"], ("resolved" if g["closed"] else "gamma price")
        p["mark"], p["mark_source"] = mark, (mark_src if mark is not None else None)
        p["value_usd"] = round(shares * mark, 4) if mark is not None else None
        p["unrealized_pnl"] = round(shares * mark - cost, 4) if mark is not None else None
        p["unrealized_pct"] = round((shares * mark - cost) / cost, 4) if (mark is not None and cost) else None
        p["max_payout"] = round(shares, 4)
        p["secs_left"] = round(float(p.get("end_ts", 0)) - time.time()) if p.get("end_ts") else None
        p.pop("order_id", None)
        out.append(p)
    return out


# -----------------------------------------------------------------------------
# Stats
# -----------------------------------------------------------------------------
EXIT_ACTIONS = {"SELL", "RESOLVE", "EXIT", "CLOSE", "SETTLE"}


def trade_stats(rows: list[dict], mode: str) -> dict:
    if mode in ("LIVE", "PAPER"):
        rows = [r for r in rows if (r.get("mode") or "").upper() == mode]
    entries = [r for r in rows if (r.get("action") or "").upper() == "BUY" and _as_bool(r.get("ok", "True"))]
    exits = [r for r in rows if (r.get("action") or "").upper() in EXIT_ACTIONS and _f(r.get("pnl")) is not None
             and _as_bool(r.get("ok", "True"))]
    pnls = [_f(r["pnl"]) or 0.0 for r in exits]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    series, cum = [], 0.0
    first = rows[0] if rows else None
    if first and _ts(first.get("time", "")):
        series.append({"t": _ts(first["time"]), "pnl": 0.0})
    peak, mdd = 0.0, 0.0
    for r in exits:
        cum += _f(r["pnl"]) or 0.0
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
        t = _ts(r.get("time", ""))
        if t:
            series.append({"t": t, "pnl": round(cum, 4), "trade": round(_f(r["pnl"]) or 0.0, 4),
                           "slug": r.get("slug", ""), "reason": r.get("reason", "")})
    fees = sum(_f(r.get("fee")) or 0.0 for r in rows if _as_bool(r.get("ok", "True")))
    volume = sum(_f(r.get("usd")) or 0.0 for r in entries)
    return {"mode": mode, "entries": len(entries), "closed": len(exits), "wins": len(wins), "losses": len(losses),
            "win_rate": (len(wins) / len(exits)) if exits else None, "realized_pnl": round(sum(pnls), 4),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
            "best": round(max(pnls), 4) if pnls else None, "worst": round(min(pnls), 4) if pnls else None,
            "fees": round(fees, 4), "volume": round(volume, 4), "max_drawdown": round(mdd, 4), "series": series}


def config_view() -> dict:
    env = read_env()
    editable = {}
    for k, spec in EDITABLE.items():
        editable[k] = {**spec, "value": _coerce(env.get(k), spec["default"]), "from_env": k in env}
    display = {k: _coerce(env.get(k), d) for k, d in DISPLAY_DEFAULTS.items()}
    return {"editable": editable, "display": display, **mode_info(env), "wallet": wallet_info(env), "env_file": str(ENV_FILE.name),
            "ui_live_phrase": UI_LIVE_PHRASE}


def validate_updates(values: dict) -> dict[str, str]:
    env = read_env()
    errors, out = [], {}
    typed: dict[str, Any] = {}
    for k, v in values.items():
        if k not in EDITABLE:
            errors.append(f"{k} is not editable from the dashboard")
            continue
        spec = EDITABLE[k]
        try:
            if spec["type"] == "bool":
                if isinstance(v, bool):
                    b = v
                elif str(v).strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                    b = _as_bool(v)
                else:
                    raise ValueError
                typed[k], out[k] = b, "true" if b else "false"
                continue
            num = float(v)
            if num != num or num in (float("inf"), float("-inf")):
                raise ValueError
            if spec["type"] == "int":
                if num != int(num):
                    raise ValueError
                num = int(num)
        except (ValueError, TypeError):
            errors.append(f"{k}: not a valid {spec['type']}")
            continue
        if not (spec["min"] <= num <= spec["max"]):
            errors.append(f"{k}: must be between {spec['min']} and {spec['max']}")
            continue
        typed[k] = num
        out[k] = str(num) if spec["type"] == "int" else f"{num:.6g}"
    eff = lambda k: typed.get(k, _coerce(env.get(k), EDITABLE[k]["default"]))  # noqa: E731
    if eff("FAV_MIN_PRICE") >= eff("FAV_MAX_PRICE"):
        errors.append("FAV_MIN_PRICE must be below FAV_MAX_PRICE")
    if eff("FAV_MIN_SECS_LEFT") >= eff("FAV_MAX_SECS_LEFT"):
        errors.append("FAV_MIN_SECS_LEFT must be below FAV_MAX_SECS_LEFT")
    if errors:
        raise HTTPException(422, "; ".join(errors))
    return out


# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(title="Polymarket BTC bot dashboard", docs_url=None, redoc_url=None, openapi_url=None)
_ALLOWED_HOSTS: set[str] = set()


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    if _ALLOWED_HOSTS and host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "host not allowed"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1].lower() not in _ALLOWED_HOSTS | {host}:
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        if "application/json" not in (request.headers.get("content-type") or ""):
            return JSONResponse({"detail": "JSON body required"}, status_code=415)
        tok = dashboard_token()
        if tok and request.headers.get("x-dashboard-token") != tok:
            return JSONResponse({"detail": "dashboard token required"}, status_code=401)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/health")
def health():
    return {"ok": True, "time": time.time()}


@app.get("/api/status")
def api_status():
    return {"process": process_status(), **mode_info(), "needs_setup": wallet_info()["needs_setup"],
            "token_required": bool(dashboard_token()),
            "server_time": time.time(), "bot_dir": BOT_DIR.name, "data_dir_exists": data_dir().exists()}


@app.get("/api/state")
def api_state():
    st = read_json(data_dir() / "state.json")
    if st is None:
        return {"available": False}
    st = {k: v for k, v in st.items() if k != "traded_slugs"}
    for grp in ("positions", "pending_redeem"):
        for p in (st.get(grp) or {}).values():
            if isinstance(p, dict):
                p.pop("order_id", None)
    return {"available": True, **st}


@app.get("/api/positions")
def api_positions():
    st = read_json(data_dir() / "state.json") or {}
    pos = enrich_positions(st)
    return {"positions": pos, "unrealized_total": round(sum(p["unrealized_pnl"] or 0 for p in pos), 4)}


@app.get("/api/trades")
def api_trades(limit: int = 200):
    rows = read_csv_all(data_dir() / "trades.csv")
    limit = max(1, min(limit, 5000))
    return {"trades": list(reversed(rows[-limit:])), "total": len(rows)}


@app.get("/api/stats")
def api_stats(mode: str = "auto"):
    rows = read_csv_all(data_dir() / "trades.csv")
    if mode == "auto":
        m = mode_info()["mode"]
        mode = "LIVE" if m.startswith("LIVE") else "PAPER"
        if rows and not any((r.get("mode") or "").upper() == mode for r in rows):
            mode = "ALL"
    return trade_stats(rows, mode.upper())


NOISE_VERDICTS = {"skip:fav_time", "skip:fav_duration", "skip:window_not_started"}


@app.get("/api/decisions")
def api_decisions(limit: int = 60, interesting: bool = True):
    limit = max(1, min(limit, 500))
    path = data_dir() / "decisions.csv"
    rows = read_csv_tail(path, 4000 if interesting else limit)
    if interesting:
        rows = [r for r in rows if r.get("verdict") not in NOISE_VERDICTS]
    # collapse identical consecutive verdicts per slug (bot re-evaluates every loop)
    out: list[dict] = []
    for r in rows:
        if out and out[-1].get("slug") == r.get("slug") and out[-1].get("verdict") == r.get("verdict") \
                and out[-1].get("side") == r.get("side"):
            out[-1]["_repeat"] = out[-1].get("_repeat", 1) + 1
            out[-1]["_last_time"] = r.get("time") or r.get("ts")
            for k in ("bid", "ask", "p_up", "secs_left", "edge", "note"):
                out[-1][k] = r.get(k, out[-1].get(k))
            continue
        out.append(dict(r))
    counts: dict[str, int] = {}
    for r in read_csv_tail(path, 4000):
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    return {"decisions": list(reversed(out[-limit:])), "verdict_counts": counts}


_SERIES_SLUG_RE = re.compile(r"^btc-updown-(5m|15m)-(\d{9,12})$")


@app.get("/api/trade-series")
def api_trade_series(slug: str, side: str):
    """Read-only: recorded price points of one market, expressed as the price of `side` (used by the share card).

    Source is data/decisions.csv: when a row's favorite side is the requested side we use its best bid; when it is
    the opposite side we use 1 - its ask (the equivalent bid of the requested side in a binary market).
    """
    m = _SERIES_SLUG_RE.match(slug or "")
    side = (side or "").upper()
    if not m or side not in ("UP", "DOWN"):
        raise HTTPException(status_code=400, detail="bad slug or side")
    start_ts = int(m.group(2))
    end_ts = start_ts + (900 if m.group(1) == "15m" else 300)
    path = data_dir() / "decisions.csv"
    header = csv_header(path)
    pts: list[list] = []
    if header:
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                lines = (ln for ln in f if slug in ln)
                for rec in csv.reader(lines):
                    row = {h: (rec[i] if i < len(rec) else "") for i, h in enumerate(header)}
                    if row.get("slug") != slug:
                        continue
                    rs = (row.get("side") or "").upper()
                    if rs == side:
                        v, kind = _f(row.get("bid")), "own"
                    elif rs in ("UP", "DOWN"):
                        a = _f(row.get("ask"))
                        v, kind = (None if a is None else 1.0 - a), "inv"
                    else:
                        continue
                    if v is None or not (0.0 < v <= 1.0):
                        continue
                    try:
                        t = datetime.fromisoformat(row["time"]).timestamp()
                    except (ValueError, KeyError):
                        continue
                    if pts and abs(pts[-1][0] - t) < 0.5:
                        continue
                    pts.append([round(t, 1), round(v, 4), kind])
        except FileNotFoundError:
            pass
    pts.sort(key=lambda p: p[0])
    return {"slug": slug, "side": side, "start_ts": start_ts, "end_ts": end_ts, "points": pts[-800:], "source": "decisions.csv"}


@app.get("/api/log")
def api_log(lines: int = 150):
    lines = max(10, min(lines, 2000))
    env = read_env()
    return {"lines": [redact(ln, env) for ln in tail_lines(data_dir() / "bot.log", lines)]}


@app.get("/api/config")
def api_config():
    return config_view()


@app.post("/api/config")
async def api_config_save(request: Request):
    body = await _body(request)
    values = body.get("values") or {}
    if not isinstance(values, dict) or not values:
        raise HTTPException(400, "No values given.")
    clean = validate_updates(values)
    cur = mode_info()
    if "DRY_RUN" in clean and clean["DRY_RUN"] == "false" and cur["dry_run"]:
        if (body.get("live_confirmation") or "").strip() != UI_LIVE_PHRASE:
            raise HTTPException(403, f"Switching to LIVE needs the typed confirmation '{UI_LIVE_PHRASE}'.")
        if not read_env().get("POLYMARKET_PRIVATE_KEY"):
            raise HTTPException(409, "No private key is set. Add your wallet first (Settings → Wallet).")
        if not cur["live_armed"]:
            if body.get("live_ack") is not True:
                raise HTTPException(403, "Tick 'I understand this uses real money' to arm live trading.")
            clean["LIVE_TRADING_CONFIRM"] = LIVE_CONFIRM_PHRASE   # the sanctioned, typed-confirmation path
    env = read_env()
    changed = {k: v for k, v in clean.items() if env.get(k) != v}
    changed_public = sorted(k for k in changed if k not in SECRET_KEYS)
    if changed:
        write_env_values(changed)
    restarted = False
    if body.get("restart") and changed:
        running = bool(find_bot_processes())
        if running:
            if mode_info()["mode"] == "LIVE" and not body.get("confirm_live"):
                raise HTTPException(403, "Saved, but restarting in LIVE mode needs confirm_live=true.")
            _run_op("restart", lambda: (stop_bot_blocking(), start_bot()))
            restarted = True
    return {"ok": True, "changed": changed_public, "restarting": restarted, "config": config_view()}


def _live_guard(body: dict) -> None:
    if mode_info()["mode"] == "LIVE" and not body.get("confirm_live"):
        raise HTTPException(403, "The bot is configured for LIVE trading. Confirm to start with real money.")


@app.post("/api/bot/start")
async def api_start(request: Request):
    body = await _body(request)
    _live_guard(body)
    if _op_state["op"]:
        raise HTTPException(409, f"Busy: {_op_state['op']} in progress.")
    return await run_in_threadpool(start_bot)


@app.post("/api/bot/stop")
async def api_stop(request: Request):
    await _body(request)
    if not find_bot_processes():
        return {"ok": True, "was_running": False}
    _run_op("stop", stop_bot_blocking)
    return {"ok": True, "stopping": True, "grace_seconds": STOP_GRACE_SECONDS}


@app.post("/api/bot/kill")
async def api_kill(request: Request):
    await _body(request)
    return await run_in_threadpool(force_kill)


@app.post("/api/bot/restart")
async def api_restart(request: Request):
    body = await _body(request)
    _live_guard(body)
    if kill_file_present():
        raise HTTPException(409, "A KILL file is present. Use 'Clear halt' first.")
    _run_op("restart", lambda: (stop_bot_blocking(), start_bot()))
    return {"ok": True, "restarting": True}


@app.post("/api/bot/halt")
async def api_halt(request: Request):
    body = await _body(request)
    if (body.get("confirmation") or "").strip().upper() != "HALT":
        raise HTTPException(403, "Type HALT to confirm.")
    p = kill_file_paths()[0]
    p.write_text(f"created by dashboard {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
    return {"ok": True, "kill_file": p.name}


@app.post("/api/bot/clear-halt")
async def api_clear_halt(request: Request):
    body = await _body(request)
    if (body.get("confirmation") or "").strip().upper() != "RESUME":
        raise HTTPException(403, "Type RESUME to confirm.")
    if find_bot_processes():
        raise HTTPException(409, "Stop the bot before clearing a halt.")
    removed = []
    for p in kill_file_paths():
        if p.exists():
            p.unlink()
            removed.append(p.name)
    sp = data_dir() / "state.json"
    st = read_json(sp)
    cleared = None
    if st and st.get("halted"):
        cleared = st.get("halt_reason", "")
        st["halted"], st["halt_reason"] = False, ""
        if cleared.startswith("max drawdown"):
            st["peak_bankroll_usd"] = st.get("bankroll_usd", st.get("peak_bankroll_usd"))
        tmp = sp.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        os.replace(tmp, sp)
    return {"ok": True, "removed_kill_files": removed, "cleared_halt": cleared}


# -----------------------------------------------------------------------------
# Manual close ("Close" button on a position). File-based command: the dashboard only writes a request;
# the running bot picks it up on its next loop, sells through its normal market-exit path and writes the
# outcome to close_results.json. The dashboard never places orders itself.
# -----------------------------------------------------------------------------
CLOSE_TTL_SECONDS = 120          # keep in sync with bot.py MANUAL_CLOSE_TTL_SECONDS (default)
_close_lock = threading.Lock()


def close_requests_path() -> Path:
    return data_dir() / "close_requests.json"


def close_results_path() -> Path:
    return data_dir() / "close_results.json"


def _read_close_requests() -> list[dict]:
    raw = read_json(close_requests_path())
    if isinstance(raw, dict):
        raw = raw.get("requests", [])
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _write_json_atomic(path: Path, obj: Any) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: the bot may have the file open for a moment
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise HTTPException(503, "Could not write the close request (file busy). Try again.")


def queue_close_request(req_target: dict) -> tuple[dict, bool]:
    """Append a close request (deduped against a fresh identical one). Returns (request, is_new)."""
    import uuid
    now = time.time()
    with _close_lock:
        reqs = [r for r in _read_close_requests() if now - float(r.get("requested_at") or 0) <= CLOSE_TTL_SECONDS]
        sig = lambda r: (bool(r.get("all")), r.get("key") or "", r.get("token_id") or "", r.get("slug") or "")  # noqa: E731
        for r in reqs:
            if sig(r) == sig(req_target):
                return r, False
        req = {"id": uuid.uuid4().hex[:16], **req_target, "requested_at": now, "source": "dashboard"}
        reqs.append(req)
        _write_json_atomic(close_requests_path(), reqs)
        return req, True


@app.post("/api/positions/close", status_code=202)
async def api_close_position(request: Request):
    body = await _body(request)
    st = read_json(data_dir() / "state.json") or {}
    positions = st.get("positions") or {}
    if body.get("all") is True:
        target = {"all": True, "slug": "", "key": "", "token_id": ""}
        matches = list(positions.items())
    else:
        target = {"all": False}
        for k in ("slug", "key", "token_id"):
            v = body.get(k)
            if v is not None and not isinstance(v, (str, int)):
                raise HTTPException(400, f"{k} must be a string")
            v = str(v).strip() if v is not None else ""
            if len(v) > 200:
                raise HTTPException(400, f"{k} is too long")
            target[k] = v
        if not (target["slug"] or target["key"] or target["token_id"]):
            raise HTTPException(400, "Give slug, key or token_id (or all: true).")
        if target["key"] or target["token_id"]:
            matches = [(k, p) for k, p in positions.items() if isinstance(p, dict) and (
                (target["key"] and k == target["key"]) or (target["token_id"] and str(p.get("token_id")) == target["token_id"]))]
        else:
            matches = [(k, p) for k, p in positions.items() if isinstance(p, dict) and p.get("slug") == target["slug"]]
    if not matches:
        raise HTTPException(404, "No matching open position (it may have just closed).")
    if not find_bot_processes():
        raise HTTPException(409, "The bot isn't running, so it can't sell. Start it first (positions are unchanged).")
    req, new = queue_close_request(target)
    return {"ok": True, "queued": new, "request": req, "positions": [k for k, _ in matches],
            "expires_in": CLOSE_TTL_SECONDS}


@app.get("/api/positions/close")
def api_close_status():
    now = time.time()
    pending = [r for r in _read_close_requests() if now - float(r.get("requested_at") or 0) <= CLOSE_TTL_SECONDS]
    res = read_json(close_results_path())
    results = res.get("results", []) if isinstance(res, dict) else []
    return {"pending": pending, "results": [r for r in results if isinstance(r, dict)][-20:], "server_time": now}


# -----------------------------------------------------------------------------
# First-run wallet setup + wallet management (localhost only; key only ever goes to .env)
# -----------------------------------------------------------------------------
def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Wallet setup is only allowed from this computer (127.0.0.1).")


def _clean_key(raw: Any) -> str:
    k = str(raw or "").strip()
    if not KEY_RE.match(k):
        raise HTTPException(422, "Private key format is wrong: expected 64 hex characters, optionally starting with 0x.")
    return k if k.lower().startswith("0x") else "0x" + k


def _clean_addr(raw: Any) -> str:
    a = str(raw or "").strip()
    if not ADDR_RE.match(a):
        raise HTTPException(422, "Wallet address format is wrong: expected 0x followed by 40 hex characters.")
    return a


def _live_arm_check(body: dict) -> None:
    if (body.get("live_confirmation") or "").strip() != UI_LIVE_PHRASE or body.get("live_ack") is not True:
        raise HTTPException(403, f"Live mode needs '{UI_LIVE_PHRASE}' typed and the real-money box ticked.")


@app.get("/api/setup")
def api_setup_get():
    ex = example_defaults()
    starter = {}
    for k in STARTER_KEYS:
        spec = EDITABLE[k]
        starter[k] = {**spec, "value": _coerce(ex.get(k), spec["default"])}
    return {**wallet_info(), **mode_info(), "starter": starter, "ui_live_phrase": UI_LIVE_PHRASE,
            "example_exists": ENV_EXAMPLE.exists()}


@app.post("/api/setup")
async def api_setup_post(request: Request):
    _local_only(request)
    body = await _body(request)
    mode = (body.get("mode") or "paper").lower()
    skip_wallet = bool(body.get("skip_wallet"))
    updates: dict[str, str] = {}
    if skip_wallet:
        if mode != "paper":
            raise HTTPException(422, "Live mode needs a private key and wallet address.")
    else:
        updates["POLYMARKET_PRIVATE_KEY"] = _clean_key(body.get("private_key"))
        updates["POLYMARKET_WALLET_ADDRESS"] = _clean_addr(body.get("wallet_address"))
    settings = body.get("settings") or {}
    if not isinstance(settings, dict):
        raise HTTPException(422, "settings must be an object")
    settings = {k: v for k, v in settings.items() if k in STARTER_KEYS}
    if settings:
        updates.update(validate_updates(settings))
    if mode == "live":
        _live_arm_check(body)
        updates["DRY_RUN"] = "false"
        updates["LIVE_TRADING_CONFIRM"] = LIVE_CONFIRM_PHRASE
    elif mode == "paper":
        updates["DRY_RUN"] = "true"
        updates["LIVE_TRADING_CONFIRM"] = ""
    else:
        raise HTTPException(422, "mode must be 'paper' or 'live'")
    running = bool(find_bot_processes())
    write_env_values(updates)
    started, start_error = False, None
    if body.get("start"):
        try:
            if running:
                await run_in_threadpool(lambda: (stop_bot_blocking(), start_bot()))
            else:
                await run_in_threadpool(start_bot)
            started = True
        except HTTPException as e:
            start_error = redact(str(e.detail))
    return {"ok": True, "started": started, "start_error": start_error, **wallet_info(), **mode_info()}


@app.get("/api/wallet")
def api_wallet():
    return {**wallet_info(), **mode_info()}


@app.post("/api/wallet/replace")
async def api_wallet_replace(request: Request):
    _local_only(request)
    body = await _body(request)
    updates = {"POLYMARKET_PRIVATE_KEY": _clean_key(body.get("private_key"))}
    if body.get("wallet_address"):
        updates["POLYMARKET_WALLET_ADDRESS"] = _clean_addr(body.get("wallet_address"))
    write_env_values(updates)
    return {"ok": True, "restart_needed": bool(find_bot_processes()), **wallet_info()}


@app.post("/api/wallet/remove")
async def api_wallet_remove(request: Request):
    _local_only(request)
    body = await _body(request)
    if (body.get("confirmation") or "").strip().upper() != "REMOVE":
        raise HTTPException(403, "Type REMOVE to confirm.")
    if find_bot_processes():
        raise HTTPException(409, "Stop the bot before removing the key.")
    write_env_values({"POLYMARKET_PRIVATE_KEY": "", "DRY_RUN": "true", "LIVE_TRADING_CONFIRM": ""})
    return {"ok": True, **wallet_info(), **mode_info()}


# -----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Local dashboard for the Polymarket BTC bot")
    ap.add_argument("--host", default=os.getenv("DASHBOARD_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8787")))
    ap.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not dashboard_token():
        raise SystemExit("Refusing to bind to a non-local address without DASHBOARD_TOKEN set.")
    for h in ("127.0.0.1", "localhost", args.host):
        _ALLOWED_HOSTS.add(f"{h}:{args.port}".lower())
    for h in ("polybot", "polybot.taild65c41.ts.net", "100.75.180.78"):   # Tailscale (tailnet-only)
        _ALLOWED_HOSTS.update({h, f"{h}:{args.port}"})
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        _ALLOWED_HOSTS.clear()     # remote use: token is the guard
    url = f"http://127.0.0.1:{args.port}/" if args.host in ("127.0.0.1", "0.0.0.0", "localhost") else f"http://{args.host}:{args.port}/"
    print(f"Dashboard: {url}  (bot folder: {BOT_DIR}, data: {data_dir()})")
    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
