"""Why did the scanner not pick a stock? Prints a compact timeline from scan_logs.

    python scan_report.py 2026-11-17 "DELHIVERY,CAMS,BAJAJFINSV,TMCV"

For each symbol: was it in that day's universe (rank), then for each timeframe
the scan rows where the outcome or reason changed (time, squeeze, close, ADX,
RSI, volume x, score, reason). Symbol names are matched by prefix too, so
"TMCV" finds "TMCV" and "TATAMOTORS" style variants are worth passing.
"""
import json
import sys
from datetime import datetime, timedelta

from sqlalchemy import text

from database import engine


def main(argv):
    day = datetime.strptime(argv[0], "%Y-%m-%d")
    wanted = [s.strip().upper() for s in argv[1].split(",") if s.strip()]
    nxt = day + timedelta(days=1)
    with engine.connect() as c:
        uni = c.execute(text("SELECT symbol, rank, turnover_cr FROM universe_members "
                             "WHERE selected_on >= :a AND selected_on < :b"),
                        {"a": day, "b": nxt}).fetchall()
        uni_map = {u[0].upper(): (u[1], u[2]) for u in uni}
        print(f"SCANREPORT universe {day:%Y-%m-%d}: {len(uni_map)} stocks", flush=True)
        for w in wanted:
            names = sorted({s for s in uni_map if s.startswith(w[:4])}) or [w]
            like = c.execute(text("SELECT DISTINCT symbol FROM scan_logs WHERE timestamp >= :a "
                                  "AND timestamp < :b AND upper(symbol) LIKE :p"),
                             {"a": day, "b": nxt, "p": w[:4] + "%"}).fetchall()
            print(f"SCANREPORT {w}: universe matches {[(n, uni_map.get(n)) for n in names]} | "
                  f"scan_log symbols {[r[0] for r in like]}", flush=True)
            for (sym,) in like:
                rows = c.execute(text(
                    "SELECT timestamp, timeframe, in_squeeze, squeeze_bars, bars_since_fire, close, "
                    "adx_value, rsi_value, vol_ratio, composite_score, passed, rejection_reason "
                    "FROM scan_logs WHERE symbol = :s AND timestamp >= :a AND timestamp < :b "
                    "ORDER BY timeframe, timestamp"), {"s": sym, "a": day, "b": nxt}).fetchall()
                last = {}
                for r in rows:
                    tf = r[1]
                    key = (r[10], (r[11] or "")[:60])
                    if last.get(tf) == key:
                        continue
                    last[tf] = key
                    print("SCANROW " + json.dumps({
                        "sym": sym, "tf": tf, "t": r[0].strftime("%H:%M"), "sqz": r[2],
                        "sqz_bars": r[3], "since_fire": r[4], "close": r[5],
                        "adx": None if r[6] is None else round(r[6], 1),
                        "rsi": None if r[7] is None else round(r[7], 1),
                        "vol": None if r[8] is None else round(r[8], 2),
                        "score": None if r[9] is None else round(r[9], 2),
                        "passed": r[10], "reason": r[11]}, default=str), flush=True)
    print("SCANREPORT DONE", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
