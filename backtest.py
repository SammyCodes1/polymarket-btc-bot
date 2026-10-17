#!/usr/bin/env python3
"""Backtest for the Polymarket BTC Up/Down bot.

Replays the bot's own decision code (bot.model_p_up, bot.entry_gates, bot.size_usd,
bot.exit_reason, bot.fee_per_share) over resolved 5m/15m windows downloaded by
collect_data.py, and compares against simple baselines.

Data limits (be aware):
  * Market path = CLOB /prices-history (~1 point per minute per window). It is treated as the
    mid; the ask/bid are mid +/- HALF_SPREAD (default 0.5c, i.e. the 1-tick spread seen live).
  * BTC = Coinbase 1-minute candles. At a quote time t the model only sees the OPEN of the
    minute containing t (no look-ahead). Settlement truth = Polymarket's resolved outcome.
  * Maker fills: a resting bid at L fills only if a later quote shows the ask at/below L
    (mid <= L - half_spread - TRADE_THROUGH), i.e. price traded through us. No queue model.
  * Exits are checked only at quote times (~60s), not every 5s like the live bot.

Usage:
  python backtest.py                 # full report (before/after, baselines, IS vs OOS)
  python backtest.py --rebuild       # rebuild the cached dataset
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import random
import statistics
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))
import bot  # noqa: E402

D = Path(__file__).with_name("data") / "backtest"
HALF_SPREAD = 0.005
TRADE_THROUGH = 0.0
MAKER_FILL_WINDOW = 120   # seconds a maker order rests before cancel
IS_FRAC = 0.70


# ---------------------------------------------------------------------------- data
@dataclass
class Pt:
    ts: float
    secs_left: float
    elapsed: float
    mid: float          # Up-token price
    spot: float
    closes: list
    sigma: float
    p_model: float = 0.5


@dataclass
class Win:
    slug: str
    duration: str
    start: float
    end: float
    up_won: bool
    fee_rate: float
    s0: float
    pts: list = field(default_factory=list)


def build_dataset() -> list[Win]:
    wins = json.loads((D / "windows.json").read_text())
    C = {int(k): v for k, v in json.loads((D / "candles_1m.json").read_text()).items()}  # ts: [o,h,l,c,v]
    out = []
    for w in wins:
        s, e = int(w["start"]), int(w["end"])
        if s not in C:
            continue
        hp = D / "prices" / f"{w['slug']}.json"
        if not hp.exists():
            continue
        W = Win(w["slug"], "15m" if "-15m-" in w["slug"] else "5m", s, e, bool(w["up_won"]),
                float(w.get("fee_rate", 0.07)), C[s][0])
        for q in json.loads(hp.read_text()):
            t = q["t"]
            if not (s + 20 <= t < e - 5):
                continue
            m0 = t - t % 60
            closes = [C[x][3] for x in range(m0 - 3600, m0, 60) if x in C]
            if len(closes) < 30 or m0 not in C:
                continue
            rets = [math.log(b / a) for a, b in zip(closes[:-1], closes[1:])]
            sigma = max(statistics.pstdev(rets), 0.0003)
            W.pts.append(Pt(t, e - t, (t - s) / (e - s), float(q["p"]), C[m0][0], closes[-10:], sigma))
        if W.pts:
            out.append(W)
    out.sort(key=lambda w: w.start)
    return out


def load(rebuild=False) -> list[Win]:
    cache = D / "dataset.pkl"
    if cache.exists() and not rebuild:
        raw = pickle.loads(cache.read_bytes())
        return [Win(**{**w, "pts": [Pt(**p) for p in w["pts"]]}) for w in raw]
    ds = build_dataset()
    cache.write_bytes(pickle.dumps([asdict(w) for w in ds]))
    return ds


def attach_model(ds: list[Win], cfg) -> None:
    for w in ds:
        for p in w.pts:
            p.p_model, _ = bot.model_p_up(p.spot, w.s0, p.closes, p.sigma, p.secs_left, 60, 0.0, cfg)


# ---------------------------------------------------------------------------- strategies
@dataclass
class Order:
    side: str            # UP / DOWN
    p_side: float        # strategy's own probability estimate for the side
    kind: str            # taker / maker
    limit: float = 0.0   # maker price


def side_mid(pt: Pt, side: str) -> float:
    return pt.mid if side == "UP" else 1 - pt.mid


def strat_skip(w, pt, cfg):
    return None


def strat_bot(w: Win, pt: Pt, cfg) -> Optional[Order]:
    """The bot's actual entry logic (model probability + entry_gates)."""
    side = "UP" if pt.p_model >= 0.5 else "DOWN"
    p_side = pt.p_model if side == "UP" else 1 - pt.p_model
    m = side_mid(pt, side)
    bid, ask = max(0.01, m - HALF_SPREAD), min(0.99, m + HALF_SPREAD)
    rev = False
    if len(pt.closes) >= 4:
        recent = pt.closes[-1] - pt.closes[-4]
        lim = pt.spot * pt.sigma * 2
        rev = (side == "UP" and recent < -lim) or (side == "DOWN" and recent > lim)
    verdict, _ = bot.entry_gates(cfg, duration=w.duration, secs_left=pt.secs_left, elapsed=pt.elapsed, p_side=p_side,
                                 bid=bid, ask=ask, fee_rate=w.fee_rate, momentum_reversing=rev)
    if verdict != "ENTER":
        return None
    kind = getattr(cfg, "entry_mode", "taker")
    return Order(side, p_side, kind, bid)


def strat_v2(w: Win, pt: Pt, cfg) -> Optional[Order]:
    """The bot's v2 default strategy: bot.favorite_signal (early favorite in 15m windows)."""
    ub, ua = max(0.01, pt.mid - HALF_SPREAD), min(0.99, pt.mid + HALF_SPREAD)
    db, da = max(0.01, 1 - pt.mid - HALF_SPREAD), min(0.99, 1 - pt.mid + HALF_SPREAD)
    verdict, side, p_side, ask, bid = bot.favorite_signal(cfg, duration=w.duration, secs_left=pt.secs_left, up_bid=ub,
                                                         up_ask=ua, down_bid=db, down_ask=da, fee_rate=w.fee_rate)
    return Order(side, p_side, "taker") if verdict == "ENTER" else None


def make_favorite(t_lo, t_hi, p_lo, p_hi, kind="taker"):
    def f(w: Win, pt: Pt, cfg):
        if not (t_lo < pt.secs_left <= t_hi):
            return None
        side = "UP" if pt.mid >= 0.5 else "DOWN"
        m = side_mid(pt, side)
        if not (p_lo <= m < p_hi):
            return None
        return Order(side, m, kind, max(0.01, m - HALF_SPREAD))
    f.__name__ = f"favorite[{t_lo}-{t_hi}s,{p_lo}-{p_hi},{kind}]"
    return f


def make_momentum(t_max, min_bp, p_max=0.95):
    def f(w: Win, pt: Pt, cfg):
        if pt.secs_left > t_max:
            return None
        move = math.log(pt.spot / w.s0) * 1e4
        if abs(move) < min_bp:
            return None
        side = "UP" if move > 0 else "DOWN"
        if side_mid(pt, side) + HALF_SPREAD > p_max:
            return None
        return Order(side, side_mid(pt, side), "taker")
    f.__name__ = f"momentum[<= {t_max}s,{min_bp}bp]"
    return f


# ---------------------------------------------------------------------------- simulation
@dataclass
class Trade:
    slug: str
    start: float
    side: str
    kind: str
    entry: float
    shares: float
    cost: float
    proceeds: float
    exit: str

    @property
    def pnl(self):
        return self.proceeds - self.cost


def run_window(w: Win, strat: Callable, cfg, usd: float, exits: str) -> Optional[Trade]:
    """Simulate one window with a fixed stake. exits: 'hold' or 'bot' (bot.exit_reason)."""
    pts = w.pts
    for i, pt in enumerate(pts):
        o = strat(w, pt, cfg)
        if not o:
            continue
        # ---- fill
        fill_i = i
        if o.kind == "taker":
            price = min(0.99, side_mid(pt, o.side) + HALF_SPREAD)
            fee = bot.fee_per_share(price, w.fee_rate)
            shares = usd / (price + fee)
            cost = usd
        else:
            price, fill_i = o.limit, None
            for j in range(i + 1, len(pts)):
                if pts[j].ts - pt.ts > MAKER_FILL_WINDOW:
                    break
                if side_mid(pts[j], o.side) + HALF_SPREAD <= price - TRADE_THROUGH:   # ask came down through our bid
                    fill_i = j
                    break
            if fill_i is None:
                return None          # not filled -> order cancelled, no trade (strategy may not retry)
            shares, cost = usd / price, usd
        # ---- manage
        if exits == "bot":
            for j in range(fill_i + 1, len(pts)):
                q = pts[j]
                bid = max(0.01, side_mid(q, o.side) - HALF_SPREAD)
                if strat is strat_bot:
                    p_side = q.p_model if o.side == "UP" else 1 - q.p_model
                else:
                    p_side = side_mid(q, o.side)
                ex = bot.exit_reason(cfg, entry=price, bid=bid, shares=shares, cost_usd=cost, fee_rate=w.fee_rate,
                                     secs_left=q.secs_left, p_side=p_side)
                if ex:
                    proceeds = shares * bid * (1 - w.fee_rate * (1 - bid))
                    return Trade(w.slug, w.start, o.side, o.kind, price, shares, cost, proceeds, ex[0])
        won = (o.side == "UP") == w.up_won
        return Trade(w.slug, w.start, o.side, o.kind, price, shares, cost, shares if won else 0.0, "resolution")
    return None


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "pnl": 0.0, "pnl_per_$": None, "ci95_per_$": None, "max_dd": 0.0}
    pnl = [t.pnl for t in trades]
    staked = sum(t.cost for t in trades)
    eq = peak = dd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    rng = random.Random(7)
    r = [t.pnl / t.cost for t in trades]
    boots = sorted(statistics.fmean(rng.choices(r, k=len(r))) for _ in range(400))
    return {"trades": len(trades), "win_rate": round(sum(x > 0 for x in pnl) / len(pnl), 3), "pnl": round(sum(pnl), 2),
            "pnl_per_$": round(sum(pnl) / staked, 4), "ci95_per_$": (round(boots[10], 4), round(boots[389], 4)),
            "max_dd": round(dd, 2)}


def evaluate(ds, strat, cfg, usd=5.0, exits="hold"):
    return [t for w in ds if (t := run_window(w, strat, cfg, usd, exits))]


def portfolio_sim(ds: list[Win], strat, cfg, exits="bot", bank0=500.0) -> dict:
    """Bankroll simulation with the bot's sizing (bot.size_usd) and risk halts, windows in time order."""
    bank, peak, max_dd = bank0, bank0, 0.0
    day, day_start, day_pnl, consec, halted_days, dd_halt = None, bank0, 0.0, 0, 0, False
    open_pos: list[tuple[float, Trade]] = []   # (end_ts, trade)
    trades = []
    for w in ds:
        # settle positions that ended before this window starts
        for end, t in [x for x in open_pos if x[0] <= w.start]:
            open_pos.remove((end, t))
            bank += t.proceeds
            day_pnl += t.pnl
            consec = consec + 1 if t.pnl < 0 else 0
            peak = max(peak, bank)
            max_dd = max(max_dd, (peak - bank) / peak)
        d = int(w.start // 86400)
        if d != day:
            day, day_start, day_pnl = d, bank, 0.0
            consec = 0
        if dd_halt or bank <= peak * (1 - cfg.max_drawdown_pct):
            dd_halt = True
            continue
        if day_pnl <= -cfg.daily_loss_limit_pct * day_start or consec >= cfg.max_consecutive_losses:
            halted_days += 1
            continue
        if len(open_pos) >= cfg.max_open_positions:
            continue
        # size from the strategy's first signal in this window
        sig = next(((pt, o) for pt in w.pts if (o := strat(w, pt, cfg))), None)
        if not sig:
            continue
        pt, o = sig
        price = o.limit if o.kind == "maker" else side_mid(pt, o.side) + HALF_SPREAD
        total = sum(t.cost for _, t in open_pos)
        net = sum(t.cost if t.side == "UP" else -t.cost for _, t in open_pos)
        usd, _ = bot.size_usd(cfg, bank=bank, p_side=max(o.p_side, price + 1e-6) if strat is not strat_bot else o.p_side,
                              price=min(price, 0.99), total_exposure=total,
                              same_dir_exposure=net if o.side == "UP" else -net, min_order_usd=5 * price)
        if usd <= 0:
            continue
        t = run_window(w, strat, cfg, usd, exits)
        if t:
            bank -= t.cost
            open_pos.append((w.end, t))
            trades.append(t)
    for _, t in open_pos:
        bank += t.proceeds
    s = stats(trades)
    s.update(final_bank=round(bank, 2), max_dd_pct=round(max_dd * 100, 1), dd_halt=dd_halt)
    return s


# ---------------------------------------------------------------------------- calibration
def calibration(ds: list[Win]) -> list[str]:
    out = []
    rows = [(w.duration, p.secs_left, p.mid, p.p_model, float(w.up_won)) for w in ds for p in w.pts]
    for dur in ("5m", "15m"):
        for lo, hi in [(0, 60), (60, 120), (120, 300), (300, 600), (600, 900)]:
            r = [x for x in rows if x[0] == dur and lo < x[1] <= hi]
            if len(r) < 200:
                continue
            bm = statistics.fmean((x[2] - x[4]) ** 2 for x in r)
            bo = statistics.fmean((x[3] - x[4]) ** 2 for x in r)
            out.append(f"  {dur:>3} {lo:>3}-{hi:<3}s left  n={len(r):6d}  Brier market={bm:.4f}  model={bo:.4f}  coinflip=0.2500")
    out.append("  calibration buckets (all quotes): predicted -> realized Up rate")
    for lo in [i / 10 for i in range(10)]:
        rm = [x for x in rows if lo <= x[2] < lo + 0.1]
        ro = [x for x in rows if lo <= x[3] < lo + 0.1]
        fm = f"market n={len(rm):6d} real={statistics.fmean(x[4] for x in rm):.3f}" if rm else "market n=0"
        fo = f"model n={len(ro):6d} real={statistics.fmean(x[4] for x in ro):.3f}" if ro else "model n=0"
        out.append(f"   {lo:.1f}-{lo + .1:.1f}: {fm} | {fo}")
    return out


# ---------------------------------------------------------------------------- report
def fmt(name, s):
    return (f"  {name:<46} trades={s['trades']:5d} win={s['win_rate']} pnl=${s['pnl']:>8} per$={s['pnl_per_$']} "
            f"ci95={s['ci95_per_$']} maxDD=${s['max_dd']}")


def v1_config():
    """The bot as first shipped (model strategy, quick TP, 30% stop, thesis exit, 20s pre-expiry exit)."""
    return replace(bot.Config(), strategy="model", min_model_prob=0.80, min_edge=0.07, min_elapsed_frac=0.20,
                   max_disagreement=0.25, take_profit_pct=0.15, take_profit_capture=0.50, take_profit_min_bid=0.0,
                   stop_loss_pct=0.30, thesis_exit_prob=0.55, force_exit_seconds=20)


def tune(IS, OOS, cfg):
    """In-sample grid search; report the top configs' out-of-sample results."""
    print("\nTUNING (selected on IS only; OOS shown for honesty; ~100 configs tested -> expect lucky IS winners):")
    res = []
    for tl, th in [(20, 60), (60, 120), (120, 300), (300, 600), (600, 900)]:
        for pl, ph in [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.85, 0.95), (0.9, 0.95), (0.95, 0.99)]:
            for kind in ("taker", "maker"):
                f = make_favorite(tl, th, pl, ph, kind)
                st = stats(evaluate(IS, f, cfg))
                if st["trades"] >= 100:
                    res.append((st["pnl_per_$"], f, st))
    res.sort(key=lambda r: -r[0])
    mk = [r[0] for r in res if "maker" in r[1].__name__]
    tk = [r[0] for r in res if "taker" in r[1].__name__]
    print(f"  favorite grid: taker avg per$ {statistics.fmean(tk):+.4f} ({sum(x > 0 for x in tk)}/{len(tk)} positive); "
          f"maker avg per$ {statistics.fmean(mk):+.4f} ({sum(x > 0 for x in mk)}/{len(mk)} positive)")
    for r in res[:5]:
        o = stats(evaluate(OOS, r[1], cfg))
        print(f"  {r[1].__name__:<40} IS per$={r[0]:+.4f} n={r[2]['trades']:4d} | OOS per$={o['pnl_per_$']} n={o['trades']} ci={o['ci95_per_$']}")
    bres = []
    for mp in (0.6, 0.7, 0.8, 0.9):
        for me in (0.0, 0.03, 0.07):
            for el in (0.0, 0.2, 0.5):
                c = replace(v1_config(), min_model_prob=mp, min_edge=me, min_elapsed_frac=el)
                st = stats(evaluate(IS, strat_bot, c, exits="bot"))
                if st["trades"] >= 50:
                    bres.append((st["pnl_per_$"], c, st))
    bres.sort(key=lambda r: -r[0])
    for r in bres[:3]:
        o = stats(evaluate(OOS, strat_bot, r[1], exits="bot"))
        print(f"  model p>={r[1].min_model_prob} edge>={r[1].min_edge} elapsed>={r[1].min_elapsed_frac}: IS per$={r[0]:+.4f} "
              f"n={r[2]['trades']} | OOS per$={o['pnl_per_$']} n={o['trades']} ci={o['ci95_per_$']}")


def news_check(ds):
    p = D / "gdelt_tone_15m.json"
    if not p.exists():
        print("\nNEWS: no GDELT history downloaded; news effect not testable offline.")
        return
    from datetime import datetime, timezone
    tone = sorted((datetime.strptime(k, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp(), v)
                  for k, v in json.loads(p.read_text()).items())
    if len(tone) < 50:
        print("\nNEWS: too little GDELT data.")
        return
    import bisect
    ts = [t for t, _ in tone]
    xs, ys, mids = [], [], []
    for w in ds:
        i = bisect.bisect_right(ts, w.start) - 1
        if i < 1 or w.start - ts[i] > 3 * 3600 or not w.pts:
            continue
        xs.append(tone[i][1] - tone[i - 1][1])   # change in news tone just before the window
        ys.append(1.0 if w.up_won else 0.0)
        mids.append(w.pts[0].mid)
    if len(xs) < 200:
        print(f"\nNEWS: only {len(xs)} windows overlap GDELT data; skipped.")
        return
    resid = [y - m for y, m in zip(ys, mids)]   # outcome surprise vs the market's first price
    c = statistics.correlation(xs, resid)
    print(f"\nNEWS (GDELT 'bitcoin' tone, {len(tone)} points, {bot.utc_iso(ts[0])[:10]}..{bot.utc_iso(ts[-1])[:10]}): "
          f"corr(tone change, outcome surprise vs market) = {c:+.3f} over {len(xs)} windows "
          f"(|corr| < {2 / math.sqrt(len(xs)):.3f} is indistinguishable from zero)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--half-spread", type=float, default=HALF_SPREAD)
    ap.add_argument("--tune", action="store_true")
    a = ap.parse_args()
    globals()["HALF_SPREAD"] = a.half_spread
    ds = load(a.rebuild)
    cut = ds[0].start + IS_FRAC * (ds[-1].start - ds[0].start)
    IS, OOS = [w for w in ds if w.start < cut], [w for w in ds if w.start >= cut]
    v1, v2 = v1_config(), bot.Config()
    attach_model(ds, v1)
    n5 = sum(w.duration == "5m" for w in ds)
    print(f"windows: {len(ds)} ({n5} x 5m, {len(ds) - n5} x 15m), quotes: {sum(len(w.pts) for w in ds)}")
    print(f"period: {bot.utc_iso(ds[0].start)} .. {bot.utc_iso(ds[-1].end)}; in-sample < {bot.utc_iso(cut)} "
          f"({len(IS)} IS / {len(OOS)} OOS windows)")
    print(f"assumptions: half-spread {HALF_SPREAD}, taker fee 0.07*p*(1-p), maker fills need the ask to reach our bid\n")
    print("CALIBRATION (lower Brier = better):")
    print("\n".join(calibration(ds)))
    print("\nSTRATEGIES, flat $5 stake:")
    rows = [("always skip", strat_skip, v1, "hold"),
            ("BEFORE: bot v1 (model, taker, v1 exits)", strat_bot, v1, "bot"),
            ("AFTER:  bot v2 (early favorite, v2 exits)", strat_v2, v2, "bot"),
            ("v2 entries, hold to resolution", strat_v2, v2, "hold"),
            ("baseline: follow favorite, any time, hold", make_favorite(0, 900, 0.5, 1.0), v1, "hold"),
            ("baseline: favorite 60-180s left 0.6-0.95", make_favorite(60, 180, 0.6, 0.95), v1, "hold"),
            ("baseline: late momentum <=120s, >=3bp", make_momentum(120, 3), v1, "hold"),
            ("maker: favorite 600-900s 0.85-0.95 (bid)", make_favorite(600, 900, 0.85, 0.95, "maker"), v2, "hold")]
    for name, st, cfg, ex in rows:
        for lab, part in (("IS", IS), ("OOS", OOS)):
            print(fmt(f"{name} [{lab}]", stats(evaluate(part, st, cfg, exits=ex))))
    print("\nPORTFOLIO SIM ($500 start, bot.size_usd sizing, daily-loss / streak / drawdown halts):")
    for name, st, cfg in (("BEFORE v1", strat_bot, v1), ("AFTER  v2", strat_v2, v2)):
        for lab, part in (("IS", IS), ("OOS", OOS), ("ALL", ds)):
            r = portfolio_sim(part, st, cfg)
            print(f"  {name} [{lab:>3}] trades={r['trades']:4d} win={r['win_rate']} pnl=${r['pnl']:>7} "
                  f"final=${r['final_bank']} maxDD={r['max_dd_pct']}% dd_halt={r['dd_halt']}")
    news_check(ds)
    if a.tune:
        tune(IS, OOS, v1)


if __name__ == "__main__":
    main()
