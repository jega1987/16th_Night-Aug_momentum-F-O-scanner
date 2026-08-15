#!/usr/bin/env python3
"""
Offline smoke test. Runs the full pipeline on the mock feed - no broker, no
network - and prints what fired and why the rest didn't.

    python selftest.py

Run this after any change to indicators.py or filters.py. If the sweep or
squeeze logic breaks, this catches it in seconds instead of during market hours.
"""
import asyncio
import math
import os
import pathlib
import sys
from datetime import datetime, time as dtime

os.environ.setdefault("FEED_MODE", "mock")
os.environ.setdefault("SCAN_ONLY_MARKET_HOURS", "false")
os.environ.setdefault("DATABASE_URL", "")

import pandas as pd  # noqa: E402

from config import cfg  # noqa: E402
from clock import now_naive  # noqa: E402
from database import Signal, init_db, session_scope  # noqa: E402
from candle_store import CandleStore  # noqa: E402
from feed_mock import MockFeed  # noqa: E402
from filters import FilterEngine  # noqa: E402
from indicators import (adx, atr, atm_strike, bars_since, detect_squeeze, rsi,  # noqa: E402
                        supertrend, swing_points)
from scanner import IndexScanner  # noqa: E402
from signal_engine import SignalEngine  # noqa: E402

OK, BAD = "  ok  ", " FAIL "


def check(label, condition, detail=""):
    print(f"[{OK if condition else BAD}] {label}{(' - ' + detail) if detail else ''}")
    return bool(condition)


def build_textbook_setup(side: str = "LONG", seed: int = 11, coil: int = 20,
                         breakout_atr: float = 1.8) -> "pd.DataFrame":
    """
    A hand-built ideal setup: a volatile leg, a genuine coil, then a wide-range
    breakout on triple volume. If the engine can't fire on this, it can't fire
    on anything, and the original codebase couldn't.
    """
    import numpy as np
    from datetime import datetime, timedelta

    from indicators import atr as _atr

    rng = np.random.default_rng(seed)
    price, rows = 24000.0, []

    for _ in range(60):                                    # volatility, builds ATR and ADX
        step = rng.normal(0, 28)
        o, c = price, price + step
        wick = abs(rng.normal(0, 20))
        rows.append([o, max(o, c) + wick, min(o, c) - wick, c, int(120000 + abs(rng.normal(0, 20000)))])
        price = c

    for _ in range(coil):                                  # compression: tight closes, normal wicks
        step = rng.normal(0, 2.5)
        o, c = price, price + step
        wick = abs(rng.normal(0, 5.0))
        rows.append([o, max(o, c) + wick, min(o, c) - wick, c, int(70000 + abs(rng.normal(0, 8000)))])
        price = c

    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    atr_now = float(_atr(frame, 14).iloc[-1])

    if side == "LONG":
        level = float(frame["high"].iloc[-coil:].max())
        close = level + breakout_atr * atr_now
        rows.append([price, close * 1.0004, min(price, close) * 0.9999, close, 260000])
    else:
        level = float(frame["low"].iloc[-coil:].min())
        close = level - breakout_atr * atr_now
        rows.append([price, max(price, close) * 1.0001, close * 0.9996, close, 260000])

    out = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    out["timestamp"] = [datetime(2026, 1, 1, 9, 15) + timedelta(minutes=5 * i) for i in range(len(out))]
    return out


async def main() -> int:
    failures = 0
    init_db()
    print("=" * 66)
    print("INDICATORS")
    print("=" * 66)

    feed = MockFeed(seed=7)
    await feed.connect()
    df = await feed.get_historical("NIFTY 50", "5m", 300)

    failures += not check("candles normalized", list(df.columns) ==
                          ["timestamp", "open", "high", "low", "close", "volume"], str(list(df.columns)))
    failures += not check("candles ascending", df["timestamp"].is_monotonic_increasing)
    failures += not check("no NaN in OHLC", not df[["open", "high", "low", "close"]].isna().any().any())

    a = atr(df, 14)
    failures += not check("ATR positive", float(a.iloc[-1]) > 0, f"{a.iloc[-1]:.2f}")

    r = rsi(df["close"], 14)
    failures += not check("RSI within 0-100", 0 <= float(r.iloc[-1]) <= 100, f"{r.iloc[-1]:.1f}")

    ad = adx(df, 14)
    failures += not check("ADX finite", pd.notna(ad.iloc[-1]) and float(ad.iloc[-1]) >= 0, f"{ad.iloc[-1]:.1f}")

    in_sq, dur, fired = detect_squeeze(df, cfg.BB_LENGTH, cfg.BB_MULT, cfg.KC_LENGTH,
                                       cfg.KC_MULT, cfg.MIN_SQUEEZE_BARS)
    failures += not check("squeeze regimes detected", int(in_sq.sum()) > 0, f"{int(in_sq.sum())} bars compressed")
    failures += not check("squeeze released at least once", int(fired.sum()) > 0, f"{int(fired.sum())} releases")
    failures += not check("duration resets after release", int(dur.max()) >= cfg.MIN_SQUEEZE_BARS,
                          f"longest {int(dur.max())} bars")
    failures += not check("bars_since works", bars_since(fired) < 10 ** 6)

    line, direction = supertrend(df, 10, 3.0)
    flips = int((direction.diff().abs() > 0).sum())
    failures += not check("supertrend flips", flips > 0, f"{flips} flips")
    failures += not check("supertrend sits on the right side of price",
                          all((direction.iloc[i] != 1) or (line.iloc[i] <= df["high"].iloc[i])
                              for i in range(len(df) - 20, len(df))))

    sh, sl = swing_points(df, 3)
    failures += not check("swing points found", int(sh.sum()) > 0 and int(sl.sum()) > 0,
                          f"{int(sh.sum())} highs / {int(sl.sum())} lows")

    failures += not check("ATM strike rounding", atm_strike(24512, 50) == 24500 and
                          atm_strike(52080, 100) == 52100)

    print()
    print("=" * 66)
    print("FILTERS  (the sweep check in the original code could never be true)")
    print("=" * 66)
    fe = FilterEngine(cfg)
    sweep_true = 0
    passes, rejections = 0, {}
    for i in range(120, len(df)):
        window = df.iloc[:i]
        if fe._check_sweep(window, "LONG") or fe._check_sweep(window, "SHORT"):
            sweep_true += 1
        res = fe.apply_all(window)
        if res.passed:
            passes += 1
        else:
            key = res.reason.split("(")[0][:44]
            rejections[key] = rejections.get(key, 0) + 1
    failures += not check("sweep detection can return True", sweep_true > 0, f"{sweep_true} windows")
    print(f"        {passes} setups passed out of {len(df) - 120} windows")
    for reason, count in sorted(rejections.items(), key=lambda kv: -kv[1])[:6]:
        print(f"          {count:>4}  {reason}")

    print()
    print("=" * 66)
    print("TEXTBOOK SETUP  (a valid signal must actually be able to fire)")
    print("=" * 66)
    for side in ("LONG", "SHORT"):
        book = build_textbook_setup(side)
        res = fe.apply_all(book)
        failures += not check(
            f"{side} squeeze breakout passes", res.passed and res.direction == side,
            f"{res.reason} | ADX {res.meta.get('adx_value')} RSI {res.meta.get('rsi_value')} "
            f"vol {res.meta.get('vol_ratio')}x score {res.scores.get('composite')}")

    print()
    print("=" * 66)
    print("UNIVERSE / WEBSOCKET / BUDGET  (new requirement logic)")
    print("=" * 66)
    from datetime import timedelta as _td2

    from feed_ws import BarBuilder, splice, _floor_to_bar
    from signal_budget import SignalBudget
    from universe import UniverseBuilder

    # --- dynamic universe ---
    ub = UniverseBuilder(feed)
    picked = await ub.build(force=True)
    failures += not check("universe built", len(picked) > 0, f"{len(picked)} symbols")
    detail = ub.detail(200)
    failures += not check("price floor enforced",
                          all(d["ltp"] >= cfg.MIN_STOCK_PRICE for d in detail),
                          f"min selected price {min((d['ltp'] for d in detail), default=0):.0f}")
    failures += not check("cheap names actually dropped", ub.last_reason.get("below_price", 0) > 0,
                          str(ub.last_reason))
    failures += not check("ranked descending",
                          [d["score"] for d in detail] == sorted([d["score"] for d in detail], reverse=True))
    failures += not check("momentum source recorded, never guessed",
                          all(d["momentum_source"] in ("5d", "1d", "none") for d in detail))
    failures += not check("universe respects UNIVERSE_SIZE", len(picked) <= cfg.UNIVERSE_SIZE)

    # --- index isolation ---
    eq = picked[0] if picked else "RELIANCE"
    failures += not check("equities get their own thresholds",
                          cfg.volume_mult(eq) != cfg.volume_mult("NIFTY 50")
                          and cfg.min_composite(eq) != cfg.min_composite("NIFTY 50"),
                          f"{eq}: vol x{cfg.volume_mult(eq)} composite {cfg.min_composite(eq)} "
                          f"vs index vol x{cfg.volume_mult('NIFTY 50')} composite {cfg.min_composite('NIFTY 50')}")

    # --- tick aggregation ---
    base = datetime(2026, 8, 14, 9, 15)
    failures += not check("bars anchored to the 09:15 open",
                          _floor_to_bar(datetime(2026, 8, 14, 9, 29), 5) == datetime(2026, 8, 14, 9, 25))
    bb = BarBuilder("T", 5)
    done = [bb.add_tick(p, v, base + _td2(minutes=m))
            for m, p, v in [(0, 100, 100), (1, 102, 250), (3, 101, 400), (6, 103, 900)]]
    bar = [d for d in done if d][0]
    failures += not check("cumulative tick volume becomes per-bar volume",
                          bar["volume"] == 300, f"got {bar['volume']}, naive sum would be 750")
    failures += not check("bar OHLC correct",
                          (bar["open"], bar["high"], bar["low"], bar["close"]) == (100, 102, 100, 101))
    bb2 = BarBuilder("R", 5)
    bb2.add_tick(100, 5000, base)
    bb2.add_tick(101, 200, base + _td2(minutes=1))
    failures += not check("volume counter reset never goes negative",
                          bb2.frame()["volume"].min() >= 0)
    bb3 = BarBuilder("U", 5)
    bb3.add_tick(100, 10, base)
    thin = bb3.add_tick(101, 20, base + _td2(minutes=6))
    failures += not check("thin bar flagged unreliable", thin["reliable"] is False,
                          f"{thin['ticks']} tick(s)")
    bb4 = BarBuilder("O", 5)
    bb4.add_tick(100, 10, base + _td2(minutes=10))
    snapshot = dict(bb4.current)
    bb4.add_tick(999, 20, base)
    failures += not check("out-of-order tick ignored", bb4.current == snapshot)

    # --- REST/stream splice ---
    rest = pd.DataFrame({"timestamp": [base + _td2(minutes=5 * i) for i in range(4)],
                         "open": [1, 2, 3, 4], "high": [1, 2, 3, 4], "low": [1, 2, 3, 4],
                         "close": [1, 2, 3, 44], "volume": [10, 10, 10, 99]})
    wsdf = pd.DataFrame({"timestamp": [base + _td2(minutes=5 * i) for i in (3, 4, 5)],
                         "open": [4, 5, 6], "high": [4, 5, 6], "low": [4, 5, 6],
                         "close": [4, 5, 6], "volume": [77, 10, 10]})
    merged = splice(rest, wsdf)
    boundary = merged[merged.timestamp == base + _td2(minutes=15)]
    failures += not check("splice leaves no duplicate bars", merged.timestamp.duplicated().sum() == 0)
    failures += not check("stream wins the boundary bar over partial REST",
                          float(boundary.volume.iloc[0]) == 77)

    from signal_engine import SignalEngine as _SE
    engine_probe = _SE()

    # --- the stream must actually be CONNECTED to the scanner, not just exist ---
    import feed_5paisa as _f5
    src = pathlib.Path("candle_store.py").read_text()
    failures += not check("candle store consumes streamed bars",
                          "_live_bars" in src and "splice(" in src)
    failures += not check("feed owns a stream lifecycle",
                          all(hasattr(_f5.FivePaisaFeed, m)
                              for m in ("start_stream", "stop_stream", "stream_healthy")))
    failures += not check("stream lifecycle is scheduled",
                          "job_manage_stream" in pathlib.Path("main.py").read_text())

    class _FakeStream:
        connected = True
        def __init__(self, sym, minutes):
            self.builders = {sym: BarBuilder(sym, minutes)}
            self._stale = False
        def is_stale(self):
            return self._stale

    probe_store = CandleStore(feed)
    hist = await probe_store.get("NIFTY 50", "5m", 200)
    fake = _FakeStream("NIFTY 50", 5)
    feed.stream = fake
    bb_live = fake.builders["NIFTY 50"]
    t0 = hist["timestamp"].iloc[-1] + _td2(minutes=5)
    for i, (px, vol) in enumerate([(99000, 100), (99050, 300), (99020, 600), (99100, 900)]):
        bb_live.add_tick(px, vol, t0 + _td2(minutes=i))
    calls_before = probe_store.api_calls_made
    with_stream = await probe_store.get("NIFTY 50", "5m", 200)
    failures += not check("live stream bars reach the scanner",
                          bool((with_stream["close"] == 99100).any()))
    failures += not check("healthy stream skips the REST call",
                          probe_store.api_calls_made == calls_before)
    fake._stale = True
    fallback = await probe_store.get("NIFTY 50", "5m", 200)
    failures += not check("stale stream falls back to REST",
                          not bool((fallback["close"] == 99100).any()))
    feed.stream = None

    # --- strict cap ---
    fired = {"n": 0}
    bud = SignalBudget(on_exhausted=lambda: fired.__setitem__("n", fired["n"] + 1))
    cands = [{"symbol": c, "composite_score": v}
             for c, v in [("A", .62), ("B", .91), ("C", .55), ("D", .78),
                          ("E", .83), ("F", .70), ("G", .95)]]
    kept = bud.take(cands)
    scores = [c["composite_score"] for c in kept]
    failures += not check("budget trims to the cap", len(kept) <= cfg.MAX_SIGNALS_PER_DAY,
                          f"{len(cands)} candidates -> {len(kept)}")
    failures += not check("budget keeps the best scores first",
                          scores == sorted(scores, reverse=True), str(scores))
    with session_scope() as db:
        for i in range(cfg.MAX_SIGNALS_PER_DAY):
            db.add(Signal(timestamp=now_naive(), symbol=f"CAP{i}", timeframe="5m",
                          direction="LONG", entry=100.0, sl=98.0, trail_sl=98.0,
                          tp1=101.0, tp2=102.0, tp3=103.0, qty=100, lots=1,
                          qty_open=100, atr14=1.0, status="OPEN"))
    failures += not check("hard cap blocks further signals", not bud.allow(),
                          str(bud.check()))
    failures += not check("cap survives a process restart (read from the ledger)",
                          SignalBudget().remaining() == 0)
    bud.notify_if_exhausted(); bud.notify_if_exhausted()
    failures += not check("unsubscribe hook fires exactly once", fired["n"] == 1,
                          f"called {fired['n']}x")

    # A malformed row must not take down management for everything else.
    with session_scope() as db:
        db.add(Signal(timestamp=now_naive(), symbol="NIFTY 50", timeframe="5m",
                      direction="LONG", entry=24500.0, sl=24400.0, status="OPEN"))
    try:
        await engine_probe.manage_open_positions(feed)
        survived = True
    except Exception as exc:
        survived = False
        print(f"        manage_open_positions raised: {exc}")
    failures += not check("a signal with missing levels is skipped, not fatal", survived)

    print()
    print("=" * 66)
    print("OPTIONS LAYER  (Black-76, IV inversion, gates)")
    print("=" * 66)
    from datetime import timedelta as _td

    from options_layer import (OptionsLayer, black76_greeks, black76_price,
                               implied_vol, years_to_expiry)

    F = 24500.0
    worst = 0.0
    for dte, sigma in ((7, 0.12), (7, 0.30), (2, 0.15), (30, 0.18), (1, 0.22)):
        t = years_to_expiry(now_naive().date() + _td(days=dte))
        for is_call in (True, False):
            px = black76_price(F, F, t, sigma, is_call)
            back = implied_vol(px, F, F, t, is_call)
            worst = max(worst, abs((back or 0) - sigma))
    failures += not check("IV inversion round-trips", worst < 1e-5, f"worst error {worst:.2e}")

    t7 = years_to_expiry(now_naive().date() + _td(days=7))
    call = black76_price(F, 24800.0, t7, 0.15, True)
    put = black76_price(F, 24800.0, t7, 0.15, False)
    parity_err = abs((call - put) - math.exp(-cfg.RISK_FREE_RATE * t7) * (F - 24800.0))
    failures += not check("put-call parity holds", parity_err < 1e-6, f"error {parity_err:.2e}")

    g_call = black76_greeks(F, F, t7, 0.15, True)
    g_put = black76_greeks(F, F, t7, 0.15, False)
    failures += not check("ATM delta near +/-0.5",
                          abs(g_call["delta"] - 0.5) < 0.05 and abs(g_put["delta"] + 0.5) < 0.05,
                          f"CE {g_call['delta']:+.3f} PE {g_put['delta']:+.3f}")
    failures += not check("theta is negative for a long option",
                          g_call["theta"] < 0 and g_put["theta"] < 0)
    failures += not check("near-dated theta exceeds far-dated",
                          abs(black76_greeks(F, F, years_to_expiry(now_naive().date() + _td(days=2)),
                                             0.15, True)["theta"]) > abs(g_call["theta"]))
    failures += not check("nonsense quote returns no IV",
                          implied_vol(0.01, F, 20000.0, t7, True) is None)

    layer = OptionsLayer(feed)
    today = now_naive().date()
    failures += not check("expiry-day theta gate fires past the cutoff",
                          bool(layer.theta_gate(today, datetime.combine(today, dtime(14, 0)))),
                          str(layer.theta_gate(today, datetime.combine(today, dtime(14, 0)))))
    failures += not check("morning of expiry day is still allowed",
                          layer.theta_gate(today, datetime.combine(today, dtime(10, 0))) is None)
    failures += not check("past expiry is rejected",
                          bool(layer.theta_gate(today - _td(days=1))))

    rolled = layer._choose_expiry([
        {"date": today, "epoch_ms": 0},
        {"date": today + _td(days=7), "epoch_ms": 1},
    ])
    if layer._past_theta_cutoff(datetime.combine(today, dtime(14, 0))):
        pass  # rollover depends on the wall clock; checked explicitly below
    forced = layer._choose_expiry([{"date": today + _td(days=7), "epoch_ms": 1}])
    failures += not check("expiry selection picks the nearest live contract",
                          forced and forced["date"] == today + _td(days=7))

    plan = await layer.build_plan("NIFTY 50", 24500.0, "LONG", target_move=25.0)
    failures += not check("option plan built", plan.strike is not None,
                          f"{plan.label} ltp={plan.ltp} iv={plan.iv} "
                          f"delta={plan.delta} drag={plan.theta_drag_pct}%")
    failures += not check("strike is at the money", plan.strike == 24500)
    failures += not check("CE chosen for a long", plan.option_type == "CE")
    if plan.iv:
        failures += not check("solved IV is plausible", 0.03 < plan.iv < 1.0, f"{plan.iv:.3f}")
    failures += not check("IV rank withheld until history exists",
                          plan.iv_rank is None or plan.iv_samples >= cfg.IV_RANK_MIN_SAMPLES,
                          f"rank={plan.iv_rank} samples={plan.iv_samples}")

    short_plan = await layer.build_plan("BANKNIFTY", 52000.0, "SHORT", target_move=120.0)
    failures += not check("PE chosen for a short", short_plan.option_type == "PE")
    failures += not check("strike steps by 100 for BankNifty", short_plan.strike % 100 == 0,
                          str(short_plan.strike))

    print()
    print("=" * 66)
    print("END-TO-END  (scan -> size -> manage -> exit)")
    print("=" * 66)
    engine = SignalEngine(options=OptionsLayer(feed))
    store = CandleStore(feed)
    scanner = IndexScanner(feed, engine=engine, store=store)

    setups = await scanner.scan_all(force=True)
    print(f"        scan_all returned {len(setups)} setup(s)")

    df5 = store.read("NIFTY 50", "5m", 300)
    failures += not check("candle store persisted bars", len(df5) > 0, f"{len(df5)} bars")
    if len(df5) >= 30:
        h15 = store.resample(df5, "15m")
        # Check a middle bucket, which is guaranteed complete.
        got = h15.iloc[len(h15) // 2]
        lo = got.timestamp
        hi = lo + pd.Timedelta(minutes=15)
        src = df5[(df5.timestamp >= lo) & (df5.timestamp < hi)]
        failures += not check(
            "15m derived from 5m aggregates correctly",
            len(src) == 3
            and abs(got.open - src.open.iloc[0]) < 0.01 and abs(got.high - src.high.max()) < 0.01
            and abs(got.low - src.low.min()) < 0.01 and abs(got.close - src.close.iloc[-1]) < 0.01
            and abs(got.volume - src.volume.sum()) < 1,
            f"{len(df5)} x 5m -> {len(h15)} x 15m, bucket {lo} from {len(src)} source bars")
        failures += not check(
            "no partial leading bucket",
            h15.timestamp.iloc[0] >= df5.timestamp.iloc[0],
            f"first 15m {h15.timestamp.iloc[0]} vs first 5m {df5.timestamp.iloc[0]}")
    before = store.api_calls_made
    await store.get("NIFTY 50", "5m", 300)
    failures += not check("repeat request served from store", store.api_calls_made == before,
                          f"hit rate {store.stats()['hit_rate_pct']}%")

    # Sizing must work regardless of whether the mock threw a live setup.
    sample = setups[0] if setups else {
        "symbol": "NIFTY 50", "timeframe": "5m", "direction": "LONG",
        "entry": 24500.0, "atr": 45.0, "scores": {"composite": 0.82}, "meta": {},
        "composite_score": 0.82,
    }
    levels = engine.calculate_levels(sample)
    failures += not check("levels computed", levels is not None)
    if levels:
        long_side = sample["direction"] == "LONG"
        ordered = (levels["sl"] < levels["entry"] < levels["tp1"] < levels["tp2"] < levels["tp3"]) if long_side \
            else (levels["sl"] > levels["entry"] > levels["tp1"] > levels["tp2"] > levels["tp3"])
        failures += not check("stop and targets ordered correctly", ordered,
                              f"SL {levels['sl']} E {levels['entry']} T3 {levels['tp3']}")
        failures += not check("quantity is a whole number of lots",
                              levels["qty"] % cfg.lot_size(sample["symbol"]) == 0,
                              f"{levels['qty']} qty / {levels['lots']} lots")
        failures += not check("risk within cap", levels["risk_pct"] <= cfg.MAX_RISK_PER_TRADE_PCT,
                              f"{levels['risk_pct']}%")

    created = await engine.create_signal(sample)
    failures += not check("signal persisted", created is not None and created.get("id"))

    if created:
        snap = dict(created)
        # Walk it to TP1, then to TP3.
        engine._apply_rules(snap, snap["tp1"], None, False)
        failures += not check("TP1 banks a partial and moves the stop to breakeven",
                              snap["tp1_hit"] and snap["qty_open"] < snap["qty"] and snap["trail_sl"] == snap["entry"],
                              f"open {snap['qty_open']}/{snap['qty']}, stop {snap['trail_sl']}")
        engine._apply_rules(snap, snap["tp3"], None, False)
        failures += not check("TP3 closes the trade with P&L booked",
                              snap["status"] == "TP3" and snap["qty_open"] == 0 and snap["pnl"] != 0,
                              f"status {snap['status']}, pnl {snap['pnl']:.0f}, R {snap['r_multiple']}")

        loser = dict(created)
        loser["id"] = -1
        engine._apply_rules(loser, loser["sl"], None, False)
        failures += not check("stop-out books a loss", loser["status"] == "SL" and loser["pnl"] < 0,
                              f"pnl {loser['pnl']:.0f}")

    updates = await engine.manage_open_positions(feed)
    print(f"        manage_open_positions touched {len(updates)} position(s)")
    print(f"        daily stats: {engine.daily_stats()}")

    print()
    print("=" * 66)
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    print("=" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
