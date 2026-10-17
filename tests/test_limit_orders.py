"""Offline tests for ENTRY_MODE=limit: pricing, sizing, the conservative paper fill model and the resting-order
lifecycle (post, partial fills, reprice, cancel at the time limit / out of band / halt), paper and mocked live.
Run:  python tests/test_limit_orders.py        (also works under pytest)"""
import csv
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pmbot-lo-"))
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

SLUG, UP, DN, CID = "btc-updown-15m-1792279800", "tok-up", "tok-dn", "0xcond"
START = 1792279800.0
END = START + 900
CFG0 = bot.CFG


def cfg(**kw):
    base = dict(strategy="favorite", entry_mode="limit", limit_offset=0.01, limit_cancel_secs_left=600,
                limit_cancel_out_of_band=True, limit_reprice_cents=1.0, limit_max_reprices=2, limit_post_only=True,
                paper_at_price_fill_frac=0.0, fav_min_price=0.82, fav_max_price=0.92, max_position_usd=4.80,
                max_open_positions=1, news_gate=False, dry_run=True, live_confirm="")
    base.update(kw)
    return replace(CFG0, **base)


class World:
    """Temp data dir, fake clock, fake order book and fake data-api prints."""
    def __init__(self, live=False, **cfgkw):
        self.live, self.cfgkw = live, cfgkw
        self.now = START + 150           # 750 s left
        self.bids, self.asks = [(0.86, 200.0)], [(0.88, 200.0)]
        self.trades = []

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pmbot-lo-"))
        self.saved = {n: getattr(bot, n) for n in ("STATE_FILE", "TRADES_CSV", "get_book", "now_ts",
                                                    "fetch_recent_trades", "CFG")}
        bot.CFG = cfg(**({"dry_run": False, "live_confirm": bot.LIVE_CONFIRM_PHRASE} if self.live else {}), **self.cfgkw)
        bot.STATE_FILE, bot.TRADES_CSV = self.dir / "state.json", self.dir / "trades.csv"
        bot.now_ts = lambda: self.now
        bot.get_book = lambda tok: bot.Book(self.bids[0][0], self.asks[0][0], 100.0, 100.0,
                                            list(self.bids), list(self.asks), tok) if tok == UP else \
            bot.Book(1 - self.asks[0][0], 1 - self.bids[0][0], 100.0, 100.0, [(1 - self.asks[0][0], 200.0)],
                     [(1 - self.bids[0][0], 200.0)], tok)
        bot.fetch_recent_trades = lambda cid, limit=300: list(self.trades)
        st = bot.State(bankroll_usd=50, peak_bankroll_usd=50, day="2026-10-18", day_start_bankroll_usd=50, cash_usd=50)
        ex = bot.Executor.__new__(bot.Executor)
        ex.client = FakeClient() if self.live else None
        ex.has_gasless_key = False
        ex.token_balance = lambda tid: None
        self.st, self.ex = st, ex
        self.pm = bot.PositionManager(st, bot.Risk(st), ex)
        self.market = bot.BtcMarket(SLUG, "15m", CID, "q", START, END, UP, DN, 0.01, 5, 0.07, True, "v1")
        return self

    def __exit__(self, *a):
        for n, v in self.saved.items():
            setattr(bot, n, v)

    def signal(self):
        book = bot.get_book(UP)
        return bot.Signal(self.market, "UP", UP, book.best_ask, 0.9, 0.02, book, ["test"])

    def trade(self, asset, side, price, size, dt=1.0, tx=None):
        self.trades.insert(0, {"asset": asset, "side": side, "price": price, "size": size,
                               "timestamp": self.now + dt, "transactionHash": tx or f"0x{len(self.trades)}"})

    def rows(self, action):
        if not bot.TRADES_CSV.exists():
            return []
        return [r for r in csv.DictReader(bot.TRADES_CSV.open()) if r["action"] == action]


class FakeClient:
    def __init__(self):
        self.posted, self.cancelled, self.matched, self.status = [], [], {}, {}

    def place_limit_order(self, **kw):
        oid = f"0xord{len(self.posted)}"
        self.posted.append(dict(kw, order_id=oid))
        self.matched[oid], self.status[oid] = 0.0, "LIVE"
        return SimpleNamespace(ok=True, order_id=oid, status="live", making_amount="0", taking_amount="0")

    def get_order(self, order_id):
        return SimpleNamespace(size_matched=self.matched[order_id], status=self.status[order_id])

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.status[order_id] = "CANCELED"
        return SimpleNamespace(canceled=(order_id,), not_canceled={})


# ---------------------------------------------------------------- pure helpers
def test_price_and_size():
    c = cfg()
    assert bot.limit_bid_price(c, mid=0.87, best_ask=0.88, tick=0.01) == 0.86
    assert bot.limit_bid_price(c, mid=0.875, best_ask=0.88, tick=0.01) == 0.86     # rounded DOWN to tick
    assert bot.limit_bid_price(c, mid=0.935, best_ask=0.94, tick=0.01) == 0.92     # capped at FAV_MAX_PRICE
    assert bot.limit_bid_price(c, mid=0.865, best_ask=0.87, tick=0.01) == 0.85
    assert bot.limit_bid_price(replace(c, limit_offset=0.0), mid=0.87, best_ask=0.88, tick=0.01) == 0.87
    assert bot.limit_bid_price(replace(c, limit_offset=0.0), mid=0.875, best_ask=0.88, tick=0.01) == 0.87  # < ask
    assert bot.limit_order_shares(4.80, 0.86, 5) == 5.58
    assert bot.limit_order_shares(4.00, 0.86, 5) == 0.0                              # below 5-share minimum


def test_paper_fill_model_is_conservative():
    c = cfg()
    o = bot.PendingOrder(SLUG, "15m", "UP", UP, DN, CID, 0.86, 5.58, posted_at=100.0, last_trade_ts=100.0)
    book = bot.Book(0.85, 0.88, 1, 1, [(0.85, 50)], [(0.88, 50)], UP)
    t = lambda **kw: dict({"timestamp": 101, "transactionHash": "0xa"}, **kw)
    assert bot.paper_fill_qty(c, o, book, [], 102)[0] == 0
    assert bot.paper_fill_qty(c, o, book, [t(asset=UP, side="SELL", price=0.86, size=50)], 102)[0] == 0   # at price
    assert bot.paper_fill_qty(c, o, book, [t(asset=UP, side="BUY", price=0.85, size=50)], 102)[0] == 0    # buyer
    assert bot.paper_fill_qty(c, o, book, [t(asset=UP, side="SELL", price=0.85, size=2)], 102)[0] == 2    # through
    assert bot.paper_fill_qty(c, o, book, [t(asset=DN, side="BUY", price=0.15, size=50)], 102)[0] == 5.58  # opp >1-bid
    assert bot.paper_fill_qty(c, o, book, [t(asset=DN, side="BUY", price=0.14, size=50)], 102)[0] == 0
    assert bot.paper_fill_qty(c, o, book, [t(asset=UP, side="SELL", price=0.80, size=50, timestamp=99)], 102)[0] == 0
    crossed = bot.Book(0.84, 0.86, 1, 1, [(0.84, 50)], [(0.86, 3.0), (0.87, 50)], UP)
    assert bot.paper_fill_qty(c, o, crossed, [], 102)[0] == 3.0
    half = replace(c, paper_at_price_fill_frac=0.5)
    assert bot.paper_fill_qty(half, o, book, [t(asset=UP, side="SELL", price=0.86, size=4)], 102)[0] == 2.0
    o.seen_tx = ["0xa:tok-up:2"]
    assert bot.paper_fill_qty(c, o, book, [t(asset=UP, side="SELL", price=0.85, size=2)], 102)[0] == 0   # deduped


# ---------------------------------------------------------------- lifecycle (paper)
def test_post_partial_fill_then_cancel_at_600s():
    with World() as w:
        assert w.pm.open_limit(w.signal(), 4.80)
        o = w.st.pending_orders[SLUG]
        assert (o.price, o.size) == (0.86, 5.58) and SLUG in w.st.traded_slugs and w.st.trades_today == 0
        w.trade(UP, "SELL", 0.85, 2.0)
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        pos = w.st.positions[SLUG]
        assert abs(pos.shares - 2.0) < 1e-9 and abs(pos.cost_usd - 1.72) < 1e-9 and w.st.trades_today == 1
        assert abs(w.st.cash_usd - (50 - 1.72)) < 1e-9 and abs(w.st.bankroll_usd - 50) < 1e-9   # maker: no fee
        w.now = END - 600
        w.pm.manage_orders({SLUG: w.market})
        assert SLUG not in w.st.pending_orders and len(w.rows("LIMIT_CANCEL")) == 1
        assert abs(w.st.positions[SLUG].shares - 2.0) < 1e-9          # partial position kept, held to settlement


def test_ask_dropping_to_our_bid_fills():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        w.bids, w.asks = [(0.84, 100)], [(0.86, 3.0), (0.87, 100)]     # sellers now offer at our 0.86 bid
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert abs(w.st.positions[SLUG].shares - 3.0) < 1e-9


def test_full_fill_completes_order():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        w.trade(DN, "BUY", 0.16, 20.0)
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert SLUG not in w.st.pending_orders and abs(w.st.positions[SLUG].shares - 5.58) < 1e-9
        assert len(w.rows("LIMIT_FILL")) == 1 and not w.rows("LIMIT_CANCEL")
        w.trade(UP, "SELL", 0.80, 50.0, dt=6)                        # later prints don't add shares
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert abs(w.st.positions[SLUG].shares - 5.58) < 1e-9


def test_cancel_when_price_leaves_band_and_no_reentry():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        w.bids, w.asks = [(0.92, 100)], [(0.94, 100)]      # mid 0.93 >= FAV_MAX_PRICE
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert SLUG not in w.st.pending_orders and SLUG not in w.st.positions
        assert "left band" in w.rows("LIMIT_CANCEL")[0]["reason"]
        assert w.pm.risk.size_for(w.signal())[1] == "already traded this window"


def test_reprice_limited_times():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        for i, (b, a) in enumerate([(0.88, 0.89), (0.90, 0.91), (0.91, 0.92)]):   # mid 0.885, 0.905, 0.915
            w.bids, w.asks = [(b, 100)], [(a, 100)]
            w.now += 5
            w.pm.manage_orders({SLUG: w.market})
        o = w.st.pending_orders[SLUG]
        assert o.reprices == 2 and o.price == 0.89           # third move ignored (max 2 reprices)
        assert o.size == 5.39 and o.size * o.price <= 4.80   # resized to stay within MAX_POSITION_USD
        assert len(w.rows("LIMIT_POST")) == 3 and len(w.rows("LIMIT_CANCEL")) == 2


def test_reprice_after_partial_keeps_remaining_size():
    with World(max_position_usd=10.0) as w:
        w.pm.open_limit(w.signal(), 10.0)                     # 11.62 sh @ 0.86
        w.trade(UP, "SELL", 0.85, 1.0)
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        w.bids, w.asks = [(0.88, 100)], [(0.89, 100)]        # mid 0.885 (>1c move) -> requote at 0.87
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        o = w.st.pending_orders[SLUG]
        assert o.price == 0.87 and abs(o.size - 10.50) < 1e-9   # budget-capped: (10 - 0.86) / 0.87 and abs(o.total_filled - 1.0) < 1e-9


def test_reprice_dropped_when_rest_below_minimum():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        w.trade(UP, "SELL", 0.85, 3.0)
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        w.bids, w.asks = [(0.88, 100)], [(0.89, 100)]        # 2.58 left < 5-share minimum: just stop quoting
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert SLUG not in w.st.pending_orders and abs(w.st.positions[SLUG].shares - 3.0) < 1e-9


def test_halt_and_shutdown_cancel_everything():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        w.st.halted, w.st.halt_reason = True, "test"
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert not w.st.pending_orders
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        w.pm.cancel_all_orders("bot stopping")
        assert not w.st.pending_orders and w.rows("LIMIT_CANCEL")[0]["reason"] == "bot stopping"


def test_pending_counts_as_open_position_and_state_roundtrip():
    with World() as w:
        w.pm.open_limit(w.signal(), 4.80)
        other = bot.BtcMarket(SLUG + "x", "15m", CID, "q", START, END, UP, DN, 0.01, 5, 0.07, True, "v1")
        sig = bot.Signal(other, "UP", UP, 0.88, 0.9, 0.02, bot.get_book(UP), [])
        assert w.pm.risk.size_for(sig)[1] == "max open positions"
        st2 = bot.State.load(50)
        assert isinstance(st2.pending_orders[SLUG], bot.PendingOrder) and st2.pending_orders[SLUG].price == 0.86


def test_below_minimum_is_not_posted():
    with World() as w:
        assert not w.pm.open_limit(w.signal(), 3.00)
        assert not w.st.pending_orders and not w.rows("LIMIT_POST")


# ---------------------------------------------------------------- lifecycle (mocked live client)
def test_live_post_only_fills_and_cancel():
    with World(live=True) as w:
        assert w.pm.open_limit(w.signal(), 4.80)
        c = w.ex.client
        kw = c.posted[0]
        assert kw["post_only"] is True and kw["side"] == "BUY" and kw["price"] == "0.86" and kw["size"] == "5.58"
        assert kw["token_id"] == UP and kw["expiration"] >= w.now + 200
        oid = kw["order_id"]
        c.matched[oid] = 2.5
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert abs(w.st.positions[SLUG].shares - 2.5) < 1e-9
        c.matched[oid] = 3.0                                  # one more fill right before our cancel
        w.now = END - 600
        w.pm.manage_orders({SLUG: w.market})
        assert c.cancelled == [oid] and SLUG not in w.st.pending_orders
        assert abs(w.st.positions[SLUG].shares - 3.0) < 1e-9 and abs(w.st.positions[SLUG].cost_usd - 2.58) < 1e-9


def test_live_failed_cancel_is_retried():
    with World(live=True) as w:
        w.pm.open_limit(w.signal(), 4.80)
        c = w.ex.client
        c.cancel_order = lambda order_id: SimpleNamespace(canceled=(), not_canceled={order_id: "busy"})
        w.now = END - 590
        w.pm.manage_orders({SLUG: w.market})
        assert SLUG in w.st.pending_orders                    # still LIVE on the exchange: keep tracking it
        c.cancel_order = FakeClient.cancel_order.__get__(c)
        w.now += 5
        w.pm.manage_orders({SLUG: w.market})
        assert SLUG not in w.st.pending_orders


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f(); print("ok", f.__name__)
    print(f"{len(fns)}/{len(fns)} passed")
