#!/usr/bin/env python3
"""
Polymarket BTC Up/Down (5m + 15m) trading bot -- "the brain".

Built for Sammy's rules:
  1. Only Polymarket's Bitcoin Up/Down 5-minute and 15-minute markets, discovered
     across all of Polymarket (series + tag sweep, not a hard-coded list).
  2. Research before every entry: live BTC price data (Coinbase/Kraken/Binance.US,
     plus Polymarket's Chainlink "price to beat") and fresh crypto news.
  3. Enter only on very high conviction; exit anything the bot is no longer sure of.
  4. Take profit as soon as a solid profit is available; never let winners ride.
  5. Run continuously: refresh news, rescan markets, manage positions, repeat.
  6. Strict risk: small sizes, per-trade stop, daily loss cap, drawdown kill switch,
     correlated-exposure caps (all BTC up/down windows are one correlated bet).
  7. Plug-in wallet for testing. DRY_RUN (paper trading) is ON by default.

v2 (after a 21-day backtest, see README / backtest.py): the default strategy is "favorite" --
buy the 85-95c favorite early (10-15 min left) in 15m windows, take profit at >=97c, stop at -60%.
The v1 BTC-momentum model is kept as STRATEGY=model but lost money in the backtest.

Sections (search for "SECTION"):
  1. CONFIG            5. ORDER EXECUTION
  2. MARKET SCANNER    6. POSITION MANAGEMENT (profit-taking, stops, thesis exits)
  3. NEWS / RESEARCH   7. RISK CONTROLS
  4. DECISION LOGIC    8. MAIN LOOP

No script can guarantee profit. These markets are close to coin flips after fees
and spread. Paper trade first, then test live with money you can afford to lose.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import logging
import math
import os
import re
import signal
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:  # python-dotenv is optional
    pass

try:
    import feedparser  # type: ignore
except ImportError:
    feedparser = None


# =============================================================================
# SECTION 1: CONFIG
# =============================================================================
def _env(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw.strip()


LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_MONEY"


@dataclass
class Config:
    # --- mode ---
    dry_run: bool = _env("DRY_RUN", True)
    live_confirm: str = _env("LIVE_TRADING_CONFIRM", "")
    paper_bankroll_usd: float = _env("PAPER_BANKROLL_USD", 500.0)

    # --- wallet (only used when live) ---
    private_key: str = _env("POLYMARKET_PRIVATE_KEY", "")
    wallet_address: str = _env("POLYMARKET_WALLET_ADDRESS", "")
    relayer_api_key: str = _env("POLYMARKET_RELAYER_API_KEY", "")
    relayer_api_key_address: str = _env("POLYMARKET_RELAYER_API_KEY_ADDRESS", "")
    # Optional Builder API key (polymarket.com > Settings > API Keys > Builder). Either a Builder key
    # (all three values) or a Relayer key (key + address) enables gasless auto-redeem of winners.
    builder_api_key: str = _env("POLYMARKET_BUILDER_API_KEY", "")
    builder_api_secret: str = _env("POLYMARKET_BUILDER_API_SECRET", "")
    builder_api_passphrase: str = _env("POLYMARKET_BUILDER_API_PASSPHRASE", "")
    auto_redeem: bool = _env("AUTO_REDEEM", True)                      # try to redeem winners when a key is set
    redeem_retry_seconds: int = _env("REDEEM_RETRY_SECONDS", 300)     # retry a failed auto-redeem this often

    # --- endpoints ---
    gamma_url: str = _env("GAMMA_URL", "https://gamma-api.polymarket.com")
    clob_url: str = _env("CLOB_URL", "https://clob.polymarket.com")
    series_slugs: str = _env("SERIES_SLUGS", "btc-up-or-down-5m,btc-up-or-down-15m")
    use_pm_price_to_beat: bool = _env("USE_PM_PRICE_TO_BEAT", True)

    # --- loop timing (seconds) ---
    loop_seconds: float = _env("LOOP_SECONDS", 5.0)
    scan_seconds: float = _env("SCAN_SECONDS", 20.0)
    news_refresh_seconds: float = _env("NEWS_REFRESH_SECONDS", 120.0)
    news_max_age_seconds: float = _env("NEWS_MAX_AGE_SECONDS", 600.0)  # research must be this fresh to trade

    # --- strategy selection (see README "Backtest findings") ---
    # favorite: buy the side the market prices at 85-95c early in a 15m window (only rule
    #           with a positive in-sample AND out-of-sample backtest; still not statistically proven)
    # model:    v1 BTC-momentum model vs market (backtest: negative after fees; off by default)
    strategy: str = _env("STRATEGY", "favorite")
    fav_durations: str = _env("FAV_DURATIONS", "15m")
    fav_min_secs_left: int = _env("FAV_MIN_SECS_LEFT", 600)
    fav_max_secs_left: int = _env("FAV_MAX_SECS_LEFT", 900)
    fav_min_price: float = _env("FAV_MIN_PRICE", 0.85)       # favorite's mid must be >= this ...
    fav_max_price: float = _env("FAV_MAX_PRICE", 0.95)       # ... and < this
    fav_edge_est: float = _env("FAV_EDGE_EST", 0.015)        # assumed edge over mid for sizing (≈half of measured)
    # --- entry mode (favorite strategy) ---
    # market: FOK taker buy at the ask (pays the taker fee). limit: rest a post-only GTC/GTD BUY at
    # mid - LIMIT_OFFSET (maker, no fee) and wait for sellers; unfilled remainder is cancelled. See README.
    entry_mode: str = _env("ENTRY_MODE", "market")
    limit_offset: float = _env("LIMIT_OFFSET", 0.01)                    # bid = mid - offset, rounded down to tick
    limit_cancel_secs_left: int = _env("LIMIT_CANCEL_SECS_LEFT", 600)   # cancel the rest at/below this many s left
    limit_cancel_out_of_band: bool = _env("LIMIT_CANCEL_OUT_OF_BAND", True)  # cancel if the favorite leaves the band
    limit_reprice_cents: float = _env("LIMIT_REPRICE_CENTS", 1.0)       # re-quote when mid moves more than this ...
    limit_max_reprices: int = _env("LIMIT_MAX_REPRICES", 2)             # ... at most this many times (0 = never)
    limit_post_only: bool = _env("LIMIT_POST_ONLY", True)
    # Paper fill model for resting bids (conservative): a fill needs the book to cross our price (best ask <= bid)
    # or a print THROUGH our price (sell below it, or a complementary buy above 1 - bid). Prints exactly AT our
    # price fill only this fraction of their size (queue ahead of us); 0 = never.
    paper_at_price_fill_frac: float = _env("PAPER_AT_PRICE_FILL_FRAC", 0.0)
    # complete-set arbitrage: buy Up+Down when both asks + fees < $1 - ARB_MIN_EDGE
    arb_enabled: bool = _env("ARB_ENABLED", True)
    arb_min_edge: float = _env("ARB_MIN_EDGE", 0.01)
    arb_max_usd: float = _env("ARB_MAX_USD", 10.0)

    # --- model-strategy thresholds (high conviction only) ---
    min_model_prob: float = _env("MIN_MODEL_PROB", 0.80)     # our side must be >= 80% by the model
    min_edge: float = _env("MIN_EDGE", 0.07)                 # model prob - (ask + fee/share)
    max_entry_price: float = _env("MAX_ENTRY_PRICE", 0.85)   # leave room to take profit
    min_entry_price: float = _env("MIN_ENTRY_PRICE", 0.15)
    max_spread: float = _env("MAX_SPREAD", 0.03)
    max_disagreement: float = _env("MAX_DISAGREEMENT", 0.25)  # model vs market gap that signals bad data, not edge
    min_elapsed_frac: float = _env("MIN_ELAPSED_FRAC", 0.20) # let the window develop first
    min_seconds_left_5m: int = _env("MIN_SECONDS_LEFT_5M", 60)
    min_seconds_left_15m: int = _env("MIN_SECONDS_LEFT_15M", 120)
    news_veto: float = _env("NEWS_VETO", 0.35)               # skip if news leans against us this hard
    model_shrink: float = _env("MODEL_SHRINK", 0.85)         # humility: pull model toward 50%
    momentum_weight: float = _env("MOMENTUM_WEIGHT", 0.30)
    news_weight: float = _env("NEWS_WEIGHT", 0.25)
    use_news: bool = _env("USE_NEWS_IN_MODEL", False)  # backtest: no measurable value (see README)

    # --- position management ---
    take_profit_pct: float = _env("TAKE_PROFIT_PCT", 0.15)        # +15% on the position...
    take_profit_capture: float = _env("TAKE_PROFIT_CAPTURE", 0.50)  # ...or half the remaining upside, whichever first
    min_profit_usd: float = _env("MIN_PROFIT_USD", 0.10)
    take_profit_min_bid: float = _env("TAKE_PROFIT_MIN_BID", 0.97)  # TP also requires bid >= this
    stop_loss_pct: float = _env("STOP_LOSS_PCT", 0.60)
    thesis_exit_prob: float = _env("THESIS_EXIT_PROB", 0.0)       # exit if our side's probability drops below (0=off)
    force_exit_seconds: int = _env("FORCE_EXIT_SECONDS", 0)       # flatten this close to the end (0=hold to resolution)
    # Hard stop as an absolute drop from the entry price, in cents (e.g. 15 = sell once bid <= entry-0.15).
    # Fires before STOP_LOSS_PCT when it is the tighter of the two. 0 = off. Backtest (Sep 26-Oct 17 2026,
    # /workspace/bt_improve): 10/15/20c stops all LOWERED P&L in both the train and test halves, so it is off.
    fav_stop_drop_cents: float = _env("FAV_STOP_DROP_CENTS", 0.0)
    # Time exit: close any open position that is not at take-profit once this many seconds remain, and never
    # open a position with this little time left. 0 = off (hold to resolution). Backtest: 120/180/300s exits
    # cost 0.12-0.40 $/trade (selling into thin late books), so it is off. FORCE_EXIT_SECONDS still works too.
    fav_exit_seconds_left: int = _env("FAV_EXIT_SECONDS_LEFT", 0)
    slippage: float = _env("SLIPPAGE", 0.02)

    # --- risk controls ---
    max_position_pct: float = _env("MAX_POSITION_PCT", 0.02)      # 2% of bankroll per trade
    max_position_usd: float = _env("MAX_POSITION_USD", 10.0)
    kelly_fraction: float = _env("KELLY_FRACTION", 0.25)
    max_total_btc_exposure_pct: float = _env("MAX_TOTAL_BTC_EXPOSURE_PCT", 0.05)
    max_net_direction_pct: float = _env("MAX_NET_DIRECTION_PCT", 0.04)
    max_open_positions: int = _env("MAX_OPEN_POSITIONS", 2)
    daily_loss_limit_pct: float = _env("DAILY_LOSS_LIMIT_PCT", 0.05)
    max_drawdown_pct: float = _env("MAX_DRAWDOWN_PCT", 0.10)
    max_consecutive_losses: int = _env("MAX_CONSECUTIVE_LOSSES", 4)
    max_trades_per_day: int = _env("MAX_TRADES_PER_DAY", 20)
    cooldown_after_loss_seconds: int = _env("COOLDOWN_AFTER_LOSS_SECONDS", 600)
    kill_switch_file: str = _env("KILL_SWITCH_FILE", "KILL")
    manual_close_ttl_seconds: float = _env("MANUAL_CLOSE_TTL_SECONDS", 120.0)  # dashboard Close requests expire after this
    # Bookkeeping sanity guard: a bankroll move bigger than this fraction with no trade/resolve event is
    # treated as a glitch (no new peak, no drawdown halt) until it has persisted BANK_JUMP_WINDOW_SECONDS.
    bank_jump_pct: float = _env("BANK_JUMP_PCT", 0.30)
    bank_jump_window_seconds: int = _env("BANK_JUMP_WINDOW_SECONDS", 60)
    # A move with no trade event bigger than BANK_FLOW_MIN_USD (and BANK_FLOW_MIN_PCT of the bankroll) is never
    # trusted right away. If it persists BANK_JUMP_WINDOW_SECONDS it is booked as an external cash flow
    # (deposit / withdrawal / manual activity): peak and day-start shift by the same amount, no drawdown halt.
    bank_flow_min_usd: float = _env("BANK_FLOW_MIN_USD", 0.25)
    bank_flow_min_pct: float = _env("BANK_FLOW_MIN_PCT", 0.03)
    # The drawdown halt needs this many consecutive breaching checks (a single low read never halts).
    drawdown_confirm_reads: int = _env("DRAWDOWN_CONFIRM_READS", 2)

    # --- news sources ---
    rss_feeds: str = _env(
        "RSS_FEEDS",
        "https://cointelegraph.com/rss,"
        "https://decrypt.co/feed,"
        "https://www.coindesk.com/arc/outboundfeeds/rss/,"
        "https://news.google.com/rss/search?q=bitcoin+when:1h&hl=en-US&gl=US&ceid=US:en",
    )
    cryptopanic_api_key: str = _env("CRYPTOPANIC_API_KEY", "")
    newsapi_key: str = _env("NEWSAPI_KEY", "")
    news_half_life_minutes: float = _env("NEWS_HALF_LIFE_MINUTES", 30.0)
    # --- pre-trade news & sentiment gate (favorite strategy) ---
    # Before every favorite buy: UP only if fresh news is bullish, DOWN only if bearish, otherwise skip.
    # Fails closed: stale / thin / unreachable news => no order. See README "News gate".
    news_gate: bool = _env("NEWS_GATE", False)     # off by default (unproven; Sammy's call)
    news_gate_threshold: float = _env("NEWS_GATE_THRESHOLD", 0.10)      # |score| needed for bullish/bearish
    news_gate_min_relevant: int = _env("NEWS_GATE_MIN_RELEVANT", 5)     # relevant headlines in the window
    news_gate_min_scored: int = _env("NEWS_GATE_MIN_SCORED", 2)         # ... of which carry a non-zero score
    news_gate_min_sources: int = _env("NEWS_GATE_MIN_SOURCES", 2)       # feeds that answered
    news_gate_max_age_seconds: float = _env("NEWS_GATE_MAX_AGE_SECONDS", 300.0)   # last successful refresh
    news_gate_headline_minutes: float = _env("NEWS_GATE_HEADLINE_MINUTES", 180.0)  # headlines older are ignored
    news_gate_half_life_minutes: float = _env("NEWS_GATE_HALF_LIFE_MINUTES", 45.0)
    news_gate_mixed_ratio: float = _env("NEWS_GATE_MIXED_RATIO", 0.6)   # minority/majority weight >= this => mixed
    news_gate_feeds: str = _env(
        "NEWS_GATE_FEEDS",
        "https://news.google.com/rss/search?q=bitcoin+price+when:3h&hl=en-NG&gl=NG&ceid=NG:en,"
        "https://news.google.com/rss/search?q=bitcoin+(funding+rate+OR+perpetual+OR+%22open+interest%22+OR+liquidations)"
        "+when:6h&hl=en-NG&gl=NG&ceid=NG:en,"
        "https://news.google.com/rss/search?q=crypto+exchange+(hack+OR+outage+OR+withdrawals+OR+listing+OR+delist)"
        "+when:6h&hl=en-NG&gl=NG&ceid=NG:en,"
        "https://news.google.com/rss/search?q=(Fed+OR+FOMC+OR+CPI+OR+inflation+OR+Powell+OR+tariffs)+markets"
        "+when:3h&hl=en-NG&gl=NG&ceid=NG:en,"
        "https://news.google.com/rss/search?q=bitcoin+whale+when:6h&hl=en-NG&gl=NG&ceid=NG:en,"
        "https://news.google.com/rss/search?q=crypto+(regulation+OR+SEC+OR+CFTC+OR+ETF)+when:6h&hl=en-NG&gl=NG&ceid=NG:en,"
        "https://www.theblock.co/rss.xml,https://www.newsbtc.com/feed/,https://cryptoslate.com/feed/,"
        "https://bitcoinist.com/feed/",
    )
    # Perp funding (Hyperliquid public API, Deribit fallback; both reachable without Binance/Coinbase/Kraken).
    # Contrarian: very positive funding (crowded longs) leans bearish. Weight 0 = shown in logs only.
    news_gate_funding: bool = _env("NEWS_GATE_FUNDING", True)
    news_gate_funding_weight: float = _env("NEWS_GATE_FUNDING_WEIGHT", 0.0)
    news_gate_funding_extreme: float = _env("NEWS_GATE_FUNDING_EXTREME", 0.0003)  # 8h rate treated as "max"
    llm_scorer_enabled: bool = _env("LLM_SCORER_ENABLED", False)
    llm_api_url: str = _env("LLM_API_URL", "")   # OpenAI-compatible /v1/chat/completions
    llm_api_key: str = _env("LLM_API_KEY", "")
    llm_model: str = _env("LLM_MODEL", "")

    # --- files ---
    data_dir: str = _env("DATA_DIR", str(Path(__file__).with_name("data")))

    @property
    def live(self) -> bool:
        return (not self.dry_run) and self.live_confirm == LIVE_CONFIRM_PHRASE


CFG = Config()
DATA = Path(CFG.data_dir)
DATA.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA / "state.json"
DECISIONS_CSV = DATA / "decisions.csv"
TRADES_CSV = DATA / "trades.csv"
CLOSE_REQUESTS = DATA / "close_requests.json"   # written by the dashboard (POST /api/positions/close)
CLOSE_RESULTS = DATA / "close_results.json"     # written by the bot: one result per request id

log = logging.getLogger("pmbot")


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(DATA / "bot.log")],
    )

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0 (pm-btc-bot)"})


def now_ts() -> float:
    return time.time()


def utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else now_ts(), tz=timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def get_json(url: str, params: Optional[dict] = None, timeout: float = 8.0) -> Any:
    r = HTTP.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def append_csv(path: Path, row: dict) -> None:
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# =============================================================================
# SECTION 2: MARKET SCANNER
# =============================================================================
SLUG_RE = re.compile(r"^btc-updown-(5m|15m)-(\d+)$")
WINDOW_SECONDS = {"5m": 300, "15m": 900}


@dataclass
class BtcMarket:
    slug: str
    duration: str            # "5m" | "15m"
    condition_id: str
    question: str
    start_ts: float          # window start (price-to-beat time)
    end_ts: float            # window end / resolution time
    up_token: str
    down_token: str
    tick_size: float
    min_order_size: float
    fee_rate: float          # taker fee rate, fee/share = rate * p * (1-p)
    accepting_orders: bool
    version: str
    twap_seconds: float = 60.0   # final value is a Chainlink TWAP over this lookback

    @property
    def seconds_left(self) -> float:
        return self.end_ts - now_ts()

    @property
    def elapsed_frac(self) -> float:
        span = self.end_ts - self.start_ts
        return max(0.0, min(1.0, (now_ts() - self.start_ts) / span)) if span > 0 else 1.0


def _parse_market(m: dict) -> Optional[BtcMarket]:
    slug = m.get("slug", "")
    match = SLUG_RE.match(slug)
    cfg = m.get("cryptoMarketConfig") or {}
    duration = match.group(1) if match else cfg.get("duration")
    if duration not in WINDOW_SECONDS:
        return None
    if cfg and cfg.get("asset", "btc") != "btc":
        return None
    if m.get("closed") or not m.get("enableOrderBook", True):
        return None
    try:
        outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"]
        tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m["clobTokenIds"]
        idx = {o.lower(): i for i, o in enumerate(outcomes)}
        end_ts = parse_iso(m["endDate"])
        if m.get("eventStartTime"):
            start_ts = parse_iso(m["eventStartTime"])
        elif match:
            start_ts = float(match.group(2))
        else:
            start_ts = end_ts - WINDOW_SECONDS[duration]
        fee_rate = float((m.get("feeSchedule") or {}).get("rate", 0.07)) if m.get("feesEnabled", True) else 0.0
        return BtcMarket(
            slug=slug, duration=duration, condition_id=m.get("conditionId", ""),
            question=m.get("question", ""), start_ts=start_ts, end_ts=end_ts,
            up_token=tokens[idx["up"]], down_token=tokens[idx["down"]],
            tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
            min_order_size=float(m.get("orderMinSize") or 5),
            fee_rate=fee_rate, accepting_orders=bool(m.get("acceptingOrders", True)),
            version=str(m.get("version", "v1")),
            twap_seconds=float(cfg.get("twapLookbackSeconds") or 60) if cfg.get("twapEnabled", True) else 0.0,
        )
    except (KeyError, ValueError, IndexError, TypeError) as e:
        log.debug("skip market %s: %s", slug, e)
        return None


def scan_btc_updown_markets() -> list[BtcMarket]:
    """Find every live BTC Up/Down 5m/15m market on Polymarket.

    Three discovery routes, merged and de-duplicated:
      a) the recurring series (btc-up-or-down-5m / -15m) filtered to windows not yet ended
      b) a sweep of everything tagged 'bitcoin' that is not yet ended (catches new series)
      c) deterministic slugs btc-updown-<dur>-<window_start_unix> for current + next window
    """
    found: dict[str, BtcMarket] = {}
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def ingest(events: list) -> None:
        for ev in events or []:
            for m in ev.get("markets", []):
                if "eventStartTime" not in m and ev.get("startTime"):
                    m["eventStartTime"] = ev["startTime"]
                bm = _parse_market(m)
                if bm and bm.end_ts > now_ts():
                    found[bm.slug] = bm

    for series in [s.strip() for s in CFG.series_slugs.split(",") if s.strip()]:
        try:
            ingest(get_json(f"{CFG.gamma_url}/events", {
                "series_slug": series, "closed": "false", "end_date_min": now_iso,
                "order": "endDate", "ascending": "true", "limit": 6}))
        except Exception as e:
            log.warning("scanner: series %s failed: %s", series, e)
    try:
        ingest(get_json(f"{CFG.gamma_url}/events", {
            "tag_slug": "bitcoin", "closed": "false", "end_date_min": now_iso,
            "order": "endDate", "ascending": "true", "limit": 40}))
    except Exception as e:
        log.warning("scanner: tag sweep failed: %s", e)
    t = int(now_ts())
    for dur, span in WINDOW_SECONDS.items():
        for start in (t - t % span, t - t % span + span):
            slug = f"btc-updown-{dur}-{start}"
            if slug in found:
                continue
            try:
                ingest(get_json(f"{CFG.gamma_url}/events", {"slug": slug}))
            except Exception as e:
                log.debug("scanner: slug %s failed: %s", slug, e)
    return sorted(found.values(), key=lambda m: m.end_ts)


@dataclass
class Book:
    best_bid: float
    best_ask: float
    bid_depth_usd: float     # $ available within 3c of best bid
    ask_depth_usd: float
    bids: list               # [(price, size)] best first
    asks: list
    token_id: str = ""

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def mid(self) -> float:
        return (self.best_ask + self.best_bid) / 2


def get_book(token_id: str) -> Optional[Book]:
    try:
        raw = get_json(f"{CFG.clob_url}/book", {"token_id": token_id})
    except Exception as e:
        log.warning("book %s…: %s", token_id[:10], e)
        return None
    bids = sorted(((float(b["price"]), float(b["size"])) for b in raw.get("bids", [])), reverse=True)
    asks = sorted((float(a["price"]), float(a["size"])) for a in raw.get("asks", []))
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    return Book(
        best_bid=bb, best_ask=ba,
        bid_depth_usd=sum(p * s for p, s in bids if p >= bb - 0.03),
        ask_depth_usd=sum(p * s for p, s in asks if p <= ba + 0.03),
        bids=bids, asks=asks, token_id=token_id,
    )


# --- BTC price signal --------------------------------------------------------
@dataclass
class PriceSnapshot:
    spot: float
    closes_1m: list          # oldest -> newest, 1-minute closes
    candles: list            # [(ts, open, high, low, close)]
    sigma_1m: float          # stdev of 1m log returns
    source: str
    fetched_at: float


def fetch_btc_prices() -> Optional[PriceSnapshot]:
    """1-minute BTC/USD candles from public REST APIs, first source that works."""
    sources = [
        ("bitstamp", "https://www.bitstamp.net/api/v2/ohlc/btcusd/", {"step": 60, "limit": 120}),
        ("coinbase", "https://api.exchange.coinbase.com/products/BTC-USD/candles", {"granularity": 60}),
        ("kraken", "https://api.kraken.com/0/public/OHLC", {"pair": "XBTUSD", "interval": 1}),
        ("binance.us", "https://api.binance.us/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1m", "limit": 120}),
    ]
    for name, url, params in sources:
        try:
            data = get_json(url, params, timeout=6)
            if name == "bitstamp":   # {"data": {"ohlc": [{timestamp, open, high, low, close}]}} oldest first
                candles = [(float(c["timestamp"]), float(c["open"]), float(c["high"]), float(c["low"]),
                            float(c["close"])) for c in data["data"]["ohlc"]]
            elif name == "coinbase":   # [time, low, high, open, close, vol], newest first
                candles = [(float(c[0]), float(c[3]), float(c[2]), float(c[1]), float(c[4])) for c in data][::-1]
            elif name == "kraken":   # [time, o, h, l, c, vwap, vol, count]
                rows = next(v for k, v in data["result"].items() if k != "last")
                candles = [(float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in rows]
            else:                    # [openTime ms, o, h, l, c, ...]
                candles = [(c[0] / 1000, float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in data]
            candles = candles[-120:]
            closes = [c[4] for c in candles]
            rets = [math.log(b / a) for a, b in zip(closes[-61:-1], closes[-60:]) if a > 0 and b > 0]
            sigma = max(statistics.pstdev(rets) if len(rets) > 10 else 0.001, 0.0003)
            spot = closes[-1]
            try:  # candles lag up to a minute; the ticker is real-time
                if name == "bitstamp":
                    spot = float(get_json("https://www.bitstamp.net/api/v2/ticker/btcusd/", timeout=4)["last"])
                else:
                    spot = float(get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=4)["price"])
            except Exception as e:
                log.debug("ticker failed, using last candle close: %s", e)
            return PriceSnapshot(spot=spot, closes_1m=closes, candles=candles,
                                 sigma_1m=sigma, source=name, fetched_at=now_ts())
        except Exception as e:
            log.warning("price source %s failed: %s", name, e)
    return None


_PTB_CACHE: dict[str, float] = {}
_BASIS: dict[str, float] = {}   # Chainlink / exchange ratio measured at the window open


def price_to_beat(market: BtcMarket, snap: PriceSnapshot) -> tuple[Optional[float], str]:
    """The window's opening price. Polymarket resolves on Chainlink's BTC/USD stream;
    its site exposes that opening value through an (undocumented) endpoint, which we
    use when it agrees with exchange data. Otherwise fall back to the exchange 1m open."""
    if market.slug in _PTB_CACHE:
        return _PTB_CACHE[market.slug], "cache"
    candle_open = None
    for ts, o, *_ in snap.candles:
        if abs(ts - market.start_ts) < 30:
            candle_open = o
            break
    if CFG.use_pm_price_to_beat and now_ts() >= market.start_ts:
        try:
            variant = {"5m": "fiveminute", "15m": "fifteen"}[market.duration]
            d = get_json("https://polymarket.com/api/crypto/crypto-price", {
                "symbol": "BTC", "variant": variant,
                "eventStartTime": utc_iso(market.start_ts).replace("+00:00", "Z"),
                "endDate": utc_iso(market.end_ts).replace("+00:00", "Z")}, timeout=5)
            op = float(d.get("openPrice") or 0)
            ref = candle_open or snap.spot
            if op > 0 and abs(op / ref - 1) < 0.003:
                _PTB_CACHE[market.slug] = op
                if candle_open:
                    # Chainlink and Coinbase differ by a few bp; on 5-minute horizons that matters,
                    # so scale exchange spot onto the Chainlink basis measured at the open.
                    _BASIS[market.slug] = op / candle_open
                return op, "polymarket-chainlink"
        except Exception as e:
            log.debug("price-to-beat endpoint failed for %s: %s", market.slug, e)
    if candle_open:
        _PTB_CACHE[market.slug] = candle_open
        return candle_open, f"{snap.source}-1m-open"
    return None, "unavailable"


# =============================================================================
# SECTION 3: NEWS / WEB RESEARCH MODULE
# =============================================================================
BULLISH = {
    "etf inflow": 2.0, "inflows": 1.5, "approval": 1.5, "approves": 1.5, "rally": 1.5, "surge": 1.5,
    "soar": 1.5, "all-time high": 2.0, "record high": 2.0, "breakout": 1.2, "bullish": 1.2,
    "rate cut": 1.5, "adoption": 1.0, "buys bitcoin": 1.5, "accumulat": 1.0, "short squeeze": 1.5,
    "jumps": 1.2, "gains": 0.8, "rebound": 1.0, "reserve": 1.0, "upgrade": 0.6, "climbs": 1.0,
}
BEARISH = {
    "etf outflow": 2.0, "outflows": 1.5, "hack": 2.0, "exploit": 1.8, "ban": 1.5, "crackdown": 1.5,
    "lawsuit": 1.2, "sec sues": 1.8, "liquidat": 1.3, "plunge": 1.8, "crash": 2.0, "dump": 1.5,
    "selloff": 1.5, "sell-off": 1.5, "bearish": 1.2, "rate hike": 1.5, "slumps": 1.5, "falls": 1.0,
    "drops": 1.0, "tumbles": 1.5, "insolvency": 2.0, "bankrupt": 2.0, "outage": 1.0, "fud": 0.8,
}
BTC_TERMS = ("bitcoin", "btc", "crypto", "fed ", "fomc", "cpi", "etf", "inflation", "powell", "treasury")


@dataclass
class NewsState:
    sentiment: float = 0.0       # -1 (bearish) .. +1 (bullish), recency weighted
    n_items: int = 0
    n_relevant: int = 0
    top_headlines: list = field(default_factory=list)
    fetched_at: float = 0.0
    ok_sources: int = 0
    # news gate (see news_gate_verdict)
    gate_score: float = 0.0          # -1..+1, recency weighted, includes funding if weighted
    gate_bull_w: float = 0.0         # weight of bullish headlines
    gate_bear_w: float = 0.0         # weight of bearish headlines
    gate_relevant: int = 0
    gate_scored: int = 0
    gate_sources: int = 0
    gate_fetched_at: float = 0.0
    gate_headlines: list = field(default_factory=list)   # [(score, age_min, topic, title)] strongest first
    funding_8h: Optional[float] = None
    funding_source: str = ""


def _rss_items(url: str, titles_only: bool = False) -> list[tuple[str, float]]:
    r = HTTP.get(url, timeout=8)
    r.raise_for_status()
    items = []
    if feedparser:
        for e in feedparser.parse(r.content).entries:
            ts = now_ts()
            for key in ("published_parsed", "updated_parsed"):
                if e.get(key):
                    ts = float(calendar.timegm(e[key]))   # feedparser structs are UTC
                    break
            items.append((e.get("title", "") if titles_only else f"{e.get('title', '')}. {e.get('summary', '')[:300]}", ts))
    else:  # tiny fallback parser
        for blk in re.findall(r"<item>(.*?)</item>", r.text, re.S):
            title = re.search(r"<title>(.*?)</title>", blk, re.S)
            pub = re.search(r"<pubDate>(.*?)</pubDate>", blk, re.S)
            ts = parsedate_to_datetime(pub.group(1)).timestamp() if pub else now_ts()
            items.append((re.sub(r"<!\[CDATA\[|\]\]>", "", title.group(1)) if title else "", ts))
    return items


def _api_items() -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    if CFG.cryptopanic_api_key:
        try:
            d = get_json("https://cryptopanic.com/api/developer/v2/posts/",
                         {"auth_token": CFG.cryptopanic_api_key, "currencies": "BTC", "public": "true"})
            for p in d.get("results", []):
                items.append((p.get("title", ""), parse_iso(p["published_at"])))
        except Exception as e:
            log.warning("cryptopanic failed: %s", e)
    if CFG.newsapi_key:
        try:
            d = get_json("https://newsapi.org/v2/everything",
                         {"q": "bitcoin OR btc", "sortBy": "publishedAt", "pageSize": 30,
                          "language": "en", "apiKey": CFG.newsapi_key})
            for a in d.get("articles", []):
                items.append((f"{a.get('title', '')}. {a.get('description') or ''}", parse_iso(a["publishedAt"])))
        except Exception as e:
            log.warning("newsapi failed: %s", e)
    return items


def _keyword_score(text: str) -> float:
    t = text.lower()
    s = sum(w for k, w in BULLISH.items() if k in t) - sum(w for k, w in BEARISH.items() if k in t)
    return max(-1.0, min(1.0, s / 3.0))


def _llm_scores(headlines: list[str]) -> Optional[list[float]]:
    """Optional hook: OpenAI-compatible chat endpoint returns a JSON list of scores in [-1,1]."""
    if not (CFG.llm_scorer_enabled and CFG.llm_api_url and CFG.llm_api_key and CFG.llm_model):
        return None
    prompt = ("Score each headline for its likely effect on Bitcoin's price over the next 15 minutes, "
              "from -1 (strongly bearish) to 1 (strongly bullish); 0 if irrelevant or stale. "
              "Reply with only a JSON array of numbers.\n" + "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines)))
    try:
        r = HTTP.post(CFG.llm_api_url, timeout=20,
                      headers={"Authorization": f"Bearer {CFG.llm_api_key}"},
                      json={"model": CFG.llm_model, "temperature": 0,
                            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        scores = json.loads(text[text.index("["): text.rindex("]") + 1])
        if len(scores) == len(headlines):
            return [max(-1.0, min(1.0, float(s))) for s in scores]
    except Exception as e:
        log.warning("LLM scorer failed, using keywords: %s", e)
    return None


# Extra lexicon for the pre-trade gate (perps/funding, exchanges, macro, whales, regulation). Phrases are matched
# longest first and consumed, so "exchange outflows" (bullish) is not also counted as "outflows" (bearish).
GATE_BULLISH = {
    **BULLISH,
    "shorts liquidated": 1.5, "short liquidations": 1.5, "short sellers": 0.6, "dovish": 1.5, "rate cuts": 1.5,
    "cooler inflation": 1.5, "inflation cools": 1.5, "cooler-than-expected": 1.5, "softer-than-expected": 1.2,
    "whale buys": 1.2, "whales buy": 1.2, "whales accumulate": 1.2, "whale accumulation": 1.2,
    "exchange outflows": 1.2, "withdrawn from exchanges": 1.0, "record inflows": 2.0, "approved": 1.2,
    "pro-crypto": 1.0, "strategic bitcoin reserve": 1.5, "rallies": 1.5, "rises": 0.8, "recovers": 1.0,
    "recovery": 0.8, "bounce": 0.8, "reclaims": 1.2, "pumps": 1.2, "tops $": 1.0, "higher": 0.5,
    "buying pressure": 1.2, "risk-on": 1.0, "relisted": 0.8, "lists ": 0.4, "listing": 0.4, "positive funding": 0.3,
    "golden cross": 1.0, "new high": 1.5, "uptrend": 1.0, "optimism": 0.8,
    "tariff relief": 1.0, "waives tariffs": 1.0, "waives threat": 0.8, "pauses tariffs": 1.0, "tariff cut": 1.0,
    "trade deal": 0.8, "eases tariffs": 1.0,
}
GATE_BEARISH = {
    **BEARISH,
    "longs liquidated": 1.5, "long liquidations": 1.5, "hawkish": 1.5, "hotter-than-expected": 1.5,
    "hot inflation": 1.5, "inflation rises": 1.0, "inflation jumps": 1.2, "tariff": 1.0, "tariffs": 1.0,
    "sell pressure": 1.2, "selling pressure": 1.2, "whale sells": 1.2, "whales sell": 1.2, "whale dumps": 1.5,
    "moves to exchange": 1.0, "sent to exchange": 1.0, "deposited to": 0.8, "exchange inflows": 1.2,
    "halts withdrawals": 2.0, "withdrawals paused": 2.0, "suspends withdrawals": 2.0, "delist": 1.2, "sues": 1.2,
    "charges": 0.8, "probe": 0.8, "investigation": 0.8, "slides": 1.2, "sinks": 1.5, "dips": 0.8, "lower": 0.5,
    "loses": 0.8, "fear": 0.8, "recession": 1.2, "yields jump": 0.8, "yields rise": 0.8, "dollar rallies": 1.0,
    "dollar strengthens": 0.8, "risk-off": 1.0, "death cross": 1.0, "downtrend": 1.0, "breakdown": 1.0,
    "rejected": 0.8, "rejection": 0.8, "drained": 1.2, "stolen": 1.5, "weakness": 0.8, "below $": 0.4,
}
GATE_BEARISH.pop("breakdown", None)      # "cost breakdown" etc.: too ambiguous
GATE_BEARISH.update({"banned": 1.5, "bans": 1.5, "hacked": 2.0})
_GATE_STEMS = ("liquidat", "accumulat", "exploit", "delist", "tariff", "regulat", "insolven", "bankrupt")


def _gate_pattern(k: str) -> "re.Pattern":
    # Word-boundary match ("ban" must not hit "banking"); stems and simple inflections still match.
    k = k.strip()
    tail = "" if k.startswith(_GATE_STEMS) or not k[-1].isalpha() else r"(?:s|es|ed|ing)?(?![a-z])"
    return re.compile(r"(?<![a-z0-9])" + re.escape(k) + tail)


_GATE_LEX = sorted([(k, w) for k, w in GATE_BULLISH.items()] + [(k, -w) for k, w in GATE_BEARISH.items()],
                   key=lambda kv: -len(kv[0]))
_GATE_LEX = [(_gate_pattern(k), w) for k, w in _GATE_LEX]
# Altcoin-only stories and promo spam are not BTC news.
GATE_ALT_TERMS = ("solana", "xrp", "ripple", "dogecoin", "doge", "cardano", "worldcoin", "wld", "avax", "avalanche",
                  "sui", "pepe", "shiba", "memecoin", "meme coin", "tron", "toncoin", "bnb", "litecoin", "chainlink",
                  "polkadot", "hbar", "hedera", "stellar", "xlm", "aptos", "altcoin")
GATE_SPAM_TERMS = ("next crypto to explode", "100x", "1000x", "presale", "pre-sale", "best crypto to buy",
                   "price prediction", "to buy now", "could make you", "millionaire")
GATE_TERMS = ("bitcoin", "btc", "crypto", "fed ", "fed's", "fomc", "cpi", "inflation", "powell", "treasury",
              "rate cut", "tariff", "sec ", "cftc", "etf", "exchange", "whale", "funding", "perpetual", "liquidat",
              "stablecoin", "tether", "open interest", "jobs report", "payrolls")
GATE_TOPICS = (("perps", ("funding", "perpetual", "open interest", "liquidat", "futures", "leverage")),
               ("whale", ("whale",)),
               ("regulation", ("sec ", "cftc", "regulat", "lawsuit", "sues", "bill", "congress", "senate", "ban")),
               ("exchange", ("exchange", "binance", "coinbase", "kraken", "bybit", "okx", "bitstamp", "hack", "withdraw")),
               ("macro", ("fed ", "fed's", "fomc", "cpi", "inflation", "powell", "tariff", "treasury", "yields",
                          "jobs report", "payrolls", "dollar", "recession")),
               ("price", ("price", "bitcoin", "btc")))


def gate_headline_score(title: str) -> float:
    """Lexicon score of one headline in [-1, 1]. Longest phrases win and consume their text."""
    t = " " + title.lower().replace("\u2019", "'") + " "
    s = 0.0
    for pat, w in _GATE_LEX:
        t, n = pat.subn(" | ", t)
        s += w * n
    return max(-1.0, min(1.0, s / 3.0))


def gate_relevant(title: str) -> bool:
    t = " " + title.lower() + " "
    if any(k in t for k in GATE_SPAM_TERMS):
        return False
    btc = "bitcoin" in t or re.search(r"(?<![a-z])btc(?![a-z])", t) is not None
    if not btc and any(re.search(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])", t) for a in GATE_ALT_TERMS):
        return False
    return any(k in t for k in GATE_TERMS)


def gate_topic(title: str) -> str:
    t = " " + title.lower() + " "
    for name, keys in GATE_TOPICS:
        if any(k in t for k in keys):
            return name
    return "other"


def build_gate(cfg: "Config", entries: list, now: float, funding_8h: Optional[float] = None) -> dict:
    """Pure: entries = [(title, published_ts)] (any sources). Returns the gate fields of NewsState."""
    horizon = now - cfg.news_gate_headline_minutes * 60
    seen, rows = set(), []
    for title, ts in entries:
        title = re.sub(r"\s+", " ", (title or "")).strip()
        key = re.sub(r"[^a-z0-9 ]", "", title.lower())[:70]
        if not title or key in seen or ts < horizon or not gate_relevant(title):
            continue
        seen.add(key)
        rows.append((title, min(ts, now)))
    num = den = bull = bear = 0.0
    scored, heads = 0, []
    for title, ts in rows:
        age = (now - ts) / 60.0
        w = 0.5 ** (age / cfg.news_gate_half_life_minutes)
        sc = gate_headline_score(title)
        num += sc * w
        den += w
        if sc > 0:
            bull += sc * w
        elif sc < 0:
            bear += -sc * w
        if sc != 0:
            scored += 1
        heads.append((round(sc, 2), round(age), gate_topic(title), title[:110], w))
    score = (num / den) * min(1.0, den / 3.0) if den > 0 else 0.0
    if funding_8h is not None and cfg.news_gate_funding_weight > 0:
        f = -max(-1.0, min(1.0, funding_8h / cfg.news_gate_funding_extreme))
        score = (1 - cfg.news_gate_funding_weight) * score + cfg.news_gate_funding_weight * f
    heads.sort(key=lambda h: -abs(h[0]) * h[4])
    return dict(gate_score=round(max(-1.0, min(1.0, score)), 3), gate_bull_w=round(bull, 3), gate_bear_w=round(bear, 3),
                gate_relevant=len(rows), gate_scored=scored, gate_headlines=[h[:4] for h in heads[:6]])


def news_gate_label(cfg: "Config", news: "NewsState") -> str:
    hi, lo = max(news.gate_bull_w, news.gate_bear_w), min(news.gate_bull_w, news.gate_bear_w)
    # mixed = real news on both sides (each side carries >= 0.5 weighted score), roughly balanced
    if lo >= 0.5 and lo / hi >= cfg.news_gate_mixed_ratio and abs(news.gate_score) < 2 * cfg.news_gate_threshold:
        return "mixed"
    if news.gate_score >= cfg.news_gate_threshold:
        return "bullish"
    if news.gate_score <= -cfg.news_gate_threshold:
        return "bearish"
    return "neutral"


def news_gate_verdict(cfg: "Config", news: "NewsState", side: str, now: float) -> tuple[str, str]:
    """Pure pre-trade gate. Returns ("ok", detail) or ("skip:news_*", detail). Fails closed."""
    if not cfg.news_gate:
        return "ok", "gate off"
    if news.gate_fetched_at == 0 or now - news.gate_fetched_at > cfg.news_gate_max_age_seconds:
        return "skip:news_stale", f"last refresh {now - news.gate_fetched_at:.0f}s ago" if news.gate_fetched_at else "never fetched"
    if news.gate_sources < cfg.news_gate_min_sources:
        return "skip:news_sources", f"sources_ok={news.gate_sources}<{cfg.news_gate_min_sources}"
    if news.gate_relevant < cfg.news_gate_min_relevant or news.gate_scored < cfg.news_gate_min_scored:
        return "skip:news_thin", f"relevant={news.gate_relevant} scored={news.gate_scored}"
    label = news_gate_label(cfg, news)
    detail = f"{label} score={news.gate_score:+.3f} bull={news.gate_bull_w:.2f} bear={news.gate_bear_w:.2f}"
    if label in ("neutral", "mixed"):
        return f"skip:news_{label}", detail
    if (label == "bullish") == (side == "UP"):
        return "ok", detail
    return "skip:news_against", detail


def fetch_funding_8h() -> tuple[Optional[float], str]:
    """BTC perp funding as an 8h rate from public APIs that work without Binance/Coinbase/Kraken."""
    try:
        r = HTTP.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=8)
        r.raise_for_status()
        meta, ctxs = r.json()
        i = [a["name"] for a in meta["universe"]].index("BTC")
        return float(ctxs[i]["funding"]) * 8, "hyperliquid"     # hourly rate -> 8h
    except Exception as e:
        log.debug("hyperliquid funding failed: %s", e)
    try:
        d = get_json("https://www.deribit.com/api/v2/public/ticker", {"instrument_name": "BTC-PERPETUAL"})
        return float(d["result"]["funding_8h"]), "deribit"
    except Exception as e:
        log.debug("deribit funding failed: %s", e)
    return None, ""


def refresh_gate(ns: "NewsState", base_entries: list) -> None:
    """Fetch the gate's topic feeds, score them together with the general feeds, update ns in place."""
    entries = list(base_entries)
    ok = 1 if base_entries else 0
    for url in [u.strip() for u in CFG.news_gate_feeds.split(",") if u.strip()]:
        try:
            entries += _rss_items(url, titles_only=True)
            ok += 1
        except Exception as e:
            log.warning("news-gate feed failed %s: %s", url[:70], e)
    f8, fsrc = fetch_funding_8h() if CFG.news_gate_funding else (None, "")
    g = build_gate(CFG, entries, now_ts(), f8)
    for k, v in g.items():
        setattr(ns, k, v)
    ns.gate_sources, ns.gate_fetched_at = ok, (now_ts() if ok else 0.0)
    ns.funding_8h, ns.funding_source = f8, fsrc
    log.info("news-gate: %s score=%+.3f bull=%.2f bear=%.2f relevant=%d scored=%d sources_ok=%d funding8h=%s top=%s",
             news_gate_label(CFG, ns), ns.gate_score, ns.gate_bull_w, ns.gate_bear_w, ns.gate_relevant, ns.gate_scored,
             ok, f"{f8:+.5%}({fsrc})" if f8 is not None else "n/a", [h[3][:60] for h in ns.gate_headlines[:2]])


def refresh_news() -> NewsState:
    raw: list[tuple[str, float]] = []
    ok = 0
    titles: list[tuple[str, float]] = []
    for url in [u.strip() for u in CFG.rss_feeds.split(",") if u.strip()]:
        try:
            got = _rss_items(url)
            raw += got
            titles += [(t.split(". ")[0], ts) for t, ts in got]
            ok += 1
        except Exception as e:
            log.warning("news feed failed %s: %s", url[:60], e)
    api = _api_items()
    ok += 1 if api else 0
    raw += api
    horizon = now_ts() - 6 * 3600
    seen, items = set(), []
    for text, ts in raw:
        key = text[:80].lower()
        if ts >= horizon and key not in seen and any(k in text.lower() for k in BTC_TERMS):
            seen.add(key)
            items.append((text, min(ts, now_ts())))
    items.sort(key=lambda x: -x[1])
    items = items[:40]
    scores = _llm_scores([t[:200] for t, _ in items]) if items else None
    if scores is None:
        scores = [_keyword_score(t) for t, _ in items]
    num = den = 0.0
    for (text, ts), s in zip(items, scores):
        w = 0.5 ** (((now_ts() - ts) / 60.0) / CFG.news_half_life_minutes)
        num += s * w
        den += w
    sentiment = max(-1.0, min(1.0, num / den)) if den > 0 else 0.0
    # Shrink when there's little signal: one recent headline shouldn't swing the bot.
    sentiment *= min(1.0, den / 3.0)
    ns = NewsState(sentiment=round(sentiment, 3), n_items=len(raw), n_relevant=len(items),
                   top_headlines=[t[:120] for t, _ in items[:5]], fetched_at=now_ts(), ok_sources=ok)
    log.info("news: sentiment=%+.3f relevant=%d sources_ok=%d top=%s",
             ns.sentiment, ns.n_relevant, ok, ns.top_headlines[:2])
    if CFG.news_gate and CFG.strategy == "favorite":
        try:
            refresh_gate(ns, titles + [(t, ts) for t, ts in api])
        except Exception as e:      # fail closed: gate_fetched_at stays 0 -> skip:news_stale
            log.warning("news-gate refresh failed: %s", e)
    return ns


# =============================================================================
# SECTION 4: DECISION LOGIC
# =============================================================================
@dataclass
class Assessment:
    p_up: float
    reasons: list


def model_p_up(spot: float, ptb: float, closes: list, sigma: float, secs_left: float,
               twap_seconds: float, news_sent: float, cfg: "Config" = None) -> tuple[float, list]:
    """Pure model (shared by the live bot and backtest.py).

    Probability BTC finishes the window at/above the price to beat:
    z = (ln(S/S0) + drift*min(T,5)) / (sigma_1m * sqrt(T_eff)), T = minutes left, where T_eff
    accounts for settlement on a TWAP over the last L minutes. Small drift tilts from
    ~5-minute momentum and news sentiment; result shrunk toward 50%.
    """
    cfg = cfg or CFG
    T = max(secs_left / 60.0, 0.25)
    x = math.log(spot / ptb)
    mom = math.log(spot / closes[-6]) / 5.0 if len(closes) >= 6 else 0.0
    drift_mom = max(-0.5 * sigma, min(0.5 * sigma, cfg.momentum_weight * mom))
    drift_news = cfg.news_weight * news_sent * sigma * 0.5
    L = twap_seconds / 60.0
    eff_T = T if L <= 0 else (T - 2 * L / 3 if T >= L else T ** 3 / (3 * L * L))
    eff_T = max(eff_T, 0.05)
    z = (x + (drift_mom + drift_news) * min(T, 5.0)) / (sigma * math.sqrt(eff_T))
    p_raw = norm_cdf(z)
    p = 0.5 + (p_raw - 0.5) * cfg.model_shrink
    reasons = [f"S={spot:.2f} S0={ptb:.2f} move={x*1e4:+.1f}bp", f"sigma1m={sigma*1e4:.1f}bp T={T:.1f}m",
               f"mom={mom*1e4:+.2f}bp/m news={news_sent:+.2f}", f"z={z:+.2f} p_raw={p_raw:.3f} p={p:.3f}"]
    return p, reasons


def estimate_p_up(market: BtcMarket, snap: PriceSnapshot, ptb: float, news: NewsState) -> Assessment:
    spot = snap.spot * _BASIS.get(market.slug, 1.0)
    p, reasons = model_p_up(spot, ptb, snap.closes_1m, snap.sigma_1m, market.seconds_left,
                            market.twap_seconds, news.sentiment if CFG.use_news else 0.0)
    return Assessment(p_up=p, reasons=reasons)


def fee_per_share(price: float, rate: float) -> float:
    return rate * price * (1 - price)


def entry_gates(cfg: "Config", *, duration: str, secs_left: float, elapsed: float, p_side: float,
                bid: float, ask: float, fee_rate: float, news_lean: float = 0.0,
                momentum_reversing: bool = False) -> tuple[str, float]:
    """Pure entry gates shared by the live bot and backtest.py. Returns (verdict, edge)."""
    min_left = cfg.min_seconds_left_5m if duration == "5m" else cfg.min_seconds_left_15m
    if secs_left < min_left:
        return "skip:too_close_to_expiry", 0.0
    if getattr(cfg, "fav_exit_seconds_left", 0) > 0 and secs_left <= cfg.fav_exit_seconds_left:
        return "skip:inside_time_exit", 0.0
    if elapsed < cfg.min_elapsed_frac:
        return "skip:window_too_young", 0.0
    if ask - bid > cfg.max_spread:
        return "skip:wide_spread", 0.0
    if not (cfg.min_entry_price <= ask <= cfg.max_entry_price):
        return "skip:ask_out_of_range", 0.0
    edge = p_side - (ask + fee_per_share(ask, fee_rate) + cfg.slippage / 2)
    if p_side < cfg.min_model_prob:
        return "skip:low_conviction", edge
    if p_side - (bid + ask) / 2 > cfg.max_disagreement:
        return "skip:model_market_disagree", edge
    if edge < cfg.min_edge:
        return "skip:insufficient_edge", edge
    if news_lean < -cfg.news_veto:
        return "skip:news_against", edge
    if momentum_reversing:
        return "skip:momentum_reversing", edge
    return "ENTER", edge


@dataclass
class Signal:
    market: BtcMarket
    side: str            # "UP" | "DOWN"
    token_id: str
    ask: float
    p_side: float
    edge: float
    book: Book
    reasons: list


def favorite_signal(cfg: "Config", *, duration: str, secs_left: float, up_bid: float, up_ask: float,
                    down_bid: float, down_ask: float, fee_rate: float) -> tuple[str, str, float, float, float]:
    """Pure 'early favorite' rule shared by the live bot and backtest.py.

    Returns (verdict, side, p_side, ask, bid). Backtest (21 days, 2016 x 15m windows): favorites
    priced 85-95c with 10-15 min left won more often than their price implied (≈+3c/share before
    costs, ≈+2.5-4% per $ after fee+spread, IS and OOS). Small sample; treat as unproven.
    """
    if duration not in [d.strip() for d in cfg.fav_durations.split(",")]:
        return "skip:fav_duration", "", 0.0, 0.0, 0.0
    if not (cfg.fav_min_secs_left < secs_left <= cfg.fav_max_secs_left):
        return "skip:fav_time", "", 0.0, 0.0, 0.0
    if getattr(cfg, "fav_exit_seconds_left", 0) > 0 and secs_left <= cfg.fav_exit_seconds_left:
        return "skip:inside_time_exit", "", 0.0, 0.0, 0.0
    up_mid, down_mid = (up_bid + up_ask) / 2, (down_bid + down_ask) / 2
    side, bid, ask, mid = ("UP", up_bid, up_ask, up_mid) if up_mid >= down_mid else ("DOWN", down_bid, down_ask, down_mid)
    if ask - bid > cfg.max_spread:
        return "skip:wide_spread", side, 0.0, ask, bid
    if not (cfg.fav_min_price <= mid < cfg.fav_max_price) or ask > cfg.fav_max_price:
        return "skip:fav_price", side, 0.0, ask, bid
    p_side = min(0.995, mid + cfg.fav_edge_est)
    if p_side - ask - fee_per_share(ask, fee_rate) <= 0:
        return "skip:insufficient_edge", side, p_side, ask, bid
    return "ENTER", side, p_side, ask, bid


def decide(market: BtcMarket, snap: Optional[PriceSnapshot], news: NewsState) -> tuple[Optional[Signal], str, dict]:
    if CFG.strategy == "favorite":
        return decide_favorite(market, snap, news)
    return decide_model(market, snap, news)


def _common_gates(market: BtcMarket, snap: Optional[PriceSnapshot], news: NewsState, d: dict) -> Optional[str]:
    if not market.accepting_orders:
        return "skip:not_accepting_orders"
    if now_ts() < market.start_ts:
        return "skip:window_not_started"
    if snap is None or now_ts() - snap.fetched_at > 30:
        return "skip:no_fresh_price_data"
    if news.fetched_at == 0 or now_ts() - news.fetched_at > CFG.news_max_age_seconds or news.ok_sources == 0:
        return "skip:research_stale"   # rule 2: no fresh research, no trade
    return None


_GATE_LAST: dict = {}


def decide_favorite(market: BtcMarket, snap: Optional[PriceSnapshot], news: NewsState) -> tuple[Optional[Signal], str, dict]:
    d: dict[str, Any] = {"slug": market.slug, "secs_left": round(market.seconds_left), "elapsed": round(market.elapsed_frac, 2)}
    g = _common_gates(market, snap, news, d)
    if g:
        return None, g, d
    if not (CFG.fav_min_secs_left < market.seconds_left <= CFG.fav_max_secs_left) or \
            market.duration not in [x.strip() for x in CFG.fav_durations.split(",")]:
        return None, "skip:fav_time", d
    up, down = get_book(market.up_token), get_book(market.down_token)
    if not up or not down:
        return None, "skip:no_book", d
    verdict, side, p_side, ask, bid = favorite_signal(
        CFG, duration=market.duration, secs_left=market.seconds_left, up_bid=up.best_bid, up_ask=up.best_ask,
        down_bid=down.best_bid, down_ask=down.best_ask, fee_rate=market.fee_rate)
    d.update(side=side, bid=bid, ask=ask, p_up=round(p_side if side == "UP" else 1 - p_side, 4))
    if verdict != "ENTER":
        return None, verdict, d
    if CFG.use_news:
        lean = news.sentiment if side == "UP" else -news.sentiment
        if lean < -CFG.news_veto:
            return None, "skip:news_against", d
    gv, gdetail = news_gate_verdict(CFG, news, side, now_ts())
    d["news_gate"] = gdetail
    if _GATE_LAST.get(market.slug) != gv:          # log each decision change once, not every loop
        _GATE_LAST[market.slug] = gv
        log.info("news-gate %s for %s %s: %s", gv, market.slug, side, gdetail)
    if gv != "ok":
        return None, gv, d
    edge = p_side - ask - fee_per_share(ask, market.fee_rate)
    d["edge"] = round(edge, 4)
    book = up if side == "UP" else down
    token = market.up_token if side == "UP" else market.down_token
    return Signal(market, side, token, ask, p_side, edge, book, [f"favorite {side} mid={book.mid:.3f}"]), "ENTER", d


def decide_model(market: BtcMarket, snap: Optional[PriceSnapshot], news: NewsState) -> tuple[Optional[Signal], str, dict]:
    """v1 model strategy. Return (signal or None, verdict, details)."""
    d: dict[str, Any] = {"slug": market.slug, "secs_left": round(market.seconds_left), "elapsed": round(market.elapsed_frac, 2)}
    if not market.accepting_orders:
        return None, "skip:not_accepting_orders", d
    if now_ts() < market.start_ts:
        return None, "skip:window_not_started", d
    min_left = CFG.min_seconds_left_5m if market.duration == "5m" else CFG.min_seconds_left_15m
    if market.seconds_left < min_left:
        return None, "skip:too_close_to_expiry", d
    if market.elapsed_frac < CFG.min_elapsed_frac:
        return None, "skip:window_too_young", d
    if snap is None or now_ts() - snap.fetched_at > 30:
        return None, "skip:no_fresh_price_data", d
    if news.fetched_at == 0 or now_ts() - news.fetched_at > CFG.news_max_age_seconds or news.ok_sources == 0:
        return None, "skip:research_stale", d   # rule 2: no fresh research, no trade
    ptb, ptb_src = price_to_beat(market, snap)
    if not ptb:
        return None, "skip:no_price_to_beat", d
    d["ptb_src"] = ptb_src
    a = estimate_p_up(market, snap, ptb, news)
    d["p_up"] = round(a.p_up, 4)
    d["model"] = " | ".join(a.reasons)
    side = "UP" if a.p_up >= 0.5 else "DOWN"
    p_side = a.p_up if side == "UP" else 1 - a.p_up
    token = market.up_token if side == "UP" else market.down_token
    book = get_book(token)
    if book is None:
        return None, "skip:no_book", d
    d.update(side=side, bid=book.best_bid, ask=book.best_ask, spread=round(book.spread, 3))
    closes = snap.closes_1m
    reversing = False
    if len(closes) >= 4:
        recent = closes[-1] - closes[-4]
        lim = snap.spot * snap.sigma_1m * 2
        reversing = (side == "UP" and recent < -lim) or (side == "DOWN" and recent > lim)
    lean = (news.sentiment if side == "UP" else -news.sentiment) if CFG.use_news else 0.0
    verdict, edge = entry_gates(CFG, duration=market.duration, secs_left=market.seconds_left,
                                elapsed=market.elapsed_frac, p_side=p_side, bid=book.best_bid, ask=book.best_ask,
                                fee_rate=market.fee_rate, news_lean=lean, momentum_reversing=reversing)
    d["edge"] = round(edge, 4)
    if verdict != "ENTER":
        return None, verdict, d
    return Signal(market, side, token, book.best_ask, p_side, edge, book, a.reasons), "ENTER", d


# =============================================================================
# SECTION 5: ORDER EXECUTION
# =============================================================================
@dataclass
class Fill:
    ok: bool
    shares: float = 0.0
    avg_price: float = 0.0
    usd: float = 0.0          # cash out (buy) / cash in (sell), fees included
    fee: float = 0.0
    order_id: str = ""
    note: str = ""


class Executor:
    """Paper executor walks the real order book; live executor uses the official
    Polymarket unified Python SDK (`polymarket-client`, SecureClient)."""

    def __init__(self) -> None:
        self.client = None
        self.has_gasless_key = False      # Builder/Relayer key present -> gasless redeem possible
        if CFG.live:
            self._connect_live()

    @property
    def can_redeem(self) -> bool:
        return self.client is not None and self.has_gasless_key and CFG.auto_redeem

    def _connect_live(self) -> None:
        try:
            geo = get_json("https://polymarket.com/api/geoblock", timeout=5)
            if geo.get("blocked"):
                raise SystemExit(f"Polymarket reports trading is geoblocked from this IP ({geo.get('country')}). Not trading live.")
        except SystemExit:
            raise
        except Exception as e:
            log.warning("geoblock check failed (%s); continuing", e)
        if not CFG.private_key:
            raise SystemExit("Live mode needs POLYMARKET_PRIVATE_KEY (see README).")
        try:
            from polymarket import SecureClient  # pip install polymarket-client (Python >= 3.11)
        except ImportError as e:
            raise SystemExit(f"Live mode needs `pip install polymarket-client`: {e}")
        kwargs: dict[str, Any] = {"private_key": CFG.private_key}
        if CFG.wallet_address:
            kwargs["wallet"] = CFG.wallet_address
        if CFG.builder_api_key and CFG.builder_api_secret and CFG.builder_api_passphrase:
            from polymarket import BuilderApiKey
            kwargs["api_key"] = BuilderApiKey(key=CFG.builder_api_key, secret=CFG.builder_api_secret,
                                              passphrase=CFG.builder_api_passphrase)
        elif CFG.relayer_api_key and CFG.relayer_api_key_address:
            from polymarket import RelayerApiKey
            kwargs["api_key"] = RelayerApiKey(key=CFG.relayer_api_key, address=CFG.relayer_api_key_address)
        elif CFG.builder_api_key or CFG.relayer_api_key:
            log.warning("incomplete API key config: Builder needs KEY+SECRET+PASSPHRASE, Relayer needs KEY+ADDRESS; "
                        "auto-redeem disabled")
        self.has_gasless_key = "api_key" in kwargs
        self.client = SecureClient.create(**kwargs)
        log.warning("LIVE trading enabled. wallet=%s type=%s auto-redeem=%s", self.client.wallet,
                    self.client.wallet_type, "on" if self.can_redeem else "off (no Builder/Relayer key)")
        if "api_key" in kwargs:
            try:
                self.client.setup_trading_approvals()  # idempotent; only submits missing approvals
            except Exception as e:
                log.warning("setup_trading_approvals failed (orders will retry approvals): %s", e)

    def collateral_balance_usd(self) -> Optional[float]:
        if not self.client:
            return None
        try:
            ba = self.client.get_balance_allowance(asset_type="COLLATERAL")
            return ba.balance / 1e6   # pUSD has 6 decimals
        except Exception as e:
            log.warning("balance read failed: %s", e)
            return None

    def token_balance(self, token_id: str) -> Optional[float]:
        if not self.client:
            return None
        try:
            return self.client.get_balance_allowance(asset_type="CONDITIONAL", asset_id=token_id).balance / 1e6
        except Exception as e:
            log.warning("token balance read failed: %s", e)
            return None

    def redeem(self, condition_id: str) -> tuple[bool, str]:
        """Gasless redeem of a resolved market. Needs a Builder or Relayer API key (SecureClient api_key=)."""
        if not self.can_redeem:
            return False, "no Builder/Relayer API key configured"
        try:
            self.client.redeem_positions(condition_id=condition_id).wait()
            return True, "redeemed"
        except Exception as e:
            return False, str(e)

    # ---- resting limit orders (ENTRY_MODE=limit) ----
    def post_limit_buy(self, token_id: str, price: float, shares: float, expiration: Optional[int] = None) -> tuple[bool, str, str]:
        """Returns (ok, order_id, note). Paper: always accepted (the fill model decides later)."""
        if self.client is None:
            return True, f"paper-{token_id[-6:]}-{int(now_ts() * 1000)}", "paper resting bid"
        try:
            kw: dict[str, Any] = dict(token_id=token_id, price=f"{price:.4f}".rstrip("0").rstrip("."),
                                      size=f"{shares:.2f}", side="BUY", post_only=CFG.limit_post_only)
            if expiration:
                kw["expiration"] = int(expiration)
            resp = self.client.place_limit_order(**kw)
        except Exception as e:
            return False, "", f"order error: {e}"
        if not getattr(resp, "ok", False):
            return False, "", f"rejected {getattr(resp, 'code', '')}: {getattr(resp, 'message', '')}"
        return True, str(resp.order_id), f"live {getattr(resp, 'status', '')}"

    def order_matched(self, order_id: str) -> tuple[Optional[float], str]:
        """Live: (size_matched, status) for an order; (None, err) if it can't be read."""
        if self.client is None:
            return None, "paper"
        try:
            o = self.client.get_order(order_id=order_id)
            return float(o.size_matched), str(o.status)
        except Exception as e:
            return None, f"get_order failed: {e}"

    def cancel(self, order_id: str) -> tuple[bool, str]:
        if self.client is None:
            return True, "paper cancel"
        try:
            r = self.client.cancel_order(order_id=order_id)
            if order_id in tuple(getattr(r, "canceled", ()) or ()):
                return True, "canceled"
            nc = dict(getattr(r, "not_canceled", {}) or {})
            return False, f"not canceled: {nc.get(order_id, nc)}"
        except Exception as e:
            return False, f"cancel error: {e}"

    @staticmethod
    def _walk(levels: list, qty: float, by_usd: bool, limit: float, is_buy: bool) -> tuple[float, float]:
        """Return (shares, notional) fillable up to qty (USD for buys, shares for sells) within limit."""
        shares = notional = 0.0
        for price, size in levels:
            if (is_buy and price > limit) or (not is_buy and price < limit):
                break
            take = size
            if by_usd:
                take = min(size, (qty - notional) / price)
            else:
                take = min(size, qty - shares)
            shares += take
            notional += take * price
            if (by_usd and notional >= qty - 1e-9) or (not by_usd and shares >= qty - 1e-9):
                break
        return shares, notional

    def buy(self, market: BtcMarket, token_id: str, usd: float, max_price: float) -> Fill:
        if self.client is None:
            book = get_book(token_id)
            if not book:
                return Fill(False, note="no book")
            shares, notional = self._walk(book.asks, usd, True, max_price, True)
            if notional < usd * 0.999:   # FOK semantics: all or nothing
                return Fill(False, note=f"paper FOK not filled (only ${notional:.2f} within {max_price})")
            px = notional / shares
            fee = market.fee_rate * shares * px * (1 - px)   # taker fee charged on top
            return Fill(True, shares, px, notional + fee, fee, "paper", "paper fill")
        try:
            resp = self.client.place_market_order(asset_id=token_id, side="BUY", amount=round(usd, 2),
                                                  max_spend=round(usd, 2), max_price=round(max_price, 2),
                                                  order_type="FOK")
        except Exception as e:
            return Fill(False, note=f"order error: {e}")
        if not getattr(resp, "ok", False):
            return Fill(False, note=f"rejected {getattr(resp, 'code', '')}: {getattr(resp, 'message', '')}")
        spent, got = float(resp.making_amount), float(resp.taking_amount)
        if got <= 0:
            return Fill(False, order_id=resp.order_id, note=f"status={resp.status}, nothing filled")
        px = spent / got
        # The taker fee is charged on top of making_amount (first live fill: 4.71 matched, 4.8089 debited),
        # so count it in the cost basis or realized P&L is overstated by the fee. AcceptedOrder carries no
        # fee field (ok/order_id/status/making_amount/taking_amount/...), so use the fee formula.
        fee = got * fee_per_share(px, market.fee_rate)
        return Fill(True, got, px, spent + fee, fee, resp.order_id, f"live {resp.status}")

    def sell(self, market: BtcMarket, token_id: str, shares: float, min_price: float) -> Fill:
        if self.client is None:
            book = get_book(token_id)
            if not book:
                return Fill(False, note="no book")
            filled, notional = self._walk(book.bids, shares, False, min_price, False)
            if filled < shares * 0.999:
                return Fill(False, note=f"paper FOK not filled (bids too thin above {min_price})")
            px = notional / filled
            fee = market.fee_rate * filled * px * (1 - px)
            return Fill(True, filled, px, notional - fee, fee, "paper", "paper fill")
        held = self.token_balance(token_id)
        if held is not None:
            shares = min(shares, held)
        shares = math.floor(shares * 100) / 100
        if shares <= 0:
            return Fill(False, note="no shares held")
        try:
            resp = self.client.place_market_order(asset_id=token_id, side="SELL", shares=shares,
                                                  min_price=round(max(min_price, 0.01), 2), order_type="FAK")
        except Exception as e:
            return Fill(False, note=f"order error: {e}")
        if not getattr(resp, "ok", False):
            return Fill(False, note=f"rejected {getattr(resp, 'code', '')}: {getattr(resp, 'message', '')}")
        sold, got = float(resp.making_amount), float(resp.taking_amount)
        if sold <= 0:
            return Fill(False, order_id=resp.order_id, note=f"status={resp.status}, nothing filled")
        px = got / sold
        fee = sold * fee_per_share(px, market.fee_rate)   # estimate; the cash sync is the source of truth
        return Fill(True, sold, px, got - fee, fee, resp.order_id, f"live {resp.status}")


def limit_bid_price(cfg: "Config", *, mid: float, best_ask: float, tick: float) -> float:
    """Pure: resting bid = mid - offset, rounded DOWN to the tick, never at/above the ask (post-only would be
    rejected) and never above FAV_MAX_PRICE."""
    tick = tick or 0.01
    px = min(mid - cfg.limit_offset, best_ask - tick, cfg.fav_max_price)
    px = math.floor(px / tick + 1e-9) * tick
    return round(max(tick, px), 4)


def limit_order_shares(usd: float, price: float, min_shares: float) -> float:
    """Pure: shares for a USD budget at price (2 dp, rounded down); 0 if below the market's minimum size."""
    sh = math.floor(usd / price * 100 + 1e-9) / 100 if price > 0 else 0.0
    return sh if sh >= min_shares else 0.0


def fetch_recent_trades(condition_id: str, limit: int = 300) -> list:
    """Public data-api prints (taker side) for one market, newest first. Used by the paper fill model."""
    d = get_json("https://data-api.polymarket.com/trades",
                 {"market": condition_id, "limit": limit, "takerOnly": "true"})
    return d if isinstance(d, list) else []


def paper_fill_qty(cfg: "Config", o: "PendingOrder", book: Optional["Book"], trades: list,
                   now: float) -> tuple[float, str]:
    """Pure conservative paper fill model for a resting BUY at o.price. Returns (shares, why).
    - book crossed: asks at/below our bid would have traded with us -> fill up to that size;
    - prints THROUGH our price since we posted: sells of our token below our bid, or buys of the
      complementary token above 1 - bid (those match against our bid via minting) -> fill up to their size;
    - prints exactly AT our price: only PAPER_AT_PRICE_FILL_FRAC of their size (queue ahead of us)."""
    remaining = max(0.0, o.size - o.filled)
    if remaining <= 0:
        return 0.0, "done"
    q, why = 0.0, []
    if book and book.asks and book.best_ask <= o.price + 1e-9:
        crossed = sum(sz for px, sz in book.asks if px <= o.price + 1e-9)
        q += crossed
        why.append(f"book crossed ask={book.best_ask:.3f} sz={crossed:.1f}")
    seen = set(o.seen_tx)
    for t in trades:
        ts = float(t.get("timestamp", 0))
        if ts <= max(o.posted_at, 0) or ts < o.last_trade_ts:
            continue
        key = f"{t.get('transactionHash', '')}:{t.get('asset', '')}:{t.get('size', '')}"
        if key in seen:
            continue
        px, sz, side, asset = float(t.get("price", 0)), float(t.get("size", 0)), t.get("side", ""), t.get("asset", "")
        if asset == o.token_id and side == "SELL" and px < o.price - 1e-9:
            q += sz; why.append(f"sell@{px:.3f}<bid")
        elif asset == o.other_token_id and side == "BUY" and px > 1 - o.price + 1e-9:
            q += sz; why.append(f"opp-buy@{px:.3f}")
        elif cfg.paper_at_price_fill_frac > 0 and ((asset == o.token_id and side == "SELL" and abs(px - o.price) < 1e-9)
                                                   or (asset == o.other_token_id and side == "BUY"
                                                       and abs(px - (1 - o.price)) < 1e-9)):
            q += sz * cfg.paper_at_price_fill_frac; why.append(f"at-price@{px:.3f}x{cfg.paper_at_price_fill_frac}")
    return round(min(remaining, q), 4), "; ".join(why[:4])


# =============================================================================
# SECTION 7: RISK CONTROLS   (defined before position management, which uses it)
# =============================================================================
@dataclass
class Position:
    slug: str
    duration: str
    side: str
    token_id: str
    shares: float
    entry_price: float
    cost_usd: float
    opened_at: float
    end_ts: float
    start_ts: float
    fee_rate: float
    order_id: str = ""
    kind: str = "dir"        # "dir" (directional) or "arb" (one leg of a complete set, held to resolution)
    cash_at_end: Optional[float] = None   # live: cash seen when the window ended (detects an early redeem)

    @property
    def key(self) -> str:
        return self.slug if self.kind == "dir" else f"{self.slug}:arb:{self.side}"


@dataclass
class PendingOrder:
    """A resting limit BUY (ENTRY_MODE=limit). Fills are booked into State.positions as they happen."""
    slug: str
    duration: str
    side: str
    token_id: str
    other_token_id: str
    condition_id: str
    price: float
    size: float                   # shares ordered (current quote)
    filled: float = 0.0           # shares filled on the CURRENT quote
    total_filled: float = 0.0     # shares filled across all quotes in this window
    order_id: str = ""
    posted_at: float = 0.0
    end_ts: float = 0.0
    start_ts: float = 0.0
    fee_rate: float = 0.0
    tick: float = 0.01
    mid_at_post: float = 0.0
    reprices: int = 0
    target_shares: float = 0.0    # shares wanted in total this window
    last_trade_ts: float = 0.0    # paper: newest data-api print already examined
    seen_tx: list = field(default_factory=list)


@dataclass
class State:
    # bankroll_usd is ALWAYS the one consistent definition (see compute_bankroll):
    #   cash + open positions (at cost) + resolved-but-unredeemed winners ($1/share)
    # It is only ever written by PositionManager.refresh_bankroll(); peak/drawdown/day-start use it.
    bankroll_usd: float
    peak_bankroll_usd: float
    day: str
    day_start_bankroll_usd: float
    realized_pnl_day: float = 0.0
    realized_pnl_total: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    last_loss_at: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    traded_slugs: list = field(default_factory=list)
    positions: dict = field(default_factory=dict)
    cash_usd: float = 0.0          # live: pUSD from the CLOB balance call; paper: simulated cash
    # key -> {slug, side, token_id, condition_id, shares, payout, cost_usd, resolved_at, cash_at_resolve,
    #         last_redeem_try, manual_notice_logged}: winners that resolved but whose $1/share payout
    #         is not in cash yet. They count toward the bankroll until the redeem shows up.
    pending_redeem: dict = field(default_factory=dict)
    # slugs the user closed by hand from the dashboard: never re-entered (directional or arb)
    manual_closed_slugs: list = field(default_factory=list)
    # slug -> PendingOrder: resting limit entries (ENTRY_MODE=limit)
    pending_orders: dict = field(default_factory=dict)

    def save(self) -> None:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(STATE_FILE)

    @classmethod
    def from_dict(cls, raw: dict) -> "State":
        raw = dict(raw)
        pos_fields = set(Position.__dataclass_fields__)
        raw["positions"] = {k: Position(**{f: x for f, x in v.items() if f in pos_fields}) if isinstance(v, dict) else v
                            for k, v in raw.get("positions", {}).items()}
        po_fields = set(PendingOrder.__dataclass_fields__)
        raw["pending_orders"] = {k: PendingOrder(**{f: x for f, x in v.items() if f in po_fields})
                                 if isinstance(v, dict) else v for k, v in raw.get("pending_orders", {}).items()}
        known = set(cls.__dataclass_fields__)
        st = cls(**{k: v for k, v in raw.items() if k in known})
        if "cash_usd" not in raw:
            # pre-fix state files: paper bankroll_usd was cash; live gets re-synced right away anyway
            st.cash_usd = st.bankroll_usd if not CFG.live else max(0.0, st.bankroll_usd - sum(
                p.cost_usd for p in st.positions.values()))
        return st

    @classmethod
    def load(cls, starting_cash: float) -> "State":
        today = datetime.now(timezone.utc).date().isoformat()
        if STATE_FILE.exists():
            st = cls.from_dict(json.loads(STATE_FILE.read_text()))
            if CFG.live and starting_cash is not None:
                st.cash_usd = starting_cash      # bankroll_usd is recomputed by refresh_bankroll()
            return st
        return cls(bankroll_usd=starting_cash, peak_bankroll_usd=starting_cash, day=today,
                   day_start_bankroll_usd=starting_cash, cash_usd=starting_cash)


def held_value(shares: float, unit_value: float, onchain_shares: Optional[float], cash_now: float,
               cash_ref: Optional[float]) -> float:
    """Value of shares we still believe we hold. If the chain/CLOB says the tokens are gone (redeemed),
    or cash rose by ~the $1/share payout since cash_ref, they are worth 0 here (the cash already has them)."""
    if onchain_shares is not None:
        return max(0.0, min(shares, onchain_shares)) * unit_value
    if cash_ref is not None and cash_now - cash_ref >= 0.9 * shares:
        return 0.0
    return shares * unit_value


def compute_bankroll(cash: float, positions: dict, pending: dict,
                     balances: Optional[dict] = None, now: Optional[float] = None) -> tuple[float, dict]:
    """THE bankroll: cash + open positions at cost + resolved-unredeemed winners at $1/share.
    balances: token_id -> on-chain share balance (None/missing = unknown). Returns (bankroll, parts)."""
    balances = balances or {}
    now = now_ts() if now is None else now
    open_v = 0.0
    for p in positions.values():
        unit = p.cost_usd / p.shares if p.shares > 0 else 0.0
        if p.end_ts > now:
            open_v += p.cost_usd                 # window still live: tokens can't have been redeemed
        else:
            open_v += held_value(p.shares, unit, balances.get(p.token_id), cash, p.cash_at_end)
    pend_v = sum(held_value(e["shares"], 1.0, balances.get(e["token_id"]), cash, e.get("cash_at_resolve"))
                 for e in pending.values())
    return cash + open_v + pend_v, {"cash": cash, "open": open_v, "pending_redeem": pend_v}


class BankrollGuard:
    """Sanity guard for bankroll moves that no trade/resolve event explains (cash-read glitches, a lagging
    balance, deposits, withdrawals). Trade events are trusted immediately via note_event().
    An unexplained move bigger than max(min_usd, min_pct * bankroll) is NOT trusted (no new peak, no drawdown
    halt). If it reverts it was a glitch. If it persists for `window` seconds (and >= 2 reads) it is real but
    NOT a trading loss/gain: observe() reports it in `self.flow` so the caller can shift the peak and day-start
    baselines by that amount (a $3.50 withdrawal must never look like a 40% drawdown)."""

    def __init__(self, pct: float, window: float, min_usd: float = 0.25, min_pct: float = 0.03) -> None:
        self.pct, self.window = pct, window
        self.min_usd, self.min_pct = min_usd, min_pct
        self.ref: Optional[float] = None
        self.anomaly_since: Optional[float] = None
        self.anomaly_reads = 0
        self.flow = 0.0          # external cash flow accepted on the last observe() call (0 if none)

    def note_event(self, delta: float) -> None:
        """A trade/resolve event moved our own bookkeeping by delta: that move is expected."""
        if self.ref is not None:
            self.ref += delta

    def _tol(self, base: float) -> float:
        return max(self.min_usd, min(self.min_pct, self.pct) * abs(base))

    def observe(self, bank: float, now: float) -> bool:
        """Returns True if this bankroll value can be trusted for peak/drawdown this tick."""
        self.flow = 0.0
        if self.ref is None:
            self.ref, self.anomaly_since, self.anomaly_reads = bank, None, 0
            return True
        delta = bank - self.ref
        if abs(delta) <= self._tol(self.ref):
            self.ref, self.anomaly_since, self.anomaly_reads = bank, None, 0
            return True
        first = self.anomaly_since is None
        if first:
            self.anomaly_since = now
        self.anomaly_reads += 1
        if now - self.anomaly_since >= self.window and self.anomaly_reads >= 2:
            log.warning("bankroll move %.2f -> %.2f (%+.2f) persisted %.0fs with no trade event: booking it as an "
                        "external cash flow (deposit/withdrawal/manual activity), not a trading result; "
                        "peak and day-start shift by the same amount", self.ref, bank, delta, now - self.anomaly_since)
            self.flow = delta
            self.ref, self.anomaly_since, self.anomaly_reads = bank, None, 0
            return True
        if first:      # log once per anomaly, not every 5s tick
            base = max(abs(self.ref), 1e-9)
            log.warning("bankroll jumped %.2f -> %.2f (%+.0f%%) with no trade event; not trusted for up to %.0fs "
                        "(no new peak, drawdown halt skipped)", self.ref, bank, 100 * delta / base, self.window)
        return False


def size_usd(cfg: "Config", *, bank: float, p_side: float, price: float, total_exposure: float,
             same_dir_exposure: float, min_order_usd: float) -> tuple[float, str]:
    """Pure position sizing shared by the live bot and backtest.py."""
    kelly = max(0.0, (p_side - price) / (1 - price)) * cfg.kelly_fraction   # binary Kelly: (p-q)/(1-q)
    usd = min(cfg.max_position_pct * bank, cfg.max_position_usd, kelly * bank)
    usd = min(usd, cfg.max_total_btc_exposure_pct * bank - total_exposure)
    usd = min(usd, cfg.max_net_direction_pct * bank - max(0.0, same_dir_exposure))
    if usd < max(min_order_usd, 1.0):
        return 0.0, f"size ${usd:.2f} below minimum (${max(min_order_usd, 1.0):.2f}) / exposure caps"
    return round(usd, 2), "ok"


class Risk:
    def __init__(self, state: State) -> None:
        self.s = state

    def roll_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.s.day != today:
            log.info("new UTC day: resetting daily counters")
            self.s.day, self.s.day_start_bankroll_usd = today, self.s.bankroll_usd
            self.s.realized_pnl_day, self.s.trades_today = 0.0, 0
            self.s.consecutive_losses = 0
            if self.s.halted and (self.s.halt_reason.startswith("daily") or "consecutive" in self.s.halt_reason):
                self.s.halted, self.s.halt_reason = False, ""
            self.s.traded_slugs = self.s.traded_slugs[-200:]
            self.s.manual_closed_slugs = self.s.manual_closed_slugs[-200:]

    def check_kill_switches(self, bank_trusted: bool = True) -> Optional[str]:
        if Path(CFG.kill_switch_file).exists() or (DATA / CFG.kill_switch_file).exists():
            return "kill switch file present"
        if self.s.realized_pnl_day <= -CFG.daily_loss_limit_pct * self.s.day_start_bankroll_usd:
            return f"daily loss limit hit ({self.s.realized_pnl_day:.2f})"
        breach = self.s.bankroll_usd <= self.s.peak_bankroll_usd * (1 - CFG.max_drawdown_pct)
        if not bank_trusted:
            pass                      # untrusted read: neither counts toward nor resets the confirmation
        elif breach:
            self.dd_hits = getattr(self, "dd_hits", 0) + 1
            if self.dd_hits >= max(1, CFG.drawdown_confirm_reads):
                return f"max drawdown hit (bankroll {self.s.bankroll_usd:.2f} vs peak {self.s.peak_bankroll_usd:.2f})"
            log.warning("bankroll %.2f is %.0f%%+ below peak %.2f (read %d/%d before halting)", self.s.bankroll_usd,
                        100 * CFG.max_drawdown_pct, self.s.peak_bankroll_usd, self.dd_hits, CFG.drawdown_confirm_reads)
        else:
            self.dd_hits = 0
        if self.s.consecutive_losses >= CFG.max_consecutive_losses:
            return f"{self.s.consecutive_losses} consecutive losses"
        return None

    def exposure(self) -> tuple[float, float]:
        total = net = 0.0
        for p in self.s.positions.values():
            if p.kind != "dir":
                continue      # complete sets are market-neutral
            total += p.cost_usd
            net += p.cost_usd if p.side == "UP" else -p.cost_usd
        return total, net

    def size_for(self, sig: Signal) -> tuple[float, str]:
        """USD to spend on this signal, or (0, reason)."""
        if self.s.halted:
            return 0.0, f"halted: {self.s.halt_reason}"
        if self.s.trades_today >= CFG.max_trades_per_day:
            return 0.0, "max trades per day"
        if now_ts() - self.s.last_loss_at < CFG.cooldown_after_loss_seconds:
            return 0.0, "cooling down after a loss"
        if sig.market.slug in self.s.manual_closed_slugs:
            return 0.0, "closed manually from the dashboard (no re-entry this window)"
        if sig.market.slug in self.s.traded_slugs:
            return 0.0, "already traded this window"
        if sum(p.kind == "dir" for p in self.s.positions.values()) + \
                sum(1 for k in self.s.pending_orders if k not in self.s.positions) >= CFG.max_open_positions:
            return 0.0, "max open positions"
        bank = self.s.bankroll_usd
        total, net = self.exposure()
        # Kelly for a binary at price q with win prob p: f* = (p - q) / (1 - q)
        same_dir = net if sig.side == "UP" else -net      # existing exposure already leaning our way
        usd, why = size_usd(CFG, bank=bank, p_side=sig.p_side, price=sig.ask, total_exposure=total,
                            same_dir_exposure=same_dir, min_order_usd=sig.market.min_order_size * sig.ask)
        if usd <= 0:
            return 0.0, why
        if sig.book.ask_depth_usd < usd * 2:
            return 0.0, "book too thin for size"
        return round(usd, 2), "ok"

    def record_close(self, pnl: float, manual: bool = False) -> None:
        """Realized P&L always counts (daily loss limit, totals). A manual close from the dashboard is the
        user's decision, not a strategy outcome, so it leaves consecutive_losses and the post-loss cooldown
        untouched (neither incremented by a manual loss nor reset by a manual win)."""
        self.s.realized_pnl_day += pnl
        self.s.realized_pnl_total += pnl
        if manual:
            return
        if pnl < 0:
            self.s.consecutive_losses += 1
            self.s.last_loss_at = now_ts()
        else:
            self.s.consecutive_losses = 0


# =============================================================================
# SECTION 6: POSITION MANAGEMENT (profit-taking, stops, thesis exits)
# =============================================================================
def arb_check(cfg: "Config", *, up_ask: float, down_ask: float, up_ask_size: float, down_ask_size: float,
              fee_rate: float, min_shares: float) -> tuple[bool, float, float]:
    """Pure complete-set check. Returns (ok, edge_per_set, shares)."""
    cost = up_ask + down_ask + fee_per_share(up_ask, fee_rate) + fee_per_share(down_ask, fee_rate)
    edge = 1.0 - cost
    shares = min(up_ask_size, down_ask_size, cfg.arb_max_usd / cost)
    return (edge >= cfg.arb_min_edge and shares >= min_shares), edge, shares


def bk_is_up(m: BtcMarket, bk: Book) -> bool:
    return getattr(bk, "token_id", m.up_token) == m.up_token


def exit_reason(cfg: "Config", *, entry: float, bid: float, shares: float, cost_usd: float, fee_rate: float,
                secs_left: float, p_side: Optional[float]) -> Optional[tuple[str, str]]:
    """Pure exit rules shared by the live bot and backtest.py. Returns (kind, detail) or None."""
    exit_value = shares * bid * (1 - fee_rate * (1 - bid))
    profit = exit_value - cost_usd
    tp_price = min(entry * (1 + cfg.take_profit_pct), entry + cfg.take_profit_capture * (1 - entry))
    tp_price = max(tp_price, cfg.take_profit_min_bid)
    if bid >= tp_price and profit >= cfg.min_profit_usd:
        return "take_profit", f"bid={bid:.3f}>=tp={tp_price:.3f} pnl~{profit:+.2f}"
    drop = getattr(cfg, "fav_stop_drop_cents", 0.0)
    if drop > 0 and bid <= entry - drop / 100.0 + 1e-9:
        return "hard_stop", f"bid={bid:.3f}<=entry-{drop:g}c={entry - drop / 100.0:.3f}"
    if cfg.stop_loss_pct > 0 and bid <= entry * (1 - cfg.stop_loss_pct):
        return "stop_loss", f"bid={bid:.3f}"
    tx = getattr(cfg, "fav_exit_seconds_left", 0)
    if tx > 0 and secs_left <= tx:
        return "time_exit", f"{secs_left:.0f}s left<={tx}s"
    if cfg.force_exit_seconds > 0 and secs_left <= cfg.force_exit_seconds:
        return "pre_expiry_exit", f"{secs_left:.0f}s left"
    if cfg.thesis_exit_prob > 0 and p_side is not None and p_side < cfg.thesis_exit_prob:
        return "thesis_invalidated", f"p={p_side:.3f}"
    return None


def parse_close_requests(text: str) -> Optional[list[dict]]:
    """Parse close_requests.json. Accepts a list or {"requests": [...]}. Returns normalized requests
    ({id, slug, token_id, key, all, requested_at}); drops malformed entries; None if the file is garbage."""
    try:
        raw = json.loads(text) if text.strip() else []
    except (ValueError, TypeError):
        return None
    if isinstance(raw, dict):
        raw = raw.get("requests", [])
    if not isinstance(raw, list):
        return None
    out = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            ts = float(r.get("requested_at"))
        except (TypeError, ValueError):
            continue
        if ts != ts:
            continue
        req = {"id": str(r.get("id") or f"anon-{ts}"), "requested_at": ts, "all": r.get("all") is True}
        for k in ("slug", "token_id", "key"):
            v = r.get(k)
            req[k] = str(v) if isinstance(v, (str, int)) and str(v) else ""
        if req["all"] or req["slug"] or req["token_id"] or req["key"]:
            out.append(req)
    return out


def close_request_matches(req: dict, pos: "Position", state_key: str = "") -> bool:
    """all -> every position; key (state.json key) or token_id -> that one position; a bare slug (no key or
    token_id given) -> every position in that window."""
    if req.get("all"):
        return True
    if req.get("key") and req["key"] in (pos.key, state_key):
        return True
    if req.get("token_id") and req["token_id"] == str(pos.token_id):
        return True
    return bool(req.get("slug")) and req["slug"] == pos.slug and not req.get("key") and not req.get("token_id")


def load_close_results() -> list[dict]:
    try:
        raw = json.loads(CLOSE_RESULTS.read_text(encoding="utf-8-sig"))
        res = raw.get("results", []) if isinstance(raw, dict) else raw
        return [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []
    except Exception:
        return []


def save_close_results(results: list[dict]) -> None:
    tmp = CLOSE_RESULTS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"results": results[-50:]}, indent=2), encoding="utf-8")
    os.replace(tmp, CLOSE_RESULTS)


class PositionManager:
    def __init__(self, state: State, risk: Risk, ex: Executor) -> None:
        self.s, self.risk, self.ex = state, risk, ex
        self.guard = BankrollGuard(CFG.bank_jump_pct, CFG.bank_jump_window_seconds,
                                   CFG.bank_flow_min_usd, CFG.bank_flow_min_pct)
        self.balances: dict = {}      # token_id -> on-chain shares from the last refresh (live only)
        self.bank_trusted = True
        self.last_exit: dict = {}     # details of the most recent _close() fill (for manual-close results)
        # p_up used for thesis exits: the market's own price for the favorite strategy (backtest shows
        # the market is better calibrated than our model), the model otherwise.
        if CFG.strategy == "favorite":
            self.estimator = lambda m, sn, ptb, nw, bk: bk.mid if bk_is_up(m, bk) else 1 - bk.mid
        else:
            self.estimator = lambda m, sn, ptb, nw, bk: estimate_p_up(m, sn, ptb, nw).p_up

    # ---- bankroll bookkeeping (the ONLY place bankroll_usd / peak_bankroll_usd are written) ----
    def _recompute(self) -> float:
        bank, _ = compute_bankroll(self.s.cash_usd, self.s.positions, self.s.pending_redeem, self.balances)
        self.s.bankroll_usd = bank
        return bank

    def _event(self, mutate) -> None:
        """Apply a trade/resolve mutation and tell the guard how much our own books moved."""
        before = self._recompute()
        mutate()
        self.guard.note_event(self._recompute() - before)

    def refresh_bankroll(self, cash: Optional[float] = None, now: Optional[float] = None) -> bool:
        """Sync cash (live), detect redeemed tokens, recompute the bankroll, update the peak only
        from a trusted value. Returns True if the bankroll can be used for the drawdown check."""
        now = now_ts() if now is None else now
        if cash is not None:
            self.s.cash_usd = cash
        if self.ex.client is not None:
            for pos in list(self.s.positions.values()):
                if pos.end_ts <= now:
                    if pos.cash_at_end is None:
                        pos.cash_at_end = self.s.cash_usd
                    self.balances[pos.token_id] = self.ex.token_balance(pos.token_id)
                    self._maybe_close_redeemed(pos, now)
            for key, e in list(self.s.pending_redeem.items()):
                bal = self.ex.token_balance(e["token_id"])
                self.balances[e["token_id"]] = bal
                self._maybe_clear_pending(key, e, bal, now)
            self._maybe_redeem(now)
        bank, parts = compute_bankroll(self.s.cash_usd, self.s.positions, self.s.pending_redeem, self.balances, now)
        self.s.bankroll_usd = bank
        self.bank_trusted = self.guard.observe(bank, now)
        if self.guard.flow:
            flow = self.guard.flow
            old_peak, old_day = self.s.peak_bankroll_usd, self.s.day_start_bankroll_usd
            self.s.peak_bankroll_usd = max(0.0, old_peak + flow)
            self.s.day_start_bankroll_usd = max(0.01, old_day + flow)
            log.warning("external cash flow %+.2f: peak %.2f -> %.2f, day start %.2f -> %.2f", flow, old_peak,
                        self.s.peak_bankroll_usd, old_day, self.s.day_start_bankroll_usd)
        if self.bank_trusted:
            self.s.peak_bankroll_usd = max(self.s.peak_bankroll_usd, bank)
        log.debug("bankroll %.2f = cash %.2f + open %.2f + pending_redeem %.2f (trusted=%s)", bank,
                  parts["cash"], parts["open"], parts["pending_redeem"], self.bank_trusted)
        return self.bank_trusted

    def _maybe_close_redeemed(self, pos: Position, now: float) -> None:
        """Live: Polymarket auto-redeems winners minutes before Gamma flips `closed`. A directional position
        whose window ended, whose tokens are gone on-chain AND whose cash rose by ~its $1/share payout since
        the window ended has been paid out: book it as a resolved win now (CLOSED reason=resolved_win, a
        RESOLVE row in trades.csv, P&L = payout - cost). Otherwise it would sit in `positions` (worth $0,
        blocking new entries via max-open-positions) until Gamma catches up."""
        if pos.kind != "dir" or pos.cash_at_end is None:
            return       # arb legs: both legs' tokens burn on redeem -> wait for Gamma's outcome instead
        bal = self.balances.get(pos.token_id)
        if bal is None or bal >= 1e-6:
            return
        if self.s.cash_usd - pos.cash_at_end >= 0.9 * pos.shares:
            log.info("%s: tokens redeemed and cash rose by the $%.2f payout -> booking the win", pos.slug, pos.shares)
            self.resolve(pos, 1.0, reason="resolved_win")
            # The guard's reference still holds the position at cost; the payout that just landed in cash is
            # this trade's result (payout - cost), not an unexplained jump.
            self.guard.note_event(pos.shares - pos.cost_usd)

    def _maybe_clear_pending(self, key: str, e: dict, bal: Optional[float], now: float) -> None:
        """A pending winner is redeemed once cash rose by ~its payout, or its tokens have been gone for
        2+ minutes (so a lagging cash balance can't make the bankroll dip by the payout)."""
        cash_up = self.s.cash_usd - e.get("cash_at_resolve", self.s.cash_usd) >= 0.9 * e["payout"]
        if bal is not None and bal < 1e-6:
            e.setdefault("zero_since", now)
        else:
            e.pop("zero_since", None)
        gone = "zero_since" in e and now - e["zero_since"] >= 120
        if cash_up or gone:
            self.s.pending_redeem.pop(key, None)
            log.info("redeem confirmed for %s (%s): $%.2f now in cash", e["slug"],
                     "cash rose by the payout" if cash_up else "tokens gone", e["payout"])

    def _maybe_redeem(self, now: float) -> None:
        for key, e in self.s.pending_redeem.items():
            if not self.ex.can_redeem:
                if not e.get("manual_notice_logged"):
                    log.warning("%s: winning %.4f %s shares ($%.2f) need redeeming. No Builder/Relayer API key "
                                "is set, so redeem them in the Polymarket UI (Portfolio > Claim). They keep "
                                "counting toward the bankroll until the payout shows up in cash.",
                                e["slug"], e["shares"], e["side"], e["payout"])
                    e["manual_notice_logged"] = True
                continue
            if not e.get("condition_id") or now - e.get("last_redeem_try", 0) < CFG.redeem_retry_seconds:
                continue
            if self.balances.get(e["token_id"]) == 0:
                continue          # already redeemed (e.g. by Polymarket); waiting for cash to confirm
            e["last_redeem_try"] = now
            ok, note = self.ex.redeem(e["condition_id"])
            if ok:
                log.info("auto-redeem submitted for %s ($%.2f)", e["slug"], e["payout"])
            else:
                log.warning("auto-redeem failed for %s: %s (retrying every %ds; or redeem in the Polymarket UI)",
                            e["slug"], note, CFG.redeem_retry_seconds)

    def open(self, sig: Signal, usd: float) -> None:
        max_price = min(0.99, sig.ask + CFG.slippage)
        fill = self.ex.buy(sig.market, sig.token_id, usd, max_price)
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "BUY",
                                "slug": sig.market.slug, "side": sig.side, "ok": fill.ok, "shares": round(fill.shares, 4),
                                "price": round(fill.avg_price, 4), "usd": round(fill.usd, 4), "fee": round(fill.fee, 4),
                                "pnl": "", "reason": f"p={sig.p_side:.3f} edge={sig.edge:.3f}", "note": fill.note})
        if not fill.ok:
            log.info("entry not filled %s: %s", sig.market.slug, fill.note)
            return
        m = sig.market

        def mutate() -> None:
            self.s.positions[m.slug] = Position(m.slug, m.duration, sig.side, sig.token_id, fill.shares,
                                                fill.avg_price, fill.usd, now_ts(), m.end_ts, m.start_ts,
                                                m.fee_rate, fill.order_id)
            self.s.cash_usd -= fill.usd     # live: local estimate until the next cash sync
        self._event(mutate)
        self.s.traded_slugs.append(m.slug)
        self.s.trades_today += 1
        self.s.save()
        log.info("OPENED %s %s %.2f sh @ %.3f ($%.2f)", m.slug, sig.side, fill.shares, fill.avg_price, fill.usd)

    # ---- resting limit entries (ENTRY_MODE=limit) ----
    def open_limit(self, sig: Signal, usd: float) -> bool:
        """Post a resting post-only BUY at mid - LIMIT_OFFSET for the favorite. Returns True if posted."""
        m, book = sig.market, sig.book
        price = limit_bid_price(CFG, mid=book.mid, best_ask=book.best_ask, tick=m.tick_size)
        shares = limit_order_shares(min(usd, CFG.max_position_usd), price, m.min_order_size)
        if shares <= 0:
            log.info("limit entry %s skipped: $%.2f at %.3f is below the %.0f-share minimum", m.slug, usd, price,
                     m.min_order_size)
            return False
        other = m.down_token if sig.token_id == m.up_token else m.up_token
        o = PendingOrder(m.slug, m.duration, sig.side, sig.token_id, other, m.condition_id, price, shares,
                         posted_at=now_ts(), end_ts=m.end_ts, start_ts=m.start_ts, fee_rate=m.fee_rate,
                         tick=m.tick_size, mid_at_post=book.mid, target_shares=shares, last_trade_ts=now_ts())
        if not self._post(o, m):
            return False
        self.s.pending_orders[m.slug] = o
        self.s.traded_slugs.append(m.slug)       # one entry attempt per window (reprices reuse it)
        self.s.save()
        return True

    def _post(self, o: PendingOrder, m: Optional[BtcMarket]) -> bool:
        exp = int(max(now_ts() + 200, o.end_ts - CFG.limit_cancel_secs_left + 30))   # GTD safety net if we crash
        ok, oid, note = self.ex.post_limit_buy(o.token_id, o.price, o.size, expiration=exp if CFG.live else None)
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "LIMIT_POST",
                                "slug": o.slug, "side": o.side, "ok": ok, "shares": round(o.size, 4),
                                "price": round(o.price, 4), "usd": round(o.size * o.price, 4), "fee": 0,
                                "pnl": "", "reason": f"mid={o.mid_at_post:.3f} reprice#{o.reprices}", "note": note})
        if not ok:
            log.warning("limit post failed %s: %s", o.slug, note)
            return False
        o.order_id, o.posted_at, o.filled = oid, now_ts(), 0.0
        log.info("LIMIT POSTED %s %s %.2f sh @ %.3f (mid %.3f) id=%s", o.slug, o.side, o.size, o.price,
                 o.mid_at_post, oid[:16])
        return True

    def _book_fill(self, o: PendingOrder, qty: float, why: str) -> None:
        """Book qty shares filled at our limit price (maker: no fee) into the window's position."""
        if qty <= 1e-9:
            return
        usd = qty * o.price
        first = o.total_filled <= 1e-9
        o.filled += qty
        o.total_filled += qty

        def mutate() -> None:
            pos = self.s.positions.get(o.slug)
            if pos is None:
                self.s.positions[o.slug] = Position(o.slug, o.duration, o.side, o.token_id, qty, o.price, usd,
                                                    now_ts(), o.end_ts, o.start_ts, o.fee_rate, o.order_id)
            else:
                pos.shares += qty
                pos.cost_usd += usd
                pos.entry_price = pos.cost_usd / pos.shares
            self.s.cash_usd -= usd
        self._event(mutate)
        if first:
            self.s.trades_today += 1
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "LIMIT_FILL",
                                "slug": o.slug, "side": o.side, "ok": True, "shares": round(qty, 4),
                                "price": round(o.price, 4), "usd": round(usd, 4), "fee": 0, "pnl": "",
                                "reason": f"filled {o.total_filled:.2f}/{o.target_shares:.2f}", "note": why[:200]})
        log.info("LIMIT FILL %s %s +%.2f sh @ %.3f (%.2f/%.2f) %s", o.slug, o.side, qty, o.price, o.total_filled,
                 o.target_shares, why[:120])
        self.s.save()

    def _sync_fills(self, o: PendingOrder, book: Optional[Book]) -> None:
        if self.ex.client is not None:
            matched, status = self.ex.order_matched(o.order_id)
            if matched is not None and matched > o.filled + 1e-9:
                self._book_fill(o, min(matched, o.size) - o.filled, f"live size_matched={matched} {status}")
            return
        try:
            trades = fetch_recent_trades(o.condition_id)
        except Exception as e:
            log.debug("paper trades fetch failed %s: %s", o.slug, e)
            trades = []
        qty, why = paper_fill_qty(CFG, o, book, trades, now_ts())
        seen = set(o.seen_tx)
        for t in trades:
            ts = float(t.get("timestamp", 0))
            key = f"{t.get('transactionHash', '')}:{t.get('asset', '')}:{t.get('size', '')}"
            if ts > o.posted_at and key not in seen:
                seen.add(key)
                o.seen_tx.append(key)
                o.last_trade_ts = max(o.last_trade_ts, ts)
        o.seen_tx = o.seen_tx[-500:]
        self._book_fill(o, qty, why)

    def _cancel_pending(self, o: PendingOrder, reason: str, keep: bool = False) -> bool:
        """Cancel the resting remainder. keep=True leaves the PendingOrder in state (used for reprices)."""
        ok, note = self.ex.cancel(o.order_id)
        if not ok and self.ex.client is not None:
            matched, status = self.ex.order_matched(o.order_id)
            if matched is not None and matched > o.filled + 1e-9:
                self._book_fill(o, min(matched, o.size) - o.filled, f"matched before cancel ({status})")
            if status.upper() not in ("MATCHED", "CANCELED", "CANCELLED", "EXPIRED", "INVALID"):
                log.warning("cancel failed for %s (%s); will retry", o.slug, note)
                return False
        elif self.ex.client is not None:      # catch fills that landed between our last poll and the cancel
            matched, _ = self.ex.order_matched(o.order_id)
            if matched is not None and matched > o.filled + 1e-9:
                self._book_fill(o, min(matched, o.size) - o.filled, "matched before cancel")
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "LIMIT_CANCEL",
                                "slug": o.slug, "side": o.side, "ok": True, "shares": round(o.size - o.filled, 4),
                                "price": round(o.price, 4), "usd": "", "fee": 0, "pnl": "",
                                "reason": reason, "note": f"filled {o.total_filled:.2f}/{o.target_shares:.2f}"})
        log.info("LIMIT CANCEL %s (%s) filled %.2f/%.2f", o.slug, reason, o.total_filled, o.target_shares)
        if not keep:
            self.s.pending_orders.pop(o.slug, None)
        self.s.save()
        return True

    def cancel_all_orders(self, reason: str) -> None:
        for o in list(self.s.pending_orders.values()):
            self._cancel_pending(o, reason)

    def manage_orders(self, markets: dict, now: Optional[float] = None) -> None:
        """Poll fills, then cancel / reprice resting entries."""
        now = now_ts() if now is None else now
        for o in list(self.s.pending_orders.values()):
            book = get_book(o.token_id)
            self._sync_fills(o, book)
            if o.total_filled >= o.target_shares - 1e-6 or o.filled >= o.size - 1e-6:
                log.info("LIMIT DONE %s filled %.2f/%.2f sh @ %.3f", o.slug, o.total_filled, o.target_shares, o.price)
                self.s.pending_orders.pop(o.slug, None)
                self.s.save()
                continue
            left = o.end_ts - now
            if left <= CFG.limit_cancel_secs_left:
                self._cancel_pending(o, f"{left:.0f}s left <= {CFG.limit_cancel_secs_left}")
                continue
            if self.s.halted:
                self._cancel_pending(o, f"halted: {self.s.halt_reason}")
                continue
            if not book:
                continue
            mid = book.mid
            if CFG.limit_cancel_out_of_band and not (CFG.fav_min_price <= mid < CFG.fav_max_price):
                self._cancel_pending(o, f"mid {mid:.3f} left band {CFG.fav_min_price}-{CFG.fav_max_price}")
                continue
            if CFG.limit_max_reprices > 0 and o.reprices < CFG.limit_max_reprices and \
                    abs(mid - o.mid_at_post) > CFG.limit_reprice_cents / 100 + 1e-9:
                new_px = limit_bid_price(CFG, mid=mid, best_ask=book.best_ask, tick=o.tick)
                if abs(new_px - o.price) < 1e-9:
                    continue
                if not self._cancel_pending(o, f"reprice: mid {o.mid_at_post:.3f}->{mid:.3f}", keep=True):
                    continue
                mkt = markets.get(o.slug)
                min_sh = mkt.min_order_size if mkt else 5.0
                pos = self.s.positions.get(o.slug)
                budget = CFG.max_position_usd - (pos.cost_usd if pos else 0.0)
                remaining = min(round(o.target_shares - o.total_filled, 2), limit_order_shares(budget, new_px, min_sh))
                if remaining < min_sh:        # what's left is below the market minimum: stop quoting
                    log.info("LIMIT %s: rest %.2f sh below minimum at %.3f; not re-quoting", o.slug, remaining, new_px)
                    self.s.pending_orders.pop(o.slug, None)
                    self.s.save()
                    continue
                o.target_shares = o.total_filled + remaining
                o.price, o.size, o.mid_at_post, o.reprices = new_px, remaining, mid, o.reprices + 1
                if not self._post(o, mkt):
                    self.s.pending_orders.pop(o.slug, None)
                self.s.save()

    def try_arbitrage(self, m: BtcMarket) -> None:
        """Complete-set arbitrage: if Up ask + Down ask + taker fees < $1 - ARB_MIN_EDGE, buy equal shares
        of both; exactly one side pays $1 at resolution. Rarely available (books are usually >= $1.01)."""
        if not CFG.arb_enabled or m.slug in self.s.manual_closed_slugs \
                or any(k.startswith(m.slug + ":arb") for k in self.s.positions):
            return
        up, down = get_book(m.up_token), get_book(m.down_token)
        if not up or not down:
            return
        ok, edge, n = arb_check(CFG, up_ask=up.best_ask, down_ask=down.best_ask, up_ask_size=up.asks[0][1],
                                down_ask_size=down.asks[0][1], fee_rate=m.fee_rate, min_shares=m.min_order_size)
        if not ok:
            return
        log.info("ARB %s: up_ask=%.3f down_ask=%.3f edge/set=%.4f shares=%.1f", m.slug, up.best_ask, down.best_ask, edge, n)
        append_csv(DECISIONS_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "slug": m.slug,
                                   "verdict": "ARB", "secs_left": round(m.seconds_left), "side": "BOTH", "p_up": "",
                                   "bid": "", "ask": f"{up.best_ask}+{down.best_ask}", "edge": round(edge, 4),
                                   "ptb_src": "", "model": "", "note": f"shares={n:.1f}"})
        legs = []
        for side, token, book in (("UP", m.up_token, up), ("DOWN", m.down_token, down)):
            fill = self.ex.buy(m, token, round(n * book.best_ask, 2), book.best_ask)
            append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "BUY_ARB",
                                    "slug": m.slug, "side": side, "ok": fill.ok, "shares": round(fill.shares, 4),
                                    "price": round(fill.avg_price, 4), "usd": round(fill.usd, 4),
                                    "fee": round(fill.fee, 4), "pnl": "", "reason": f"edge/set={edge:.4f}",
                                    "note": fill.note})
            if not fill.ok:
                log.warning("arb leg %s failed (%s); unwinding", side, fill.note)
                for pos in legs:
                    self._close(pos, "arb_unwind", 0.01, m)
                return
            pos = Position(m.slug, m.duration, side, token, fill.shares, fill.avg_price, fill.usd, now_ts(),
                           m.end_ts, m.start_ts, m.fee_rate, fill.order_id, kind="arb")

            def mutate(pos: Position = pos, usd: float = fill.usd) -> None:
                self.s.positions[pos.key] = pos
                self.s.cash_usd -= usd
            self._event(mutate)
            legs.append(pos)
        self.s.save()

    def _close(self, pos: Position, reason: str, min_price: float, mkt: Optional[BtcMarket],
               manual: bool = False) -> bool:
        fake = mkt or BtcMarket(pos.slug, pos.duration, "", "", pos.start_ts, pos.end_ts, pos.token_id, pos.token_id,
                                0.01, 5, pos.fee_rate, True, "v1")
        fill = self.ex.sell(fake, pos.token_id, pos.shares, min_price)
        pnl = fill.usd - pos.cost_usd if fill.ok else 0.0
        self.last_exit = {"ok": fill.ok, "shares": round(fill.shares, 4), "price": round(fill.avg_price, 4),
                          "proceeds": round(fill.usd, 4), "fee": round(fill.fee, 4),
                          "pnl": round(pnl, 4) if fill.ok else None, "note": fill.note}
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "SELL",
                                "slug": pos.slug, "side": pos.side, "ok": fill.ok, "shares": round(fill.shares, 4),
                                "price": round(fill.avg_price, 4), "usd": round(fill.usd, 4), "fee": round(fill.fee, 4),
                                "pnl": round(pnl, 4) if fill.ok else "", "reason": reason, "note": fill.note})
        if not fill.ok:
            log.warning("exit failed %s (%s): %s", pos.slug, reason, fill.note)
            return False
        self._settle(pos, fill.usd, reason, manual=manual)
        return True

    def _settle(self, pos: Position, proceeds: float, reason: str, manual: bool = False) -> None:
        """Close a sold position: proceeds go to cash (live: a local estimate until the next cash sync).
        Never touches peak_bankroll_usd; refresh_bankroll() does that from the consistent bankroll."""
        pnl = proceeds - pos.cost_usd

        def mutate() -> None:
            self.s.positions.pop(pos.key, None)
            self.s.cash_usd += proceeds
        self._event(mutate)
        self.risk.record_close(pnl, manual=manual)
        self.s.save()
        log.info("CLOSED %s %s reason=%s pnl=%+.2f bankroll=%.2f", pos.slug, pos.side, reason, pnl, self.s.bankroll_usd)

    def _settle_at_resolution(self, pos: Position) -> None:
        """Position held past the end (no bids). Paper: settle from Gamma's resolved prices."""
        try:
            ev = get_json(f"{CFG.gamma_url}/events", {"slug": pos.slug})
            m = ev[0]["markets"][0]
            prices = [float(x) for x in json.loads(m["outcomePrices"])]
            outcomes = [o.lower() for o in json.loads(m["outcomes"])]
            p = prices[outcomes.index(pos.side.lower())]
            # Gamma can take 10+ minutes to flip `closed` (and UMA status stays empty) after the window ends,
            # while Polymarket has already auto-redeemed. Settle once the outcome is effectively decided
            # (>=0.99 / <=0.01) a minute past the end. Live winners whose tokens are still held go to
            # pending_redeem (valued $1/share until the payout lands in cash), so nothing is double-counted.
            if not m.get("closed") and now_ts() > pos.end_ts + 60 and (p >= 0.99 or p <= 0.01):
                p = 1.0 if p >= 0.99 else 0.0
                m["closed"] = True
            if m.get("closed") and p in (0.0, 1.0):
                self.resolve(pos, p, m.get("conditionId", ""))
        except Exception as e:
            log.debug("resolution check %s: %s", pos.slug, e)

    def resolve(self, pos: Position, p: float, condition_id: str = "", reason: str = "") -> None:
        """Book a resolved position. Live winners whose tokens are still on-chain go to pending_redeem
        (still worth $1/share); if the tokens are already gone, the payout is already in the synced cash."""
        if self.s.positions.get(pos.key) is not pos:
            log.debug("resolve %s: already booked, ignoring", pos.key)
            return          # idempotent: never book the same position twice
        pending = None
        payout = pos.shares * p
        if CFG.live and p == 1.0:
            bal = self.ex.token_balance(pos.token_id) if self.ex.client is not None else None
            already = (bal is not None and bal < 1e-6) or (
                bal is None and pos.cash_at_end is not None and self.s.cash_usd - pos.cash_at_end >= 0.9 * pos.shares)
            if already:
                log.info("%s resolved WIN and was already redeemed (payout already in cash)", pos.slug)
            else:
                pending = {"slug": pos.slug, "side": pos.side, "token_id": pos.token_id,
                           "condition_id": condition_id, "shares": pos.shares, "payout": round(payout, 6),
                           "cost_usd": pos.cost_usd, "resolved_at": now_ts(), "cash_at_resolve": self.s.cash_usd,
                           "last_redeem_try": 0.0, "manual_notice_logged": False}
        # Paper: the payout goes straight to simulated cash. Live: never add it to cash locally -- either it
        # is parked in pending_redeem, or it was already redeemed (synced cash has it), or it is a loser ($0).
        proceeds_to_cash = 0.0 if CFG.live else payout
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "RESOLVE",
                                "slug": pos.slug, "side": pos.side, "ok": True, "shares": pos.shares, "price": p,
                                "usd": round(payout, 4), "fee": 0, "pnl": round(payout - pos.cost_usd, 4),
                                "reason": reason or ("resolved_win" if p == 1.0 else "resolved_loss" if p == 0.0
                                                     else "resolution"),
                                "note": "pending redeem" if pending else ("already redeemed" if CFG.live and p == 1.0
                                                                          else "")})
        pnl = payout - pos.cost_usd

        def mutate() -> None:
            self.s.positions.pop(pos.key, None)
            if pending is not None:
                self.s.pending_redeem[pos.key] = pending
            self.s.cash_usd += proceeds_to_cash
        self._event(mutate)
        self.risk.record_close(pnl)
        self.s.save()
        why = reason or ("resolved_win" if p == 1.0 else "resolved_loss" if p == 0.0 else "resolved")
        log.info("CLOSED %s %s reason=%s pnl=%+.2f bankroll=%.2f%s", pos.slug, pos.side, why, pnl,
                 self.s.bankroll_usd, " (payout pending redeem)" if pending else "")
        if pending is not None:
            self._maybe_redeem(now_ts())

    # ---- manual close requests from the dashboard (file-based; see dashboard/server.py) ----
    def process_close_requests(self, markets: dict[str, BtcMarket], now: Optional[float] = None) -> int:
        """Sell positions the user asked to close from the dashboard, through the same _close() market exit
        as stop-loss/take-profit (same fee + bookkeeping, a SELL row in trades.csv, `CLOSED ... reason=
        manual_close` in the log). Results go to close_results.json keyed by request id. Never raises.
        Returns the number of positions closed."""
        try:
            return self._process_close_requests(markets, now_ts() if now is None else now)
        except Exception as e:      # a bad request file must never take the bot down
            log.exception("manual close: unexpected error (ignored): %s", e)
            return 0

    def _process_close_requests(self, markets: dict[str, BtcMarket], now: float) -> int:
        if not CLOSE_REQUESTS.exists():
            return 0
        work = CLOSE_REQUESTS.with_suffix(".processing")
        try:
            os.replace(CLOSE_REQUESTS, work)       # claim the file atomically; the dashboard may write a new one
        except OSError as e:
            log.warning("manual close: cannot claim %s: %s", CLOSE_REQUESTS.name, e)
            return 0
        reqs = parse_close_requests(work.read_text(encoding="utf-8-sig", errors="replace"))
        work.unlink(missing_ok=True)
        if reqs is None:
            log.warning("manual close: %s was malformed; ignored", CLOSE_REQUESTS.name)
            return 0
        results = load_close_results()
        done_ids = {r.get("id") for r in results}
        closed = 0
        for req in reqs:
            if req["id"] in done_ids:
                continue                           # already handled (idempotent re-delivery)
            res = {"id": req["id"], "slug": req.get("slug") or "", "requested_at": req["requested_at"],
                   "handled_at": now, "positions": []}
            age = now - req["requested_at"]
            targets = [p for k, p in list(self.s.positions.items()) if close_request_matches(req, p, k)]
            if age > CFG.manual_close_ttl_seconds:
                res.update(status="expired", note=f"request was {age:.0f}s old (limit {CFG.manual_close_ttl_seconds:.0f}s)")
            elif not targets:
                res.update(status="not_open", note="no matching open position")
            else:
                outcomes = []
                for pos in targets:
                    o = self._manual_close_one(pos, markets, now)
                    outcomes.append(o["status"])
                    res["positions"].append(o)
                    closed += o["status"] == "closed"
                res["status"] = ("closed" if all(s == "closed" for s in outcomes)
                                 else "partial" if "closed" in outcomes else outcomes[0])
                res["note"] = "; ".join(o.get("note", "") for o in res["positions"] if o.get("note"))
                res["pnl"] = round(sum(o.get("pnl") or 0.0 for o in res["positions"]), 4)
            if res["status"] in ("expired", "not_open"):
                log.info("manual close %s (%s): %s", res["slug"] or "all", res["status"], res["note"])
            results.append(res)
            done_ids.add(req["id"])
        save_close_results(results)
        return closed

    def _manual_close_one(self, pos: Position, markets: dict[str, BtcMarket], now: float) -> dict:
        out = {"key": pos.key, "slug": pos.slug, "side": pos.side, "shares": round(pos.shares, 4)}
        if pos.end_ts <= now:
            out.update(status="window_ended", note="window already ended; it settles at resolution instead")
            return out
        book = get_book(pos.token_id)
        if not book or not book.bids:
            out.update(status="failed", note="no bids in the order book right now")
            log.warning("manual close %s failed: no bids", pos.slug)
            return out
        bid = book.best_bid
        min_px = max(0.01, bid - CFG.slippage)     # same market exit as stop-loss / thesis exits
        log.info("manual close requested from dashboard: %s %s %.2f sh, bid=%.3f", pos.slug, pos.side, pos.shares, bid)
        self.last_exit = {}
        ok = self._close(pos, "manual_close", min_px, markets.get(pos.slug), manual=True)
        e = self.last_exit or {}
        out.update(status="closed" if ok else "failed", bid=bid, price=e.get("price"), proceeds=e.get("proceeds"),
                   fee=e.get("fee"), pnl=e.get("pnl"), note="" if ok else (e.get("note") or "sell not filled"))
        if pos.slug not in self.s.manual_closed_slugs:
            self.s.manual_closed_slugs.append(pos.slug)   # block re-entry into this window
        if pos.slug not in self.s.traded_slugs:
            self.s.traded_slugs.append(pos.slug)
        self.s.save()
        return out

    def manage(self, markets: dict[str, BtcMarket], snap: Optional[PriceSnapshot], news: NewsState,
               flatten_all: bool = False) -> None:
        for pos in list(self.s.positions.values()):
            left = pos.end_ts - now_ts()
            if left <= 0:
                self._settle_at_resolution(pos)
                continue
            book = get_book(pos.token_id)
            if not book:
                continue
            if pos.kind == "arb" and not flatten_all:
                continue      # complete set: guaranteed $1 per pair at resolution, nothing to manage
            bid = book.best_bid
            min_px = max(0.01, bid - CFG.slippage)
            mkt = markets.get(pos.slug)
            if flatten_all:
                self._close(pos, "flatten_all", min_px, mkt)
                continue
            p_side = None
            if CFG.strategy == "favorite":
                p_side = book.mid      # the market's own (well-calibrated) probability for our side
            elif mkt and snap:
                ptb, _ = price_to_beat(mkt, snap)
                if ptb:
                    p_up = self.estimator(mkt, snap, ptb, news, book)
                    p_side = p_up if pos.side == "UP" else 1 - p_up
            ex = exit_reason(CFG, entry=pos.entry_price, bid=bid, shares=pos.shares, cost_usd=pos.cost_usd,
                             fee_rate=pos.fee_rate, secs_left=left, p_side=p_side)
            if ex:
                kind, detail = ex
                px = max(0.01, bid - 0.01) if kind == "take_profit" else min_px
                self._close(pos, f"{kind} {detail}", px, mkt)


# =============================================================================
# SECTION 8: MAIN LOOP
# =============================================================================
_STOP = False


def _handle_sig(*_: Any) -> None:
    global _STOP
    _STOP = True
    log.info("stop requested; finishing current loop")


def log_decision(verdict: str, d: dict, extra: str = "") -> None:
    row = {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "slug": d.get("slug", ""), "verdict": verdict,
           "secs_left": d.get("secs_left", ""), "side": d.get("side", ""), "p_up": d.get("p_up", ""),
           "bid": d.get("bid", ""), "ask": d.get("ask", ""), "edge": d.get("edge", ""),
           "ptb_src": d.get("ptb_src", ""), "model": d.get("model", ""), "note": extra}
    append_csv(DECISIONS_CSV, row)


def startup_migrations(state: State, reset_halt: bool = False) -> None:
    """Run after the first consistent bankroll refresh.
    - --reset-halt: clear any halt and re-anchor the peak at the current bankroll.
    - Auto-fix for the pre-fix double-count bug: a 'max drawdown hit' halt whose bankroll is NOT really
      10%+ below a recomputed peak (max of today's start and now) is cleared and the peak reset."""
    bank = state.bankroll_usd
    if reset_halt:
        log.warning("--reset-halt: clearing halt (%r), peak %.2f -> %.2f", state.halt_reason,
                    state.peak_bankroll_usd, bank)
        state.halted, state.halt_reason = False, ""
        state.peak_bankroll_usd = bank
        return
    if state.halted and state.halt_reason.startswith("max drawdown hit"):
        new_peak = max(state.day_start_bankroll_usd, bank)
        if bank > new_peak * (1 - CFG.max_drawdown_pct):
            log.warning("migration: clearing stale halt (%r). Recomputed bankroll %.2f vs recomputed peak %.2f "
                        "is not a %.0f%% drawdown; peak %.2f -> %.2f", state.halt_reason, bank, new_peak,
                        100 * CFG.max_drawdown_pct, state.peak_bankroll_usd, new_peak)
            state.halted, state.halt_reason = False, ""
            state.peak_bankroll_usd = new_peak
        else:
            log.warning("migration: drawdown halt kept (bankroll %.2f vs recomputed peak %.2f)", bank, new_peak)


def main() -> None:
    ap = argparse.ArgumentParser(description="Polymarket BTC Up/Down 5m/15m bot (paper mode by default)")
    ap.add_argument("--max-loops", type=int, default=0, help="stop after N loops (0 = run forever)")
    ap.add_argument("--flatten", action="store_true", help="close all open positions and exit")
    ap.add_argument("--stop-after-trades", type=int, default=0,
                    help="exit once N entries were made this run and all positions are closed (0 = never)")
    ap.add_argument("--reset-halt", action="store_true",
                    help="clear a saved halt (halted/halt_reason) and set peak_bankroll = current bankroll")
    args = ap.parse_args()
    setup_logging()
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    if hasattr(signal, "SIGBREAK"):   # Windows: Ctrl+Break / CTRL_BREAK_EVENT
        signal.signal(signal.SIGBREAK, _handle_sig)
    stop_file = DATA / "STOP"         # graceful-stop request (used by the dashboard); positions stay open
    stop_file.unlink(missing_ok=True)

    if not CFG.dry_run and not CFG.live:
        raise SystemExit(f"DRY_RUN=false but LIVE_TRADING_CONFIRM is not '{LIVE_CONFIRM_PHRASE}'. Refusing to trade live.")
    ex = Executor()
    live_bal = ex.collateral_balance_usd()
    for _ in range(5):
        if not CFG.live or live_bal is not None:
            break
        time.sleep(5)
        live_bal = ex.collateral_balance_usd()
    if CFG.live and live_bal is None:
        raise SystemExit("Could not read the live pUSD balance; not starting (bankroll would be wrong).")
    state = State.load(live_bal if live_bal is not None else CFG.paper_bankroll_usd)
    risk = Risk(state)
    pm = PositionManager(state, risk, ex)
    pm.refresh_bankroll(live_bal)
    startup_migrations(state, reset_halt=args.reset_halt)
    state.save()
    log.info("entry_mode=%s%s news_gate=%s", CFG.entry_mode,
             f" (offset {CFG.limit_offset}, cancel at {CFG.limit_cancel_secs_left}s, reprices {CFG.limit_max_reprices})"
             if CFG.entry_mode == "limit" else "", CFG.news_gate)
    log.info("mode=%s bankroll=$%.2f (cash %.2f, %d open, %d pending redeem) peak=$%.2f halted=%s",
             "LIVE" if CFG.live else "PAPER (dry run)", state.bankroll_usd, state.cash_usd, len(state.positions),
             len(state.pending_redeem), state.peak_bankroll_usd, state.halted)

    news = NewsState()
    markets: list[BtcMarket] = []
    last_scan = last_news = 0.0
    loops = 0
    entries_this_run = 0
    while not _STOP:
        if stop_file.exists():
            log.info("STOP file found: stopping gracefully (open positions stay open)")
            stop_file.unlink(missing_ok=True)
            break
        loops += 1
        try:
            risk.roll_day()
            if now_ts() - last_news >= CFG.news_refresh_seconds:
                news = refresh_news()
                last_news = now_ts()
            if now_ts() - last_scan >= CFG.scan_seconds:
                markets = scan_btc_updown_markets()
                last_scan = now_ts()
                log.info("scanner: %d live BTC up/down markets: %s", len(markets),
                         ", ".join(f"{m.slug}({m.seconds_left:.0f}s)" for m in markets[:6]))
            snap = fetch_btc_prices()
            # One consistent bankroll: cash + open positions + resolved-unredeemed winners (see compute_bankroll)
            trusted = pm.refresh_bankroll(ex.collateral_balance_usd() if CFG.live else None)
            by_slug = {m.slug: m for m in markets}

            halt = risk.check_kill_switches(bank_trusted=trusted)
            if halt and not state.halted:
                state.halted, state.halt_reason = True, halt
                log.error("HALTING new entries: %s -- flattening positions", halt)
                pm.cancel_all_orders(f"halt: {halt}")
                pm.manage(by_slug, snap, news, flatten_all=True)
                state.save()
            if args.flatten:
                pm.manage(by_slug, snap, news, flatten_all=True)
                break

            pm.process_close_requests(by_slug)      # dashboard "Close" buttons (works while halted too)
            pm.manage(by_slug, snap, news)
            pm.manage_orders(by_slug)                # resting limit entries: fills, cancels, reprices

            for m in markets:
                if args.stop_after_trades and entries_this_run >= args.stop_after_trades:
                    break
                if now_ts() < m.start_ts:
                    continue   # only windows that are live right now
                if m.seconds_left > 15 and not state.halted:
                    pm.try_arbitrage(m)
                if m.slug in state.positions or m.slug in state.pending_orders:
                    continue
                sig, verdict, d = decide(m, snap, news)
                if sig is None:
                    log_decision(verdict, d)
                    log.debug("%s -> %s", m.slug, verdict)
                    continue
                usd, why = risk.size_for(sig)
                if usd <= 0:
                    log_decision("skip:risk", d, why)
                    log.info("%s qualified (%s p=%.3f edge=%.3f) but risk said no: %s",
                             m.slug, sig.side, sig.p_side, sig.edge, why)
                    continue
                log_decision("ENTER", d, f"usd={usd}")
                log.info("ENTER %s %s p=%.3f ask=%.3f edge=%.3f size=$%.2f | %s",
                         m.slug, sig.side, sig.p_side, sig.ask, sig.edge, usd, " | ".join(sig.reasons))
                if CFG.entry_mode == "limit" and CFG.strategy == "favorite":
                    pm.open_limit(sig, usd)
                else:
                    pm.open(sig, usd)
                entries_this_run += 1
                if args.stop_after_trades and entries_this_run >= args.stop_after_trades:
                    break
            state.save()
            if args.stop_after_trades and entries_this_run >= args.stop_after_trades and not state.positions:
                log.info("stop-after-trades reached (%d) and no open positions: exiting", entries_this_run)
                break
        except Exception as e:
            log.exception("loop error (continuing): %s", e)
        if args.max_loops and loops >= args.max_loops:
            break
        time.sleep(CFG.loop_seconds)
    if state.pending_orders:
        pm.cancel_all_orders("bot stopping")   # never leave resting orders behind
    state.save()
    if state.positions:
        log.warning("exiting with %d open position(s); they stay open. Run with --flatten to close.", len(state.positions))
    log.info("stopped. realized pnl today=%+.2f total=%+.2f", state.realized_pnl_day, state.realized_pnl_total)


if __name__ == "__main__":
    main()
