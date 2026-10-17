"""Offline tests for FAV_STOP_DROP_CENTS (hard cents stop) and FAV_EXIT_SECONDS_LEFT (time exit). Run:
    python tests/test_exits.py          (also works under pytest)"""
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-ex-"))
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

BASE = replace(bot.CFG, stop_loss_pct=0.60, take_profit_min_bid=0.97, force_exit_seconds=0, thesis_exit_prob=0.0,
               fav_stop_drop_cents=0.0, fav_exit_seconds_left=0)


def ex(cfg, bid, secs_left=400, entry=0.72):
    shares = 4.80 / entry
    return bot.exit_reason(cfg, entry=entry, bid=bid, shares=shares, cost_usd=4.80, fee_rate=0.07,
                           secs_left=secs_left, p_side=bid)


def sig(cfg, secs_left, up_bid=0.87, up_ask=0.88):
    return bot.favorite_signal(cfg, duration="15m", secs_left=secs_left, up_bid=up_bid, up_ask=up_ask,
                               down_bid=1 - up_ask, down_ask=1 - up_bid, fee_rate=0.07)


def test_defaults_are_off():
    assert bot.Config().fav_stop_drop_cents == 0.0 or os.getenv("FAV_STOP_DROP_CENTS")
    assert bot.Config().fav_exit_seconds_left == 0 or os.getenv("FAV_EXIT_SECONDS_LEFT")
    assert ex(BASE, 0.55) is None                     # only the 60% stop (~0.288) applies
    assert ex(BASE, 0.28)[0] == "stop_loss"
    assert ex(BASE, 0.50, secs_left=30) is None       # holds to resolution


def test_hard_stop_triggers_before_pct_stop():
    cfg = replace(BASE, fav_stop_drop_cents=15)
    assert ex(cfg, 0.58) is None                      # 14c down
    k, d = ex(cfg, 0.57)                              # exactly 15c down
    assert k == "hard_stop" and "0.570" in d
    assert ex(cfg, 0.40)[0] == "hard_stop"            # far below: hard stop reported first
    assert ex(cfg, 0.97)[0] == "take_profit"          # TP still wins


def test_hard_stop_floating_point_edge():
    cfg = replace(BASE, fav_stop_drop_cents=10)
    assert ex(cfg, 0.62, entry=0.72)[0] == "hard_stop"


def test_time_exit_closes_and_respects_tp():
    cfg = replace(BASE, fav_exit_seconds_left=180)
    assert ex(cfg, 0.80, secs_left=181) is None
    k, d = ex(cfg, 0.80, secs_left=180)
    assert k == "time_exit" and "180" in d
    assert ex(cfg, 0.98, secs_left=100)[0] == "take_profit"
    assert ex(cfg, 0.20, secs_left=100)[0] == "stop_loss"


def test_time_exit_blocks_entries():
    cfg = replace(BASE, fav_min_secs_left=0, fav_max_secs_left=900, fav_min_price=0.85, fav_max_price=0.95,
                  fav_edge_est=0.03, max_spread=0.03, fav_exit_seconds_left=300)
    assert sig(cfg, 301)[0] == "ENTER"
    assert sig(cfg, 300)[0] == "skip:inside_time_exit"
    assert sig(replace(cfg, fav_exit_seconds_left=0), 300)[0] == "ENTER"


def test_model_entry_gate_respects_time_exit():
    cfg = replace(BASE, fav_exit_seconds_left=200, min_seconds_left_15m=60, min_elapsed_frac=0.0)
    v, _ = bot.entry_gates(cfg, duration="15m", secs_left=150, elapsed=0.8, p_side=0.9, bid=0.70, ask=0.71,
                           fee_rate=0.07)
    assert v == "skip:inside_time_exit"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f(); print("ok", f.__name__)
    print(f"{len(fns)}/{len(fns)} passed")
