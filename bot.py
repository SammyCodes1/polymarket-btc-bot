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
    # Bookkeeping sanity guard: a bankroll move bigger than this fraction with no trade/resolve event is
    # treated as a glitch (no new peak, no drawdown halt) until it has persisted BANK_JUMP_WINDOW_SECONDS.
    bank_jump_pct: float = _env("BANK_JUMP_PCT", 0.30)
    bank_jump_window_seconds: int = _env("BANK_JUMP_WINDOW_SECONDS", 60)

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


def _rss_items(url: str) -> list[tuple[str, float]]:
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
            items.append((f"{e.get('title', '')}. {e.get('summary', '')[:300]}", ts))
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


def refresh_news() -> NewsState:
    raw: list[tuple[str, float]] = []
    ok = 0
    for url in [u.strip() for u in CFG.rss_feeds.split(",") if u.strip()]:
        try:
            raw += _rss_items(url)
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
    """Sanity guard against bookkeeping glitches: a bankroll move of more than BANK_JUMP_PCT with no
    trade/resolve event is not trusted (no new peak, no drawdown halt) until it has persisted for
    BANK_JUMP_WINDOW_SECONDS. Trade events (open/close/resolve) are trusted immediately."""

    def __init__(self, pct: float, window: float) -> None:
        self.pct, self.window = pct, window
        self.ref: Optional[float] = None
        self.anomaly_since: Optional[float] = None

    def note_event(self, delta: float) -> None:
        """A trade/resolve event moved our own bookkeeping by delta: that move is expected."""
        if self.ref is not None:
            self.ref += delta

    def observe(self, bank: float, now: float) -> bool:
        """Returns True if this bankroll value can be trusted for peak/drawdown this tick."""
        if self.ref is None:
            self.ref, self.anomaly_since = bank, None
            return True
        base = max(abs(self.ref), 1e-9)
        if abs(bank - self.ref) / base <= self.pct:
            self.ref, self.anomaly_since = bank, None
            return True
        first = self.anomaly_since is None
        if first:
            self.anomaly_since = now
        if now - self.anomaly_since >= self.window:
            log.warning("bankroll move %.2f -> %.2f persisted %.0fs; accepting it", self.ref, bank,
                        now - self.anomaly_since)
            self.ref, self.anomaly_since = bank, None
            return True
        if first:      # log once per anomaly, not every 5s tick
            log.warning("bankroll jumped %.2f -> %.2f (%+.0f%%) with no trade event; treating it as a bookkeeping "
                        "glitch for up to %.0fs (no new peak, drawdown halt skipped)", self.ref, bank,
                        100 * (bank - self.ref) / base, self.window)
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

    def check_kill_switches(self, bank_trusted: bool = True) -> Optional[str]:
        if Path(CFG.kill_switch_file).exists() or (DATA / CFG.kill_switch_file).exists():
            return "kill switch file present"
        if self.s.realized_pnl_day <= -CFG.daily_loss_limit_pct * self.s.day_start_bankroll_usd:
            return f"daily loss limit hit ({self.s.realized_pnl_day:.2f})"
        if bank_trusted and self.s.bankroll_usd <= self.s.peak_bankroll_usd * (1 - CFG.max_drawdown_pct):
            return f"max drawdown hit (bankroll {self.s.bankroll_usd:.2f} vs peak {self.s.peak_bankroll_usd:.2f})"
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
        if sig.market.slug in self.s.traded_slugs:
            return 0.0, "already traded this window"
        if sum(p.kind == "dir" for p in self.s.positions.values()) >= CFG.max_open_positions:
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

    def record_close(self, pnl: float) -> None:
        self.s.realized_pnl_day += pnl
        self.s.realized_pnl_total += pnl
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
    if cfg.stop_loss_pct > 0 and bid <= entry * (1 - cfg.stop_loss_pct):
        return "stop_loss", f"bid={bid:.3f}"
    if cfg.force_exit_seconds > 0 and secs_left <= cfg.force_exit_seconds:
        return "pre_expiry_exit", f"{secs_left:.0f}s left"
    if cfg.thesis_exit_prob > 0 and p_side is not None and p_side < cfg.thesis_exit_prob:
        return "thesis_invalidated", f"p={p_side:.3f}"
    return None


class PositionManager:
    def __init__(self, state: State, risk: Risk, ex: Executor) -> None:
        self.s, self.risk, self.ex = state, risk, ex
        self.guard = BankrollGuard(CFG.bank_jump_pct, CFG.bank_jump_window_seconds)
        self.balances: dict = {}      # token_id -> on-chain shares from the last refresh (live only)
        self.bank_trusted = True
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
            for pos in self.s.positions.values():
                if pos.end_ts <= now:
                    if pos.cash_at_end is None:
                        pos.cash_at_end = self.s.cash_usd
                    self.balances[pos.token_id] = self.ex.token_balance(pos.token_id)
            for key, e in list(self.s.pending_redeem.items()):
                bal = self.ex.token_balance(e["token_id"])
                self.balances[e["token_id"]] = bal
                self._maybe_clear_pending(key, e, bal, now)
            self._maybe_redeem(now)
        bank, parts = compute_bankroll(self.s.cash_usd, self.s.positions, self.s.pending_redeem, self.balances, now)
        self.s.bankroll_usd = bank
        self.bank_trusted = self.guard.observe(bank, now)
        if self.bank_trusted:
            self.s.peak_bankroll_usd = max(self.s.peak_bankroll_usd, bank)
        log.debug("bankroll %.2f = cash %.2f + open %.2f + pending_redeem %.2f (trusted=%s)", bank,
                  parts["cash"], parts["open"], parts["pending_redeem"], self.bank_trusted)
        return self.bank_trusted

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

    def try_arbitrage(self, m: BtcMarket) -> None:
        """Complete-set arbitrage: if Up ask + Down ask + taker fees < $1 - ARB_MIN_EDGE, buy equal shares
        of both; exactly one side pays $1 at resolution. Rarely available (books are usually >= $1.01)."""
        if not CFG.arb_enabled or any(k.startswith(m.slug + ":arb") for k in self.s.positions):
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

    def _close(self, pos: Position, reason: str, min_price: float, mkt: Optional[BtcMarket]) -> bool:
        fake = mkt or BtcMarket(pos.slug, pos.duration, "", "", pos.start_ts, pos.end_ts, pos.token_id, pos.token_id,
                                0.01, 5, pos.fee_rate, True, "v1")
        fill = self.ex.sell(fake, pos.token_id, pos.shares, min_price)
        pnl = fill.usd - pos.cost_usd if fill.ok else 0.0
        append_csv(TRADES_CSV, {"time": utc_iso(), "mode": "LIVE" if CFG.live else "PAPER", "action": "SELL",
                                "slug": pos.slug, "side": pos.side, "ok": fill.ok, "shares": round(fill.shares, 4),
                                "price": round(fill.avg_price, 4), "usd": round(fill.usd, 4), "fee": round(fill.fee, 4),
                                "pnl": round(pnl, 4) if fill.ok else "", "reason": reason, "note": fill.note})
        if not fill.ok:
            log.warning("exit failed %s (%s): %s", pos.slug, reason, fill.note)
            return False
        self._settle(pos, fill.usd, reason)
        return True

    def _settle(self, pos: Position, proceeds: float, reason: str) -> None:
        """Close a sold position: proceeds go to cash (live: a local estimate until the next cash sync).
        Never touches peak_bankroll_usd; refresh_bankroll() does that from the consistent bankroll."""
        pnl = proceeds - pos.cost_usd

        def mutate() -> None:
            self.s.positions.pop(pos.key, None)
            self.s.cash_usd += proceeds
        self._event(mutate)
        self.risk.record_close(pnl)
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
            # Paper only: Gamma can take 10+ minutes to flip `closed` after the window ends.
            # Settle once the outcome is effectively decided (>=0.99 / <=0.01) a minute past the end.
            if not CFG.live and not m.get("closed") and time.time() > pos.end_ts + 60 and (p >= 0.99 or p <= 0.01):
                p = 1.0 if p >= 0.99 else 0.0
                m["closed"] = True
            if m.get("closed") and p in (0.0, 1.0):
                self.resolve(pos, p, m.get("conditionId", ""))
        except Exception as e:
            log.debug("resolution check %s: %s", pos.slug, e)

    def resolve(self, pos: Position, p: float, condition_id: str = "") -> None:
        """Book a resolved position. Live winners whose tokens are still on-chain go to pending_redeem
        (still worth $1/share); if the tokens are already gone, the payout is already in the synced cash."""
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
                                "reason": "resolution", "note": "pending redeem" if pending else ""})
        pnl = payout - pos.cost_usd

        def mutate() -> None:
            self.s.positions.pop(pos.key, None)
            if pending is not None:
                self.s.pending_redeem[pos.key] = pending
            self.s.cash_usd += proceeds_to_cash
        self._event(mutate)
        self.risk.record_close(pnl)
        self.s.save()
        log.info("CLOSED %s %s reason=resolved pnl=%+.2f bankroll=%.2f%s", pos.slug, pos.side, pnl,
                 self.s.bankroll_usd, " (payout pending redeem)" if pending else "")
        if pending is not None:
            self._maybe_redeem(now_ts())

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
                pm.manage(by_slug, snap, news, flatten_all=True)
                state.save()
            if args.flatten:
                pm.manage(by_slug, snap, news, flatten_all=True)
                break

            pm.manage(by_slug, snap, news)

            for m in markets:
                if args.stop_after_trades and entries_this_run >= args.stop_after_trades:
                    break
                if now_ts() < m.start_ts:
                    continue   # only windows that are live right now
                if m.seconds_left > 15 and not state.halted:
                    pm.try_arbitrage(m)
                if m.slug in state.positions:
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
    state.save()
    if state.positions:
        log.warning("exiting with %d open position(s); they stay open. Run with --flatten to close.", len(state.positions))
    log.info("stopped. realized pnl today=%+.2f total=%+.2f", state.realized_pnl_day, state.realized_pnl_total)


if __name__ == "__main__":
    main()
