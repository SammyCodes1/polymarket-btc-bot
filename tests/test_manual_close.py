"""Offline tests for the dashboard "Close" button (no network, no real orders). Run:
    python tests/test_manual_close.py          (also works under pytest)
Covers the request-file handling (bot side and dashboard side) and manual-close bookkeeping in paper
mode and in live mode with a mocked order client."""
import asyncio
import csv
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-mc-"))
os.environ.setdefault("COOLDOWN_AFTER_LOSS_SECONDS", "0")
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

SLUG, TOKEN = "btc-updown-15m-1792267200", "tok-down-123"
NOW = 1792267200.0 + 300          # 5 minutes into the window
END = 1792267200.0 + 900


class Env:
    """Per-test temp data dir + paper/live mode + fake order book. Restores everything afterwards."""
    def __init__(self, live=False, bids=((0.84, 120.0),), asks=((0.85, 90.0),)):
        self.live, self.bids, self.asks = live, list(bids), list(asks)

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pmbot-mc-"))
        self.saved = {n: getattr(bot, n) for n in ("STATE_FILE", "TRADES_CSV", "CLOSE_REQUESTS", "CLOSE_RESULTS",
                                                   "get_book", "now_ts")}
        self.saved_cfg = (bot.CFG.dry_run, bot.CFG.live_confirm)
        bot.STATE_FILE, bot.TRADES_CSV = self.dir / "state.json", self.dir / "trades.csv"
        bot.CLOSE_REQUESTS, bot.CLOSE_RESULTS = self.dir / "close_requests.json", self.dir / "close_results.json"
        bot.now_ts = lambda: NOW
        bot.CFG.dry_run = not self.live
        bot.CFG.live_confirm = bot.LIVE_CONFIRM_PHRASE if self.live else ""
        assert bot.CFG.live == self.live

        def fake_book(token_id):
            if not self.bids or not self.asks:
                return None
            return bot.Book(self.bids[0][0], self.asks[0][0], 100.0, 100.0, list(self.bids), list(self.asks), token_id)
        bot.get_book = fake_book
        return self

    def __exit__(self, *a):
        for n, v in self.saved.items():
            setattr(bot, n, v)
        bot.CFG.dry_run, bot.CFG.live_confirm = self.saved_cfg


class FakeClient:
    """Stands in for polymarket.SecureClient. Records orders, never touches the network."""
    def __init__(self, fill_px=0.84):
        self.orders, self.fill_px = [], fill_px

    def place_market_order(self, **kw):
        self.orders.append(kw)
        assert kw["side"] == "SELL"
        sh = kw["shares"]
        return SimpleNamespace(ok=True, making_amount=str(sh), taking_amount=str(round(sh * self.fill_px, 6)),
                               order_id="0xsell", status="matched")


def setup(cash=490.0, entry=0.72, shares=13.8889, cost=10.20, live=False):
    st = bot.State(bankroll_usd=cash + cost, peak_bankroll_usd=cash + cost, day="2026-10-17",
                   day_start_bankroll_usd=cash + cost, cash_usd=cash, consecutive_losses=2, last_loss_at=111.0)
    st.positions[SLUG] = bot.Position(SLUG, "15m", "DOWN", TOKEN, shares, entry, cost, NOW - 200, END, END - 900, 0.07)
    st.traded_slugs.append(SLUG)
    ex = bot.Executor.__new__(bot.Executor)
    ex.client = FakeClient() if live else None
    ex.has_gasless_key = False
    ex.token_balance = lambda tid: shares if live else None
    risk = bot.Risk(st)
    pm = bot.PositionManager(st, risk, ex)
    pm.refresh_bankroll(cash if live else None, now=NOW)
    return st, risk, ex, pm


def write_requests(obj, raw=None):
    bot.CLOSE_REQUESTS.write_text(raw if raw is not None else json.dumps(obj), encoding="utf-8")


def req(rid="r1", age=3.0, **kw):
    return {"id": rid, "requested_at": NOW - age, **({"key": SLUG, "token_id": TOKEN, "slug": SLUG} if not kw else kw)}


def results():
    return json.loads(bot.CLOSE_RESULTS.read_text())["results"]


def sell_rows():
    if not bot.TRADES_CSV.exists():
        return []
    return [r for r in csv.DictReader(bot.TRADES_CSV.open()) if r["action"] == "SELL"]


class LogCap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def capture():
    h = LogCap()
    bot.log.addHandler(h)
    bot.log.setLevel(logging.INFO)
    return h


# ---------------------------------------------------------------- request-file parsing
def test_parse_close_requests():
    assert bot.parse_close_requests("{not json") is None
    assert bot.parse_close_requests('"x"') is None
    assert bot.parse_close_requests("") == []
    got = bot.parse_close_requests(json.dumps({"requests": [
        {"id": "a", "requested_at": 5, "slug": "s"},
        {"id": "b", "requested_at": "nope", "slug": "s"},     # bad timestamp -> dropped
        {"id": "c", "requested_at": 5},                       # no target -> dropped
        {"id": "d", "requested_at": 5, "all": "yes"},         # all must be literally true -> dropped
        {"id": "e", "requested_at": 5, "all": True},
        "junk", None, 7]}))
    assert [r["id"] for r in got] == ["a", "e"], got
    assert got[1]["all"] is True and got[0]["slug"] == "s" and got[0]["key"] == ""


def test_matching_rules():
    p = bot.Position(SLUG, "15m", "DOWN", TOKEN, 1, 0.7, 0.7, 0, END, 0, 0.07)
    m = bot.close_request_matches
    assert m({"all": True}, p)
    assert m({"key": SLUG}, p) and m({"key": "legacy:DOWN"}, p, "legacy:DOWN")
    assert m({"token_id": TOKEN}, p) and not m({"token_id": "other"}, p)
    assert m({"slug": SLUG}, p) and not m({"slug": "other"}, p)
    assert not m({"key": "other", "slug": SLUG}, p), "a key that doesn't match must not fall back to the slug"


# ---------------------------------------------------------------- paper manual close
def test_paper_manual_close_bookkeeping():
    with Env(live=False):
        st, risk, ex, pm = setup()
        cap = capture()
        write_requests([req()])
        assert pm.process_close_requests({}, now=NOW) == 1
        assert SLUG not in st.positions
        shares, bid = 13.8889, 0.84
        proceeds = shares * bid - 0.07 * shares * bid * (1 - bid)
        assert abs(st.cash_usd - (490.0 + proceeds)) < 1e-6
        pnl = proceeds - 10.20
        assert abs(st.realized_pnl_day - pnl) < 1e-6 and abs(st.realized_pnl_total - pnl) < 1e-6
        assert st.consecutive_losses == 2 and st.last_loss_at == 111.0, "manual close must not touch the loss streak"
        rows = sell_rows()
        assert len(rows) == 1 and rows[0]["reason"] == "manual_close" and rows[0]["mode"] == "PAPER"
        assert abs(float(rows[0]["pnl"]) - pnl) < 1e-3 and float(rows[0]["fee"]) > 0
        assert any(ln.startswith(f"CLOSED {SLUG} DOWN reason=manual_close") for ln in cap.lines), cap.lines
        r = results()[-1]
        assert r["id"] == "r1" and r["status"] == "closed" and abs(r["pnl"] - pnl) < 1e-3
        assert not bot.CLOSE_REQUESTS.exists() and not bot.CLOSE_REQUESTS.with_suffix(".processing").exists()
        assert SLUG in st.manual_closed_slugs
        saved = json.loads(bot.STATE_FILE.read_text())
        assert SLUG not in saved["positions"] and SLUG in saved["manual_closed_slugs"]
        bot.log.removeHandler(cap)


def test_no_reentry_after_manual_close():
    with Env(live=False):
        st, risk, ex, pm = setup()
        write_requests([req()])
        pm.process_close_requests({}, now=NOW)
        st.traded_slugs.clear()          # even if traded_slugs were trimmed, the manual block still holds
        m = bot.BtcMarket(SLUG, "15m", "c", "q", END - 900, END, "up", TOKEN, 0.01, 5, 0.07, True, "v2")
        sig = SimpleNamespace(market=m, side="DOWN", p_side=0.75, ask=0.72, book=SimpleNamespace(ask_depth_usd=1e6))
        usd, why = risk.size_for(sig)
        assert usd == 0 and "manually" in why, why
        # arbitrage also stays out of the window
        called = []
        bot.get_book = lambda t: called.append(t)
        pm.try_arbitrage(m)
        assert not called


def test_manual_loss_counts_toward_daily_limit_not_streak():
    with Env(live=False, bids=((0.30, 120.0),), asks=((0.31, 90.0),)):
        st, risk, ex, pm = setup(cash=0.0, cost=10.20)
        st.day_start_bankroll_usd = 10.20
        st.consecutive_losses, st.last_loss_at = 0, 0.0
        write_requests([req()])
        assert pm.process_close_requests({}, now=NOW) == 1
        assert st.realized_pnl_day < -5
        assert st.consecutive_losses == 0 and st.last_loss_at == 0.0
        halt = risk.check_kill_switches(bank_trusted=True)
        assert halt and halt.startswith("daily loss limit"), halt


def test_expired_and_not_open_requests():
    with Env(live=False):
        st, risk, ex, pm = setup()
        write_requests([req("old", age=bot.CFG.manual_close_ttl_seconds + 5),
                        {"id": "nope", "requested_at": NOW, "slug": "btc-updown-15m-1"}])
        assert pm.process_close_requests({}, now=NOW) == 0
        assert SLUG in st.positions and not sell_rows()
        by = {r["id"]: r["status"] for r in results()}
        assert by == {"old": "expired", "nope": "not_open"}, by


def test_malformed_file_does_not_crash():
    with Env(live=False):
        st, risk, ex, pm = setup()
        for raw in ("{garbage", "\ufeff[]", "[1,2,3]", '{"requests": "x"}', "\x00\x01"):
            write_requests(None, raw=raw)
            assert pm.process_close_requests({}, now=NOW) == 0
            assert SLUG in st.positions and not bot.CLOSE_REQUESTS.exists()
        # a BOM-prefixed but valid file (PowerShell Set-Content -Encoding UTF8) still works
        bot.CLOSE_REQUESTS.write_text("\ufeff" + json.dumps([req("bom")]), encoding="utf-8")
        assert pm.process_close_requests({}, now=NOW) == 1


def test_internal_error_is_contained():
    with Env(live=False):
        st, risk, ex, pm = setup()
        write_requests([req()])

        def boom(*a, **k):
            raise RuntimeError("boom")
        pm._manual_close_one = boom
        assert pm.process_close_requests({}, now=NOW) == 0     # logged, not raised


def test_idempotent_redelivery():
    with Env(live=False):
        st, risk, ex, pm = setup()
        write_requests([req("same")])
        assert pm.process_close_requests({}, now=NOW) == 1
        # simulate the dashboard re-writing an already-handled request plus a fresh one for a new position
        st.positions[SLUG] = bot.Position(SLUG, "15m", "DOWN", TOKEN, 5, 0.7, 3.5, NOW, END, END - 900, 0.07)
        write_requests([req("same")])
        assert pm.process_close_requests({}, now=NOW) == 0 and SLUG in st.positions
        assert [r["id"] for r in results()] == ["same"]


def test_window_ended_and_no_bids():
    with Env(live=False, bids=()):
        st, risk, ex, pm = setup()
        write_requests([req("nobid")])
        assert pm.process_close_requests({}, now=NOW) == 0
        assert SLUG in st.positions and results()[-1]["status"] == "failed"
        write_requests([req("late", age=-(END + 5 - NOW) + 1)])     # requested 1s before "now" = END+5
        assert pm.process_close_requests({}, now=END + 5) == 0
        assert SLUG in st.positions and results()[-1]["status"] == "window_ended"


def test_paper_fok_not_filled_reports_failure():
    with Env(live=False, bids=((0.84, 2.0),)):      # too thin for 13.9 shares within slippage
        st, risk, ex, pm = setup()
        write_requests([req("thin")])
        assert pm.process_close_requests({}, now=NOW) == 0
        r = results()[-1]
        assert r["status"] == "failed" and "not filled" in r["note"] and SLUG in st.positions
        assert sell_rows() and sell_rows()[-1]["ok"] == "False"


def test_live_manual_close_uses_existing_sell_path():
    with Env(live=True):
        st, risk, ex, pm = setup(live=True)
        write_requests([req("live")])
        assert pm.process_close_requests({}, now=NOW) == 1
        assert len(ex.client.orders) == 1
        o = ex.client.orders[0]
        assert o["side"] == "SELL" and o["order_type"] == "FAK" and o["asset_id"] == TOKEN
        assert abs(o["min_price"] - 0.82) < 1e-9, o          # best bid 0.84 - SLIPPAGE 0.02, same as stop-loss
        assert o["shares"] == 13.88                          # floored to 2dp by Executor.sell
        assert SLUG not in st.positions and sell_rows()[-1]["mode"] == "LIVE"
        assert st.consecutive_losses == 2 and results()[-1]["status"] == "closed"


def test_all_closes_everything():
    with Env(live=False):
        st, risk, ex, pm = setup()
        other = "btc-updown-15m-1792268100"
        st.positions[other] = bot.Position(other, "15m", "UP", "tok-up-2", 5, 0.7, 3.6, NOW, END + 900, END, 0.07)
        write_requests([{"id": "all", "requested_at": NOW, "all": True}])
        assert pm.process_close_requests({}, now=NOW) == 2
        assert not st.positions and results()[-1]["status"] == "closed"


# ---------------------------------------------------------------- dashboard side (server.py)
def _server():
    """Import dashboard/server.py against a temp bot dir (needs fastapi/psutil/uvicorn; skipped otherwise)."""
    try:
        import fastapi  # noqa: F401
        import psutil  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return None
    d = Path(tempfile.mkdtemp(prefix="pmdash-mc-"))
    os.environ["DASHBOARD_ENV_FILE"] = str(d / ".env")
    sys.path.insert(0, str(ROOT / "dashboard"))
    import server
    server.data_dir = lambda: d
    return server, d


class FakeRequest:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


def test_server_close_endpoint_writes_request_file():
    got = _server()
    if got is None:
        print("  (skipped: fastapi/psutil/uvicorn not installed)")
        return
    server, d = got
    from fastapi import HTTPException
    (d / "state.json").write_text(json.dumps({"positions": {SLUG: {"slug": SLUG, "side": "DOWN", "token_id": TOKEN}}}))
    call = lambda b: asyncio.run(server.api_close_position(FakeRequest(b)))  # noqa: E731

    def status_of(b):
        try:
            call(b)
        except HTTPException as e:
            return e.status_code
        return 202
    server.find_bot_processes = lambda: []
    assert status_of({"key": SLUG}) == 409                     # bot not running -> refuse (it couldn't sell)
    server.find_bot_processes = lambda: [object()]
    assert status_of({}) == 400 and status_of({"slug": {"x": 1}}) == 400
    assert status_of({"key": "btc-updown-15m-1"}) == 404
    r1 = call({"key": SLUG, "token_id": TOKEN, "slug": SLUG})
    assert r1["queued"] and r1["positions"] == [SLUG]
    r2 = call({"key": SLUG, "token_id": TOKEN, "slug": SLUG})
    assert not r2["queued"] and r2["request"]["id"] == r1["request"]["id"], "duplicate clicks are deduped"
    call({"all": True})
    reqs = json.loads((d / "close_requests.json").read_text())
    assert len(reqs) == 2 and reqs[0]["key"] == SLUG and reqs[1]["all"] is True
    assert not list(d.glob("*.tmp")), "temp file must be renamed into place"
    # the bot parses exactly what the server wrote
    parsed = bot.parse_close_requests((d / "close_requests.json").read_text())
    assert [p["id"] for p in parsed] == [r["id"] for r in reqs]
    # stale requests are pruned when a new one is written
    reqs[0]["requested_at"] = time.time() - 999
    (d / "close_requests.json").write_text(json.dumps(reqs))
    server.queue_close_request({"all": False, "slug": SLUG, "key": "", "token_id": ""})
    left = json.loads((d / "close_requests.json").read_text())
    assert [r.get("key") for r in left] == ["", ""] and left[0]["all"] is True
    st = server.api_close_status()
    assert len(st["pending"]) == 2 and st["results"] == []


if __name__ == "__main__":
    logging.basicConfig(level="WARNING", format="  log %(levelname)s %(message)s")
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
