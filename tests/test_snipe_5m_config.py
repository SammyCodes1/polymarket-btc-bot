"""Offline checks that the existing favorite settings cover the Oct 2026 paper ideas without new code:
  * late snipe: FAV_MIN_PRICE=0.95 FAV_MAX_PRICE=0.99 FAV_MIN_SECS_LEFT=60 FAV_MAX_SECS_LEFT=180,
    TAKE_PROFIT_MIN_BID=1.01 (hold to settlement), FAV_MIN_SIZE_ONLY with MAX_POSITION_USD=5.00
  * 5m favorite: FAV_DURATIONS=5m FAV_MIN_SECS_LEFT=200 FAV_MAX_SECS_LEFT=300, 82-92c
Run: python tests/test_snipe_5m_config.py   (also works under pytest)"""
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-sn-"))
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

SNIPE = replace(bot.CFG, fav_durations="15m,5m", fav_min_price=0.95, fav_max_price=0.99, fav_min_secs_left=60,
                fav_max_secs_left=180, fav_edge_est=0.03, max_spread=0.03, take_profit_min_bid=1.01,
                stop_loss_pct=0.0, fav_stop_drop_cents=0.0, fav_exit_seconds_left=0, force_exit_seconds=0,
                thesis_exit_prob=0.0, min_profit_usd=0.10, take_profit_pct=0.15, take_profit_capture=0.5,
                max_position_usd=5.00, max_position_pct=1.0, kelly_fraction=100.0, max_total_btc_exposure_pct=1.0,
                max_net_direction_pct=1.0, fav_min_size_only=True, fav_min_size_buffer=0.01)
FIVE = replace(bot.CFG, fav_durations="5m", fav_min_price=0.82, fav_max_price=0.92, fav_min_secs_left=200,
               fav_max_secs_left=300, fav_edge_est=0.03, max_spread=0.03)


def sig(cfg, duration, left, bid, ask):
    return bot.favorite_signal(cfg, duration=duration, secs_left=left, up_bid=bid, up_ask=ask,
                               down_bid=round(1 - ask, 4), down_ask=round(1 - bid, 4), fee_rate=0.07)


def test_snipe_enters_in_band_and_window():
    for d in ("15m", "5m"):
        assert sig(SNIPE, d, 120, 0.96, 0.97)[0] == "ENTER"
        assert sig(SNIPE, d, 120, 0.98, 0.99)[0] == "ENTER"        # ask 99c still allowed
    assert sig(SNIPE, "15m", 120, 0.99, 1.00)[0] == "skip:fav_price"   # no 100c asks
    assert sig(SNIPE, "15m", 120, 0.92, 0.93)[0] == "skip:fav_price"
    assert sig(SNIPE, "15m", 200, 0.96, 0.97)[0] == "skip:fav_time"
    assert sig(SNIPE, "15m", 60, 0.96, 0.97)[0] == "skip:fav_time"    # min is exclusive


def test_snipe_holds_to_settlement():
    sh = 5.05
    assert bot.exit_reason(SNIPE, entry=0.97, bid=0.999, shares=sh, cost_usd=4.90, fee_rate=0.07,
                           secs_left=30, p_side=0.999) is None
    assert bot.exit_reason(SNIPE, entry=0.97, bid=0.20, shares=sh, cost_usd=4.90, fee_rate=0.07,
                           secs_left=30, p_side=0.2) is None


def test_snipe_min_size_stake_fits_cap():
    for ask, want in ((0.95, 4.80), (0.97, 4.90), (0.99, 5.00)):
        usd, why = bot.size_usd(SNIPE, bank=50.0, p_side=0.995, price=ask, total_exposure=0.0,
                                same_dir_exposure=0.0, min_order_usd=5 * ask)
        assert why == "ok" and usd == want and usd / ask >= 5, (ask, usd)


def test_five_minute_favorite():
    assert sig(FIVE, "5m", 250, 0.85, 0.86)[0] == "ENTER"
    assert sig(FIVE, "15m", 250, 0.85, 0.86)[0] == "skip:fav_duration"
    assert sig(FIVE, "5m", 180, 0.85, 0.86)[0] == "skip:fav_time"
    assert sig(FIVE, "5m", 250, 0.92, 0.93)[0] == "skip:fav_price"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
