"""Offline tests for the pre-trade news & sentiment gate (no network). Run:
    python tests/test_news_gate.py          (also works under pytest)"""
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-ng-"))
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

NOW = 1792280000.0
CFG = replace(bot.CFG, news_gate=True, news_gate_threshold=0.10, news_gate_min_relevant=5, news_gate_min_scored=2,
              news_gate_min_sources=2, news_gate_max_age_seconds=300, news_gate_headline_minutes=180,
              news_gate_half_life_minutes=45, news_gate_mixed_ratio=0.6, news_gate_funding_weight=0.0,
              news_gate_funding_extreme=0.0003, fav_min_price=0.82, fav_max_price=0.92, fav_edge_est=0.03,
              fav_min_secs_left=600, fav_max_secs_left=900, max_spread=0.03)

NEUTRAL = ["Bitcoin holds steady near $98K", "Crypto markets await Fed minutes", "BTC trading volume thin on Sunday"]
BULL = ["Bitcoin rallies as ETF inflows surge", "Shorts liquidated as BTC reclaims $100K",
        "Whales accumulate bitcoin, exchange outflows hit record"]
BEAR = ["Bitcoin slides as longs liquidated", "Crypto exchange hacked, halts withdrawals",
        "Hawkish Fed sends bitcoin lower"]


def state(titles, ages_min=None, sources=5, fetched_ago=10, funding=None, cfg=CFG):
    ages_min = ages_min or [5 + 3 * i for i in range(len(titles))]
    ns = bot.NewsState()
    for k, v in bot.build_gate(cfg, [(t, NOW - a * 60) for t, a in zip(titles, ages_min)], NOW, funding).items():
        setattr(ns, k, v)
    ns.gate_sources, ns.gate_fetched_at = sources, NOW - fetched_ago
    return ns


def test_lexicon_word_boundaries_and_phrases():
    assert bot.gate_headline_score("Stablecoins may never escape banking risks") == 0
    assert bot.gate_headline_score("Bitcoin mining cost breakdown") == 0
    assert bot.gate_headline_score("Exchange outflows hit record") > 0     # not counted as bearish "outflows"
    assert bot.gate_headline_score("ETF outflows reach 10-day high") < 0
    assert bot.gate_headline_score("Shorts liquidated as BTC jumps") > 0
    assert bot.gate_headline_score("Longs liquidated as BTC drops") < 0
    assert bot.gate_headline_score("Fed turns dovish") > 0 and bot.gate_headline_score("Fed turns hawkish") < 0


def test_relevance_filters_alts_and_spam():
    assert not bot.gate_relevant("Solana price drops on selloffs")
    assert bot.gate_relevant("Solana and Bitcoin drop on selloffs")
    assert not bot.gate_relevant("Next crypto to explode: analysts eye 100x")
    assert bot.gate_relevant("Powell says inflation remains sticky")


def test_bullish_allows_up_blocks_down():
    ns = state(NEUTRAL + BULL)
    assert bot.news_gate_label(CFG, ns) == "bullish"
    assert bot.news_gate_verdict(CFG, ns, "UP", NOW)[0] == "ok"
    assert bot.news_gate_verdict(CFG, ns, "DOWN", NOW)[0] == "skip:news_against"


def test_bearish_allows_down_blocks_up():
    ns = state(NEUTRAL + BEAR)
    assert bot.news_gate_label(CFG, ns) == "bearish"
    assert bot.news_gate_verdict(CFG, ns, "DOWN", NOW)[0] == "ok"
    assert bot.news_gate_verdict(CFG, ns, "UP", NOW)[0] == "skip:news_against"


def test_neutral_and_mixed_skip_both_sides():
    ns = state(NEUTRAL + NEUTRAL[:2] + ["Bitcoin gains slightly", "Bitcoin falls slightly"])
    assert bot.news_gate_verdict(CFG, ns, "UP", NOW)[0] == "skip:news_neutral"
    ns = state(NEUTRAL + BULL + BEAR)
    assert bot.news_gate_label(CFG, ns) == "mixed"
    for side in ("UP", "DOWN"):
        assert bot.news_gate_verdict(CFG, ns, side, NOW)[0] == "skip:news_mixed"


def test_fails_closed():
    assert bot.news_gate_verdict(CFG, bot.NewsState(), "UP", NOW)[0] == "skip:news_stale"     # never fetched
    assert bot.news_gate_verdict(CFG, state(NEUTRAL + BULL, fetched_ago=400), "UP", NOW)[0] == "skip:news_stale"
    assert bot.news_gate_verdict(CFG, state(NEUTRAL + BULL, sources=1), "UP", NOW)[0] == "skip:news_sources"
    assert bot.news_gate_verdict(CFG, state(BULL[:2]), "UP", NOW)[0] == "skip:news_thin"
    old = state(NEUTRAL + BULL, ages_min=[200] * 6)        # all headlines older than the 180-min window
    assert old.gate_relevant == 0 and bot.news_gate_verdict(CFG, old, "UP", NOW)[0] == "skip:news_thin"


def test_gate_off_and_dedupe():
    assert bot.news_gate_verdict(replace(CFG, news_gate=False), bot.NewsState(), "UP", NOW)[0] == "ok"
    ns = state(BULL + BULL + NEUTRAL)
    assert ns.gate_relevant == 6


def test_funding_weight():
    titles = NEUTRAL + NEUTRAL[:2]
    assert state(titles, funding=0.0006).gate_score == 0
    c = replace(CFG, news_gate_funding_weight=0.5)
    assert state(titles, funding=0.0006, cfg=c).gate_score == -0.5    # crowded longs -> bearish lean
    assert state(titles, funding=-0.0006, cfg=c).gate_score == 0.5


def test_works_with_82_92_band_through_decide_favorite():
    """End to end through decide_favorite with the 0.82-0.92 band (Sammy's live setting)."""
    from types import SimpleNamespace
    saved = (bot.CFG, bot.get_book, bot.now_ts, bot._common_gates)
    market = SimpleNamespace(slug="btc-updown-15m-1792279800", seconds_left=700, duration="15m",
                             elapsed_frac=0.2, up_token="u", down_token="d", fee_rate=0.07)
    up = bot.Book(best_bid=0.86, best_ask=0.87, bid_depth_usd=100, ask_depth_usd=100,
                  bids=[(0.86, 100.0)], asks=[(0.87, 100.0)])
    down = bot.Book(best_bid=0.13, best_ask=0.14, bid_depth_usd=100, ask_depth_usd=100,
                    bids=[(0.13, 100.0)], asks=[(0.14, 100.0)])
    try:
        bot.CFG = replace(CFG, strategy="favorite", use_news=False)
        bot.get_book = lambda tok: up if tok == "u" else down
        bot.now_ts = lambda: NOW
        bot._common_gates = lambda *a, **k: None
        sig, verdict, d = bot.decide_favorite(market, None, state(NEUTRAL + BULL))
        assert verdict == "ENTER" and sig.side == "UP" and "bullish" in d["news_gate"]
        sig, verdict, _ = bot.decide_favorite(market, None, state(NEUTRAL + BEAR))
        assert sig is None and verdict == "skip:news_against"
        sig, verdict, _ = bot.decide_favorite(market, None, bot.NewsState())
        assert sig is None and verdict == "skip:news_stale"
        bot.CFG = replace(bot.CFG, news_gate=False)
        sig, verdict, _ = bot.decide_favorite(market, None, bot.NewsState())
        assert verdict == "ENTER"                      # gate off: entry logic unchanged
    finally:
        bot.CFG, bot.get_book, bot.now_ts, bot._common_gates = saved


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f(); print("ok", f.__name__)
    print(f"{len(fns)}/{len(fns)} passed")
