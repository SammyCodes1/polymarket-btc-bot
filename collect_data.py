#!/usr/bin/env python3
"""Download history for backtest.py (cached under data/backtest/).

  - Gamma: resolved BTC Up/Down 5m + 15m windows (outcome, tokens, start/end)
  - CLOB:  /prices-history (≈1-minute price path) for each window's Up token
  - Coinbase: 1-minute BTC-USD candles covering the whole period

Usage: python collect_data.py --days 14 [--workers 8]
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

OUT = Path(__file__).with_name("data") / "backtest"
OUT.mkdir(parents=True, exist_ok=True)
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (pm-btc-backtest)"


def get(url, params=None, tries=5):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_windows(series, start_ts, end_ts):
    rows, cursor = {}, start_ts
    while cursor < end_ts:
        page = get(f"{GAMMA}/events", {"series_slug": series, "closed": "true", "end_date_min": iso(cursor),
                                       "end_date_max": iso(end_ts), "order": "endDate", "ascending": "true", "limit": 100})
        if not page:
            break
        last = cursor
        for ev in page:
            for m in ev.get("markets", []):
                try:
                    prices = [float(x) for x in json.loads(m["outcomePrices"])]
                    outcomes = [o.lower() for o in json.loads(m["outcomes"])]
                    toks = json.loads(m["clobTokenIds"])
                    end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp()
                    start = datetime.fromisoformat((m.get("eventStartTime") or ev.get("startTime")).replace("Z", "+00:00")).timestamp()
                    if sorted(prices) != [0.0, 1.0]:
                        continue
                    rows[m["slug"]] = {
                        "slug": m["slug"], "duration": "5m" if "5m" in series else "15m",
                        "start": start, "end": end, "up_won": prices[outcomes.index("up")] == 1.0,
                        "up_token": toks[outcomes.index("up")], "down_token": toks[outcomes.index("down")],
                        "fee_rate": float((m.get("feeSchedule") or {}).get("rate", 0.07)) if m.get("feesEnabled", True) else 0.0,
                        "volume": float(m.get("volumeNum") or 0),
                    }
                    last = max(last, end)
                except Exception:
                    pass
        if last <= cursor:
            break
        cursor = last + 1
        print(f"  {series}: {len(rows)} windows (through {iso(cursor)})", flush=True)
    return list(rows.values())


def fetch_history(w):
    p = OUT / "prices" / f"{w['slug']}.json"
    if p.exists():
        return
    h = get(f"{CLOB}/prices-history", {"market": w["up_token"], "startTs": int(w["start"]) - 60,
                                       "endTs": int(w["end"]) + 60, "fidelity": 1})
    p.write_text(json.dumps(h.get("history", [])))


def fetch_candles(start_ts, end_ts):
    out = {}
    t = start_ts - 3 * 3600
    while t < end_ts:
        e = min(t + 300 * 60, end_ts)
        data = get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                   {"granularity": 60, "start": iso(t), "end": iso(e)})
        for c in data:   # [time, low, high, open, close, volume]
            out[int(c[0])] = [float(c[3]), float(c[2]), float(c[1]), float(c[4]), float(c[5])]
        t = e
        time.sleep(0.15)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    end_ts = time.time() - 600
    start_ts = end_ts - a.days * 86400
    wins = []
    for series in ("btc-up-or-down-5m", "btc-up-or-down-15m"):
        wins += fetch_windows(series, start_ts, end_ts)
    (OUT / "windows.json").write_text(json.dumps(wins))
    print(f"windows: {len(wins)}", flush=True)
    (OUT / "prices").mkdir(exist_ok=True)
    done = 0
    with ThreadPoolExecutor(a.workers) as ex:
        for _ in ex.map(lambda w: _safe(fetch_history, w), wins):
            done += 1
            if done % 250 == 0:
                print(f"  price histories: {done}/{len(wins)}", flush=True)
    candles = fetch_candles(int(start_ts), int(end_ts))
    (OUT / "candles_1m.json").write_text(json.dumps(candles))
    print(f"candles: {len(candles)}  done.", flush=True)


def _safe(f, w):
    try:
        f(w)
    except Exception as e:
        print("  fail", w["slug"], e, flush=True)


if __name__ == "__main__":
    main()
