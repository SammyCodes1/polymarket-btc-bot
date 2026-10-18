"""Offline tests for SPOT_EXIT_ENABLED / SPOT_EXIT_CROSS_USD / SPOT_EXIT_SECS_LEFT (no network). Run:
    python tests/test_spot_exit.py          (also works under pytest)"""
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-se-"))
os.environ.setdefault("COOLDOWN_AFTER_LOSS_SECONDS", "0")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import bot  # noqa: E402
import test_manual_close as mc  # noqa: E402  (reuses its paper Env/setup helpers)

ON = replace(bot.CFG, spot_exit_enabled=True, spot_exit_cross_usd=50.0, spot_exit_secs_left=600, spot_exit_max_age=20.0)


def r(cfg=ON, side="UP", spot=99_940.0, ptb=100_000.0, left=300, age=1.0):
    return bot.spot_exit_reason(cfg, side=side, spot=spot, ptb=ptb, secs_left=left, snap_age=age)


def test_default_off():
    assert bot.Config().spot_exit_enabled is False or os.getenv("SPOT_EXIT_ENABLED")
    assert r(cfg=replace(ON, spot_exit_enabled=False)) is None


def test_up_and_down_directions():
    assert r(side="UP", spot=99_940)[0] == "spot_exit"           # $60 below strike, holding UP
    assert r(side="UP", spot=99_960) is None                       # only $40 against
    assert r(side="UP", spot=100_200) is None                      # in our favour
    assert r(side="DOWN", spot=100_050)[0] == "spot_exit"          # exactly $50 against DOWN
    assert r(side="DOWN", spot=99_900) is None


def test_time_window_and_staleness():
    assert r(left=601) is None and r(left=600)[0] == "spot_exit"
    assert r(left=0) is None
    assert r(age=25) is None                                       # stale spot never triggers
    assert r(spot=None) is None and r(ptb=None) is None


def _market(slug):
    return bot.BtcMarket(slug, "15m", "0xc", "q", mc.END - 900, mc.END, "tok-up", mc.TOKEN, 0.01, 5, 0.07, True, "v2")


def _snap(spot):
    return bot.PriceSnapshot(spot=spot, closes_1m=[spot], candles=[], sigma_1m=0.0005, source="bitstamp",
                             fetched_at=mc.NOW - 1)


def _run(spot, enabled=True):
    saved = (bot.CFG.spot_exit_enabled, bot.CFG.strategy, bot.CFG.stop_loss_pct, bot.CFG.take_profit_min_bid)
    bot.CFG.spot_exit_enabled, bot.CFG.strategy = enabled, "favorite"
    bot.CFG.stop_loss_pct, bot.CFG.take_profit_min_bid = 0.0, 0.99
    bot._PTB_CACHE[mc.SLUG] = 100_000.0
    try:
        with mc.Env(bids=((0.30, 200.0),), asks=((0.32, 200.0),)):
            st, risk, ex, pm = mc.setup(entry=0.85, shares=5.6, cost=4.80)     # holding DOWN (see mc.setup)
            pm.manage({mc.SLUG: _market(mc.SLUG)}, _snap(spot), bot.NewsState())
            return st, mc.sell_rows()
    finally:
        bot.CFG.spot_exit_enabled, bot.CFG.strategy, bot.CFG.stop_loss_pct, bot.CFG.take_profit_min_bid = saved
        bot._PTB_CACHE.pop(mc.SLUG, None)


def test_manage_sells_on_cross_paper():
    # mc.NOW is 600s before END -> inside SPOT_EXIT_SECS_LEFT=600. DOWN position, BTC $80 above strike.
    st, sells = _run(100_080.0)
    assert mc.SLUG not in st.positions and len(sells) == 1 and "spot_exit" in sells[0]["reason"], sells


def test_manage_holds_when_not_crossed_or_disabled():
    st, sells = _run(100_020.0)
    assert mc.SLUG in st.positions and not sells
    st, sells = _run(100_080.0, enabled=False)
    assert mc.SLUG in st.positions and not sells


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
