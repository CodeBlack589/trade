#!/usr/bin/env python3
"""delta-trader: market data, analysis, trade planning, simulation and sizing
for Delta Exchange (crypto perpetuals). Standard library only. Python 3.8+.

It READS data and does maths. It never places, edits or cancels an order.

Commands (run with --help on any of them):
  candles   SYMBOL                download OHLCV candles (public, no key)
  analyze   SYMBOL                trend, momentum, volatility, levels, setup flags
  plan      SYMBOL --side long    entry / stop / target  -> delta/plan.json
  simulate                        10,000 replays of the plan from real bars
  size      --capital 1000        contracts for your risk, leverage, liquidation check
  ticket                          print the order for YOU to place on Delta
  balance                         wallet balances (needs DELTA_API_KEY/SECRET)

Settings (env vars):
  DELTA_BASE_URL   default https://api.india.delta.exchange
                   (global: https://api.delta.exchange)
  DELTA_API_KEY / DELTA_API_SECRET   only for `balance`; read-only key recommended
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("DELTA_BASE_URL", "https://api.india.delta.exchange").rstrip("/")
OUT = os.environ.get("DELTA_OUT_DIR", "delta")
UA = "delta-trader/1.0 (python stdlib)"
RES_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
               "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800}
MAX_PER_CALL = 2000
DISCLAIMER = "Education and tooling, not financial advice. Leveraged crypto can lose more than you expect."


class Fail(Exception):
    pass


# ---------------------------------------------------------------- HTTP

def _request(path, params=None, auth=False):
    query = urllib.parse.urlencode(params or {})
    url = BASE + path + ("?" + query if query else "")
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if auth:
        key, secret = os.environ.get("DELTA_API_KEY"), os.environ.get("DELTA_API_SECRET")
        if not key or not secret:
            raise Fail("set DELTA_API_KEY and DELTA_API_SECRET (env vars, never in code or chat)")
        ts = str(int(time.time()))
        payload = "GET" + ts + path + ("?" + query if query else "")
        sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers.update({"api-key": key, "timestamp": ts, "signature": sig})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                body = json.loads(r.read().decode())
            if not body.get("success", True):
                raise Fail("Delta error: %s" % body.get("error"))
            return body.get("result", body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 429 or e.code >= 500:
                last = "HTTP %s %s" % (e.code, detail)
                time.sleep(2 ** attempt)
                continue
            raise Fail("HTTP %s from %s: %s" % (e.code, path, detail))
        except urllib.error.URLError as e:
            last = str(e.reason)
            time.sleep(2 ** attempt)
    raise Fail("could not reach %s (%s)" % (BASE + path, last))


# ---------------------------------------------------------------- data

def fetch_candles(symbol, res, days):
    if res not in RES_SECONDS:
        raise Fail("resolution must be one of " + ", ".join(RES_SECONDS))
    step = RES_SECONDS[res]
    end = int(time.time())
    start = end - int(days * 86400)
    rows, cursor = {}, start
    while cursor < end:
        chunk_end = min(end, cursor + step * MAX_PER_CALL)
        result = _request("/v2/history/candles",
                          {"resolution": res, "symbol": symbol, "start": cursor, "end": chunk_end})
        for c in result or []:
            rows[int(c["time"])] = c
        cursor = chunk_end
        time.sleep(0.25)
    return normalise(rows.values())


def normalise(raw):
    bars = []
    for c in raw:
        try:
            bars.append({"t": int(c["time"]), "o": float(c["open"]), "h": float(c["high"]),
                         "l": float(c["low"]), "c": float(c["close"]),
                         "v": float(c.get("volume") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["t"])
    if not bars:
        raise Fail("no candles returned (check the symbol, e.g. BTCUSD, and the resolution)")
    return bars


def load_candles(args):
    if getattr(args, "offline", None):
        with open(args.offline) as f:
            data = json.load(f)
        return normalise(data.get("result", data) if isinstance(data, dict) else data)
    return fetch_candles(args.symbol, args.res, args.days)


def product(symbol):
    return _request("/v2/products/" + urllib.parse.quote(symbol))


def ticker(symbol):
    return _request("/v2/tickers/" + urllib.parse.quote(symbol))


# ---------------------------------------------------------------- indicators

def ema(values, n):
    if len(values) < n:
        return [None] * len(values)
    k = 2 / (n + 1)
    out = [None] * (n - 1) + [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(bars, n=14):
    if len(bars) <= n:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
           for p, b in zip(bars, bars[1:])]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def swings(bars, k=3, limit=4):
    """Pivot highs/lows: a bar higher (lower) than k bars each side."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i]["h"] == max(b["h"] for b in win):
            highs.append(bars[i]["h"])
        if bars[i]["l"] == min(b["l"] for b in win):
            lows.append(bars[i]["l"])
    return highs[-limit:], lows[-limit:]


def analyse(bars, res):
    closes = [b["c"] for b in bars]
    last = bars[-1]
    per_day = max(1, 86400 // RES_SECONDS.get(res, 3600))
    e20, e50, e200 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
    r = rsi(closes)
    a = atr(bars)
    look = bars[-21:-1] if len(bars) > 21 else bars[:-1]
    hi20, lo20 = max(b["h"] for b in look), min(b["l"] for b in look)
    ago = bars[-1 - per_day] if len(bars) > per_day else bars[0]
    rets = [math.log(b["c"] / p["c"]) for p, b in zip(bars, bars[1:]) if p["c"] > 0]
    vol_ann = statistics.pstdev(rets) * math.sqrt(per_day * 365) * 100 if len(rets) > 2 else None
    vols = [b["v"] for b in bars[-21:-1]]
    vol_ratio = last["v"] / (sum(vols) / len(vols)) if vols and sum(vols) else None
    sh, sl = swings(bars)

    if None not in (e20, e50, e200):
        if last["c"] > e20 > e50 > e200:
            trend = "uptrend (price > EMA20 > EMA50 > EMA200)"
        elif last["c"] < e20 < e50 < e200:
            trend = "downtrend (price < EMA20 < EMA50 < EMA200)"
        else:
            trend = "mixed / ranging (EMAs not stacked)"
    elif None not in (e20, e50):
        trend = "up (EMA20 > EMA50)" if e20 > e50 else "down (EMA20 < EMA50)"
    else:
        trend = "not enough bars"

    flags = []
    if last["c"] > hi20:
        flags.append("BREAKOUT: closed above the 20-bar high %.2f" % hi20)
    if last["c"] < lo20:
        flags.append("BREAKDOWN: closed below the 20-bar low %.2f" % lo20)
    if e20 and e50 and e20 > e50 and a and abs(last["c"] - e20) < 0.5 * a:
        flags.append("PULLBACK: within 0.5 ATR of a rising EMA20")
    if e20 and e50 and e20 < e50 and a and abs(last["c"] - e20) < 0.5 * a:
        flags.append("RALLY INTO RESISTANCE: within 0.5 ATR of a falling EMA20")
    if r is not None and r < 30:
        flags.append("OVERSOLD: RSI %.1f < 30" % r)
    if r is not None and r > 70:
        flags.append("OVERBOUGHT: RSI %.1f > 70" % r)
    if vol_ratio and vol_ratio > 2:
        flags.append("VOLUME: last bar %.1fx the 20-bar average" % vol_ratio)

    return {
        "as_of": iso(last["t"]), "bars": len(bars), "resolution": res,
        "last": last["c"], "change_24h_pct": (last["c"] / ago["c"] - 1) * 100,
        "ema20": e20, "ema50": e50, "ema200": e200, "rsi14": r, "atr14": a,
        "atr_pct": a / last["c"] * 100 if a else None,
        "high20": hi20, "low20": lo20, "swing_highs": sh, "swing_lows": sl,
        "volatility_annual_pct": vol_ann, "volume_ratio": vol_ratio,
        "trend": trend, "flags": flags,
    }


# ---------------------------------------------------------------- plan / sim / size

def make_plan(symbol, side, entry, stop_rule, target_rule, a, swing_lo, swing_hi):
    long = side == "long"
    kind, _, val = stop_rule.partition(":")
    if kind == "atr":
        m = float(val or 1.5)
        if not a:
            raise Fail("no ATR available; use --stop price:X or pct:Y")
        stop = entry - m * a if long else entry + m * a
    elif kind == "pct":
        p = float(val or 1) / 100
        stop = entry * (1 - p) if long else entry * (1 + p)
    elif kind == "swing":
        stop = swing_lo if long else swing_hi
        if stop is None:
            raise Fail("no swing level found; use another --stop")
    elif kind == "price":
        stop = float(val)
    else:
        raise Fail("--stop must be atr:1.5, pct:1, swing or price:12345")
    risk = (entry - stop) if long else (stop - entry)
    if risk <= 0:
        raise Fail("stop %.2f is on the wrong side of entry %.2f for a %s" % (stop, entry, side))
    if target_rule.upper().endswith("R"):
        mult = float(target_rule[:-1])
        target = entry + mult * risk if long else entry - mult * risk
    else:
        target = float(target_rule)
    reward = (target - entry) if long else (entry - target)
    if reward <= 0:
        raise Fail("target %.2f is on the wrong side of entry for a %s" % (target, side))
    return {"symbol": symbol, "side": side, "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2), "risk_per_unit": round(risk, 2),
            "reward_risk": round(reward / risk, 2), "created": iso(time.time())}


def simulate(bars, plan, horizon, replays=10000, seed=19, block=6):
    """Block-bootstrap the bar-by-bar returns (keeps short-term clustering),
    replay `horizon` bars from entry, record which level is touched first.
    If a bar touches both, it counts as the stop (conservative)."""
    rets = []
    for p, b in zip(bars, bars[1:]):
        if p["c"] > 0:
            rets.append((b["h"] / p["c"], b["l"] / p["c"], b["c"] / p["c"]))
    if len(rets) < block * 4:
        raise Fail("need more history to simulate (got %d bars)" % len(bars))
    long = plan["side"] == "long"
    E, S, T = plan["entry"], plan["stop"], plan["target"]
    rng = random.Random(seed)
    hit_t = hit_s = 0
    finals, bars_to = [], []
    for _ in range(replays):
        price, outcome, n = E, None, 0
        while n < horizon and outcome is None:
            i = rng.randrange(0, len(rets) - block)
            for hm, lm, cm in rets[i:i + block]:
                hi, lo = price * hm, price * lm
                n += 1
                if long:
                    if lo <= S: outcome = "stop"
                    elif hi >= T: outcome = "target"
                else:
                    if hi >= S: outcome = "stop"
                    elif lo <= T: outcome = "target"
                price *= cm
                if outcome or n >= horizon:
                    break
        if outcome == "target":
            hit_t += 1; bars_to.append(n)
        elif outcome == "stop":
            hit_s += 1; bars_to.append(n)
        else:
            finals.append(price)
    risk = abs(E - S)
    open_r = [((f - E) if long else (E - f)) / risk for f in finals]
    rr = plan["reward_risk"]
    exp_r = (hit_t * rr - hit_s * 1 + sum(open_r)) / replays
    return {"replays": replays, "seed": seed, "horizon_bars": horizon,
            "target_first_pct": hit_t / replays * 100, "stop_first_pct": hit_s / replays * 100,
            "neither_pct": len(finals) / replays * 100,
            "median_bars_to_exit": statistics.median(bars_to) if bars_to else None,
            "expected_R": exp_r,
            "breakeven_win_rate_pct": 100 / (1 + rr)}


def backtest_rule(bars, side, stop_atr, rr, horizon):
    """Honest history: every bar that closed above its 20-bar high (long) or
    below its 20-bar low (short) is an entry; stop = stop_atr x ATR; target = rr R."""
    wins = losses = neither = 0
    i = 35
    while i < len(bars) - 1:
        look = bars[i - 20:i]
        a = atr(bars[i - 30:i + 1])
        c = bars[i]["c"]
        trig = c > max(b["h"] for b in look) if side == "long" else c < min(b["l"] for b in look)
        if not trig or not a:
            i += 1
            continue
        risk = stop_atr * a
        S = c - risk if side == "long" else c + risk
        T = c + rr * risk if side == "long" else c - rr * risk
        out = None
        for j in range(i + 1, min(len(bars), i + 1 + horizon)):
            b = bars[j]
            if side == "long":
                if b["l"] <= S: out = "L"
                elif b["h"] >= T: out = "W"
            else:
                if b["h"] >= S: out = "L"
                elif b["l"] <= T: out = "W"
            if out:
                break
        wins += out == "W"; losses += out == "L"; neither += out is None
        i = j + 1 if out else i + horizon
    n = wins + losses + neither
    return {"signals": n, "target_first": wins, "stop_first": losses, "neither": neither,
            "win_rate_pct": wins / n * 100 if n else None}


def size(plan, capital, risk_pct, leverage, contract_value, mm_pct, fee_pct):
    risk_cash = capital * risk_pct / 100
    per_contract_loss = plan["risk_per_unit"] * contract_value
    fee_per_contract = plan["entry"] * contract_value * fee_pct / 100 * 2  # in + out
    contracts = int(risk_cash // (per_contract_loss + fee_per_contract))
    notional = contracts * plan["entry"] * contract_value
    margin = notional / leverage
    long = plan["side"] == "long"
    # isolated-margin approximation: liquidation when loss eats margin minus maintenance
    liq = plan["entry"] * (1 - 1 / leverage + mm_pct / 100) if long \
        else plan["entry"] * (1 + 1 / leverage - mm_pct / 100)
    stop_before_liq = plan["stop"] > liq if long else plan["stop"] < liq
    warnings = []
    if contracts < 1:
        warnings.append("risk budget too small for even 1 contract at this stop: widen the budget or tighten the stop")
    if margin > capital:
        warnings.append("margin %.2f is more than your capital: lower size or raise leverage" % margin)
    if not stop_before_liq:
        warnings.append("DANGER: estimated liquidation %.2f is hit BEFORE your stop. Lower the leverage." % liq)
    return {"contracts": contracts, "contract_value": contract_value, "notional": round(notional, 2),
            "margin_required": round(margin, 2), "leverage": leverage,
            "max_loss_at_stop": round(contracts * (per_contract_loss + fee_per_contract), 2),
            "risk_budget": round(risk_cash, 2), "est_liquidation": round(liq, 2),
            "stop_before_liquidation": stop_before_liq, "warnings": warnings}


# ---------------------------------------------------------------- helpers

def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def save(name, data):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def load(name):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        raise Fail("%s not found: run the previous step first" % path)
    with open(path) as f:
        return json.load(f)


def f2(x, d=2):
    return "n/a" if x is None else ("{:,.%df}" % d).format(x)


# ---------------------------------------------------------------- commands

def cmd_candles(a):
    bars = load_candles(a)
    if a.save:
        with open(a.save, "w") as f:
            json.dump([{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"],
                        "close": b["c"], "volume": b["v"]} for b in bars], f)
        print("saved %d candles to %s" % (len(bars), a.save))
    for b in bars[-a.tail:]:
        print("%s  O %s  H %s  L %s  C %s  V %s" % (iso(b["t"]), f2(b["o"]), f2(b["h"]),
                                                    f2(b["l"]), f2(b["c"]), f2(b["v"], 0)))


def cmd_analyze(a):
    bars = load_candles(a)
    r = analyse(bars, a.res)
    r["symbol"] = a.symbol
    save("analysis.json", r)
    if a.json:
        print(json.dumps(r, indent=2)); return
    print("%s  %s candles, %d bars, as of %s" % (a.symbol, a.res, r["bars"], r["as_of"]))
    print("  last        %s   (%+.2f%% vs 24h ago)" % (f2(r["last"]), r["change_24h_pct"]))
    print("  trend       %s" % r["trend"])
    print("  EMA 20/50/200  %s / %s / %s" % (f2(r["ema20"]), f2(r["ema50"]), f2(r["ema200"])))
    print("  RSI14       %s" % f2(r["rsi14"], 1))
    print("  ATR14       %s  (%s%% of price)" % (f2(r["atr14"]), f2(r["atr_pct"])))
    print("  20-bar range  %s - %s" % (f2(r["low20"]), f2(r["high20"])))
    print("  swing highs %s" % ", ".join(f2(x) for x in r["swing_highs"]))
    print("  swing lows  %s" % ", ".join(f2(x) for x in r["swing_lows"]))
    print("  volatility  %s%% annualised" % f2(r["volatility_annual_pct"], 1))
    print("  flags       " + ("\n              ".join(r["flags"]) or "none"))
    print("\nThese are measurements, not a buy or sell call. " + DISCLAIMER)


def cmd_plan(a):
    an = load("analysis.json") if not a.offline else analyse(load_candles(a), a.res)
    entry = a.entry if a.entry else an["last"]
    lo = an["swing_lows"][-1] if an.get("swing_lows") else None
    hi = an["swing_highs"][-1] if an.get("swing_highs") else None
    p = make_plan(a.symbol, a.side, entry, a.stop, a.target, an.get("atr14"), lo, hi)
    path = save("plan.json", p)
    print("%s %s  entry %s  stop %s  target %s  risk/unit %s  R:R %s" % (
        p["side"].upper(), p["symbol"], f2(p["entry"]), f2(p["stop"]), f2(p["target"]),
        f2(p["risk_per_unit"]), p["reward_risk"]))
    print("saved " + path)


def cmd_simulate(a):
    plan = load("plan.json")
    a.symbol = plan["symbol"]
    bars = load_candles(a)
    s = simulate(bars, plan, a.horizon, a.replays, a.seed)
    stop_atr = abs(plan["entry"] - plan["stop"]) / (atr(bars) or 1)
    h = backtest_rule(bars, plan["side"], stop_atr, plan["reward_risk"], a.horizon)
    s["history"] = h
    save("simulation.json", s)
    if a.json:
        print(json.dumps(s, indent=2)); return
    print("%s %s: %d replays of %d x %s bars from %d real bars (seed %d)" % (
        plan["side"].upper(), plan["symbol"], s["replays"], a.horizon, a.res, len(bars), a.seed))
    print("  target first  %5.1f%%" % s["target_first_pct"])
    print("  stop first    %5.1f%%" % s["stop_first_pct"])
    print("  neither       %5.1f%%   (still open at the horizon)" % s["neither_pct"])
    print("  expected R    %+.2f   (break-even needs %.1f%% target-first at R:R %s)" % (
        s["expected_R"], s["breakeven_win_rate_pct"], plan["reward_risk"]))
    print("  history: 20-bar %s rule, same stop/target, fired %d times: %d target, %d stop, %d neither" % (
        "breakout" if plan["side"] == "long" else "breakdown",
        h["signals"], h["target_first"], h["stop_first"], h["neither"]))
    edge = s["expected_R"]
    verdict = ("no edge in the replays; this is a coin flip or worse" if edge <= 0.05
               else "small positive edge in the replays, well inside noise for one trade" if edge < 0.25
               else "replays lean positive; history above shows whether the rule agrees")
    print("  verdict: " + verdict)
    print("\nA simulation from recent bars, not a prediction. " + DISCLAIMER)


def cmd_size(a):
    plan = load("plan.json")
    cv = a.contract_value
    if cv is None:
        try:
            cv = float(product(plan["symbol"])["contract_value"])
        except Exception as e:
            raise Fail("could not read contract_value from Delta (%s); pass --contract-value" % e)
    s = size(plan, a.capital, a.risk, a.leverage, cv, a.maint_margin, a.fee)
    out = dict(plan=plan, sizing=s)
    save("order.json", out)
    print("%s %s: %d contracts (1 contract = %s %s)" % (
        plan["side"].upper(), plan["symbol"], s["contracts"], cv, plan["symbol"][:3]))
    print("  notional %s  margin %s at %sx" % (f2(s["notional"]), f2(s["margin_required"]), a.leverage))
    print("  max loss at stop %s of %s budget (incl. est. fees)" % (f2(s["max_loss_at_stop"]), f2(s["risk_budget"])))
    print("  est. liquidation %s   stop hit first: %s" % (f2(s["est_liquidation"]),
                                                        "yes" if s["stop_before_liquidation"] else "NO"))
    for w in s["warnings"]:
        print("  ! " + w)


def cmd_ticket(a):
    o = load("order.json")
    p, s = o["plan"], o["sizing"]
    if s["contracts"] < 1 or not s["stop_before_liquidation"]:
        print("NOT READY: " + "; ".join(s["warnings"])); return 1
    side = "buy" if p["side"] == "long" else "sell"
    print("ORDER TICKET (place it yourself on Delta; this tool sends nothing)")
    print("  %s %d %s  limit ~%s" % (side.upper(), s["contracts"], p["symbol"], f2(p["entry"])))
    print("  bracket stop-loss   %s" % f2(p["stop"]))
    print("  bracket take-profit %s" % f2(p["target"]))
    print("  leverage %sx, isolated margin; max loss at stop ~%s" % (s["leverage"], f2(s["max_loss_at_stop"])))
    print("\nEquivalent POST /v2/orders body, for reference only:")
    print(json.dumps({"product_symbol": p["symbol"], "size": s["contracts"], "side": side,
                      "order_type": "limit_order", "limit_price": str(p["entry"]),
                      "bracket_stop_loss_price": str(p["stop"]),
                      "bracket_take_profit_price": str(p["target"])}, indent=2))


def cmd_balance(a):
    for w in _request("/v2/wallet/balances", auth=True) or []:
        bal = float(w.get("balance") or 0)
        if bal:
            print("%-6s balance %s  available %s" % (w.get("asset_symbol"), w.get("balance"),
                                                     w.get("available_balance")))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def data_args(p, sym=True):
        if sym:
            p.add_argument("symbol", nargs="?", default="BTCUSD")
        p.add_argument("--res", default="1h", help="1m 5m 15m 1h 4h 1d ... (default 1h)")
        p.add_argument("--days", type=float, default=30, help="history to fetch (default 30)")
        p.add_argument("--offline", help="read candles from a saved JSON file instead of the API")

    p = sub.add_parser("candles"); data_args(p)
    p.add_argument("--save"); p.add_argument("--tail", type=int, default=10)
    p.set_defaults(fn=cmd_candles)

    p = sub.add_parser("analyze"); data_args(p); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("plan"); data_args(p)
    p.add_argument("--side", choices=["long", "short"], required=True)
    p.add_argument("--entry", type=float, help="default: last price from analyze")
    p.add_argument("--stop", default="atr:1.5", help="atr:1.5 | pct:1 | swing | price:X")
    p.add_argument("--target", default="2R", help="2R | 1.5R | price")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("simulate"); data_args(p, sym=False)
    p.add_argument("--horizon", type=int, default=24, help="bars to hold at most (default 24)")
    p.add_argument("--replays", type=int, default=10000)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_simulate)

    p = sub.add_parser("size")
    p.add_argument("--capital", type=float, required=True, help="account size in USD")
    p.add_argument("--risk", type=float, default=1.0, help="%% of capital to lose at the stop (default 1)")
    p.add_argument("--leverage", type=float, default=5)
    p.add_argument("--contract-value", type=float, help="BTC per contract (default: read from Delta)")
    p.add_argument("--maint-margin", type=float, default=0.5, help="maintenance margin %% (default 0.5)")
    p.add_argument("--fee", type=float, default=0.05, help="taker fee %% per side (default 0.05)")
    p.set_defaults(fn=cmd_size)

    sub.add_parser("ticket").set_defaults(fn=cmd_ticket)
    sub.add_parser("balance").set_defaults(fn=cmd_balance)

    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except Fail as e:
        print("delta.py: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
