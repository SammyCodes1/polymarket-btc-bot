"""Offline bookkeeping tests for bot.py (no network, no orders). Run:  python tests/test_bookkeeping.py
(also works under pytest). Replays the 2026-10-17 21:00 live trade that falsely tripped the drawdown halt."""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="pmbot-test-")
os.environ.update({"DRY_RUN": "false", "LIVE_TRADING_CONFIRM": "I_UNDERSTAND_THIS_USES_REAL_MONEY",
                   "DATA_DIR": _TMP, "MAX_DRAWDOWN_PCT": "0.10", "DAILY_LOSS_LIMIT_PCT": "0.05",
                   "COOLDOWN_AFTER_LOSS_SECONDS": "0", "AUTO_REDEEM": "true"})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

assert bot.CFG.live, "tests must exercise the live bookkeeping path"

SLUG, TOKEN, COND = "btc-updown-15m-1792267200", "1019691395", "0xcond"
START, END = 1792267200.0, 1792268100.0
GASLESS_ERR = "Gasless transactions require a Builder API Key or Relayer API Key. Pass api_key= when constructing the client."


class Clock:
    t = START + 26


def _now():
    return Clock.t


bot.now_ts = _now


class FakeClient:
    """Stands in for polymarket.SecureClient. Never touches the network."""
    def __init__(self):
        self.token_bal = {}
        self.redeem_calls = 0
        self.redeem_ok = False

    def place_market_order(self, **kw):
        assert kw["side"] == "BUY"
        return SimpleNamespace(ok=True, making_amount="4.71", taking_amount="6.72857", order_id="0xabc", status="matched")

    def redeem_positions(self, condition_id):
        self.redeem_calls += 1
        if not self.redeem_ok:
            raise RuntimeError(GASLESS_ERR)
        return SimpleNamespace(wait=lambda: None)


def make_ex(gasless_key=False):
    ex = bot.Executor.__new__(bot.Executor)
    ex.client = FakeClient()
    ex.has_gasless_key = gasless_key
    ex.token_balance = lambda tid: ex.client.token_bal.get(tid)
    return ex


def fresh(cash=5.28, gasless_key=False):
    for f in Path(_TMP).glob("*"):
        f.unlink()
    Clock.t = START + 26
    st = bot.State(bankroll_usd=cash, peak_bankroll_usd=cash, day=bot.datetime.now(bot.timezone.utc).date().isoformat(),
                   day_start_bankroll_usd=cash, cash_usd=cash)
    risk = bot.Risk(st)
    ex = make_ex(gasless_key)
    pm = bot.PositionManager(st, risk, ex)
    pm.refresh_bankroll(cash)
    return st, risk, ex, pm


def market():
    return bot.BtcMarket(SLUG, "15m", COND, "q", START, END, TOKEN, "down", 0.01, 5, 0.07, True, "v2")


def open_via_bot(pm, ex):
    """Real code path: Executor.buy (live branch, fake client) + PositionManager.open."""
    m = market()
    sig = SimpleNamespace(market=m, token_id=TOKEN, side="UP", ask=0.70, p_side=0.745, edge=0.011)
    pm.open(sig, 4.71)
    ex.client.token_bal[TOKEN] = 6.72857
    return pm.s.positions[SLUG]


def tick(pm, risk, cash, dt=5):
    """One main-loop iteration's bookkeeping: cash sync -> consistent bankroll -> kill switches."""
    Clock.t += dt
    trusted = pm.refresh_bankroll(cash)
    halt = risk.check_kill_switches(bank_trusted=trusted)
    if halt and not pm.s.halted:
        pm.s.halted, pm.s.halt_reason = True, halt
    return trusted


def approx(a, b, tol=0.02):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------------------------------------
def test_buy_records_taker_fee():
    st, risk, ex, pm = fresh()
    pos = open_via_bot(pm, ex)
    fee = 6.72857 * 0.07 * 0.70 * 0.30
    assert approx(pos.cost_usd, 4.71 + fee, 0.001), pos.cost_usd           # 4.8089, matches the live debit
    rows = (Path(_TMP) / "trades.csv").read_text().splitlines()
    assert "0.0989" in rows[-1], rows[-1]


def test_parent_sequence_win_redeem_fails_then_redeemed():
    """Exact sequence from the task: cash 5.28, open 6.72857 @0.70, cash sync 0.57, resolve WIN while the
    redeem fails -> no halt, bankroll ~7.30; then redeem lands (cash 7.30) -> pending clears, bankroll stable."""
    st, risk, ex, pm = fresh(gasless_key=True)          # key set, but relayer rejects -> redeem fails
    pos = open_via_bot(pm, ex)
    pos.cost_usd = 4.71                                  # the task's figures (fee-free) to match ~7.30
    assert tick(pm, risk, 0.57)
    assert approx(st.bankroll_usd, 5.28) and not st.halted
    Clock.t = END + 30                                   # window over, tokens still on-chain
    assert tick(pm, risk, 0.57, dt=0) and approx(st.bankroll_usd, 5.28)
    pm.resolve(pos, 1.0, COND)                           # Gamma says UP won; auto-redeem raises
    assert ex.client.redeem_calls == 1
    assert SLUG in st.pending_redeem and not st.positions
    assert approx(st.bankroll_usd, 7.30), st.bankroll_usd     # 0.57 + 6.73, NOT 11.91
    for _ in range(3):
        assert tick(pm, risk, 0.57)
        assert approx(st.bankroll_usd, 7.30) and not st.halted, (st.bankroll_usd, st.halt_reason)
    assert approx(st.peak_bankroll_usd, 7.30)
    assert approx(st.realized_pnl_day, 2.02)
    # redeem lands: cash 7.30, tokens burned
    ex.client.token_bal[TOKEN] = 0.0
    assert tick(pm, risk, 7.30)
    assert not st.pending_redeem, st.pending_redeem
    assert approx(st.bankroll_usd, 7.30) and approx(st.peak_bankroll_usd, 7.30) and not st.halted
    for _ in range(3):
        tick(pm, risk, 7.30)
        assert approx(st.bankroll_usd, 7.30) and not st.halted


def test_real_live_sequence_external_auto_redeem():
    """What actually happened (Polymarket data-api activity): BUY 21:00:29 for $4.8089 incl fee, Polymarket
    auto-REDEEMed at 21:16:09 while our position was still open (Gamma 'closed' lagged until ~21:19:25).
    Old code: bankroll = cash 7.20 + cost 4.71 = 11.91 -> peak 11.91 -> halt at 7.20. New code: never > 7.20."""
    st, risk, ex, pm = fresh()
    pos = open_via_bot(pm, ex)
    cash_after_buy = 5.280043 - 4.808899              # 0.4711
    tick(pm, risk, cash_after_buy)
    assert approx(st.bankroll_usd, 5.28, 0.01)
    Clock.t = END + 1
    tick(pm, risk, cash_after_buy, dt=0)               # first tick past the end records cash_at_end
    # 21:16:09 external redeem: tokens gone, cash 7.1997
    ex.client.token_bal[TOKEN] = 0.0
    max_bank = 0.0
    for _ in range(40):                                # ~3.3 minutes of ticks before Gamma flips 'closed'
        tick(pm, risk, 7.199714)
        max_bank = max(max_bank, st.bankroll_usd, st.peak_bankroll_usd)
        assert not st.halted, st.halt_reason
    assert max_bank < 7.21, max_bank
    pm.resolve(pos, 1.0, COND)                         # resolves after it was already redeemed
    assert not st.pending_redeem and not st.positions
    for _ in range(5):
        tick(pm, risk, 7.199714)
        assert not st.halted and approx(st.bankroll_usd, 7.20, 0.01)
    assert approx(st.peak_bankroll_usd, 7.20, 0.01)
    assert approx(st.realized_pnl_day, 1.92, 0.01), st.realized_pnl_day   # 6.7286 - 4.8089 (fee counted)


def test_external_redeem_after_resolution_unknown_token_balance():
    """Token balance read failing (None): the cash jump of ~payout still clears the pending entry."""
    st, risk, ex, pm = fresh()
    pos = open_via_bot(pm, ex)
    tick(pm, risk, 0.4711)
    Clock.t = END + 30
    tick(pm, risk, 0.4711, dt=0)
    pm.resolve(pos, 1.0, COND)
    assert SLUG in st.pending_redeem and approx(st.bankroll_usd, 7.20, 0.01)
    ex.token_balance = lambda tid: None
    tick(pm, risk, 7.1997)
    assert not st.pending_redeem and approx(st.bankroll_usd, 7.20, 0.01) and not st.halted


def test_manual_redeem_notice_logged_once(caplog=None):
    st, risk, ex, pm = fresh(gasless_key=False)
    pos = open_via_bot(pm, ex)
    tick(pm, risk, 0.4711)
    Clock.t = END + 30
    msgs = []
    h = bot.logging.Handler()
    h.emit = lambda r: msgs.append(r.getMessage())
    bot.log.addHandler(h)
    try:
        pm.resolve(pos, 1.0, COND)
        for _ in range(10):
            tick(pm, risk, 0.4711)
    finally:
        bot.log.removeHandler(h)
    assert ex.client.redeem_calls == 0               # no key -> never calls the relayer
    assert sum("redeem them in the Polymarket UI" in m for m in msgs) == 1, msgs
    assert not any("auto-redeem failed" in m for m in msgs)
    assert approx(st.bankroll_usd, 7.20, 0.01) and not st.halted


def test_losing_resolution():
    st, risk, ex, pm = fresh()
    pos = open_via_bot(pm, ex)
    tick(pm, risk, 0.4711)
    Clock.t = END + 30
    tick(pm, risk, 0.4711, dt=0)
    assert pm.bank_trusted
    pm.resolve(pos, 0.0, COND)
    assert not st.pending_redeem and not st.positions
    assert approx(st.bankroll_usd, 0.47, 0.01)
    assert approx(st.realized_pnl_day, -4.81, 0.01)
    assert tick(pm, risk, 0.4711)                    # a real, event-matched drop is trusted ...
    assert st.halted and st.halt_reason.startswith("daily loss limit"), st.halt_reason   # ... and halts


def test_glitch_guard_skips_halt_then_accepts():
    st, risk, ex, pm = fresh(cash=7.20)
    assert tick(pm, risk, 7.20)
    assert not tick(pm, risk, 0.10)                  # cash read glitch, no trade event
    assert not st.halted and approx(st.peak_bankroll_usd, 7.20)
    assert tick(pm, risk, 7.20)                      # back to normal
    # an upward glitch never sets a peak
    assert not tick(pm, risk, 14.0)
    assert approx(st.peak_bankroll_usd, 7.20)
    assert tick(pm, risk, 7.20)
    # a persistent drop is accepted after the window and then halts
    for _ in range(20):
        tick(pm, risk, 5.0)
        if st.halted:
            break
    assert st.halted and st.halt_reason.startswith("max drawdown hit"), st.halt_reason


def test_open_with_lagging_cash_sync_does_not_inflate_peak():
    st, risk, ex, pm = fresh()
    open_via_bot(pm, ex)
    tick(pm, risk, 5.28)                             # CLOB cash not debited yet: would be 5.28 + 4.81
    assert approx(st.peak_bankroll_usd, 5.28, 0.01), st.peak_bankroll_usd
    tick(pm, risk, 0.4711)
    assert approx(st.bankroll_usd, 5.28, 0.01) and approx(st.peak_bankroll_usd, 5.28, 0.01)


def _write_state(d):
    (Path(_TMP) / "state.json").write_text(json.dumps(d))


def test_migration_clears_stale_drawdown_halt():
    for f in Path(_TMP).glob("*"):
        f.unlink()
    pc = json.loads((Path(__file__).resolve().parents[1] / "data" / "pc_sample" / "state.json").read_text()) \
        if (Path(__file__).resolve().parents[1] / "data" / "pc_sample" / "state.json").exists() else None
    # PC state after the false halt: position closed, peak 11.91, halted
    d = {"bankroll_usd": 7.199714, "peak_bankroll_usd": 11.909713, "day": "2026-10-17",
         "day_start_bankroll_usd": 5.280043, "realized_pnl_day": 2.018571, "realized_pnl_total": 2.018571,
         "trades_today": 1, "consecutive_losses": 0, "last_loss_at": 0.0, "halted": True,
         "halt_reason": "max drawdown hit (bankroll 7.20 vs peak 11.91)", "traded_slugs": [SLUG], "positions": {}}
    _write_state(d)
    st = bot.State.load(7.199714)
    pm = bot.PositionManager(st, bot.Risk(st), make_ex())
    pm.refresh_bankroll(7.199714)
    bot.startup_migrations(st)
    assert not st.halted and st.halt_reason == ""
    assert approx(st.peak_bankroll_usd, 7.20, 0.001) and approx(st.bankroll_usd, 7.20, 0.001)
    # the earlier PC snapshot (position still open past its end, tokens already redeemed) also reads 7.20
    if pc:
        _write_state(pc)
        Clock.t = END + 300
        st2 = bot.State.load(7.199714)
        ex2 = make_ex()
        ex2.client.token_bal[pc["positions"][SLUG]["token_id"]] = 0.0
        pm2 = bot.PositionManager(st2, bot.Risk(st2), ex2)
        pm2.refresh_bankroll(7.199714)
        assert approx(st2.bankroll_usd, 7.20, 0.001), st2.bankroll_usd


def test_migration_keeps_real_drawdown_halt():
    d = {"bankroll_usd": 4.0, "peak_bankroll_usd": 5.28, "day": "2026-10-17", "day_start_bankroll_usd": 5.28,
         "halted": True, "halt_reason": "max drawdown hit (bankroll 4.00 vs peak 5.28)", "positions": {}}
    _write_state(d)
    st = bot.State.load(4.0)
    pm = bot.PositionManager(st, bot.Risk(st), make_ex())
    pm.refresh_bankroll(4.0)
    bot.startup_migrations(st)
    assert st.halted
    bot.startup_migrations(st, reset_halt=True)      # --reset-halt
    assert not st.halted and st.halt_reason == "" and approx(st.peak_bankroll_usd, 4.0)


if __name__ == "__main__":
    bot.logging.basicConfig(level="WARNING", format="  log %(levelname)s %(message)s")
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
