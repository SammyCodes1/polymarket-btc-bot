"""Offline tests for FAV_MIN_SIZE_ONLY (stake only the minimum order + buffer). Run:
    python tests/test_min_size.py          (also works under pytest)"""
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-ms-"))
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

BASE = replace(bot.CFG, max_position_usd=4.80, max_position_pct=1.0, kelly_fraction=1.0,
               max_total_btc_exposure_pct=1.0, max_net_direction_pct=1.0,
               fav_min_size_only=False, fav_min_size_buffer=0.03)


def sz(cfg, ask, bank=10.0, p=0.99):
    return bot.size_usd(cfg, bank=bank, p_side=p, price=ask, total_exposure=0.0, same_dir_exposure=0.0,
                        min_order_usd=5 * ask)


def test_default_off():
    assert bot.Config().fav_min_size_only is False or os.getenv("FAV_MIN_SIZE_ONLY")
    assert sz(BASE, 0.85)[0] == 4.80


def test_min_size_stake():
    cfg = replace(BASE, fav_min_size_only=True)
    usd, why = sz(cfg, 0.83)
    assert why == "ok" and usd == 4.28          # ceil(4.15 * 1.03)
    usd, _ = sz(cfg, 0.92)
    assert usd == 4.74                          # ceil(4.60 * 1.03)
    assert usd / 0.92 >= 5


def test_never_above_cap_and_still_rejects_below_min():
    cfg = replace(BASE, fav_min_size_only=True, max_position_usd=4.50)
    assert sz(cfg, 0.88)[0] == 4.50             # cap 4.50 < 4.40*1.03 -> stays at cap
    assert sz(cfg, 0.92)[0] == 0.0              # 4.50 < 5 x 0.92 -> rejected as before


def test_zero_buffer():
    cfg = replace(BASE, fav_min_size_only=True, fav_min_size_buffer=0.0)
    assert sz(cfg, 0.86)[0] == 4.30


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
