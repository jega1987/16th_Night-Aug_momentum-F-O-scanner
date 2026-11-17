"""Print the latest saved backtest trades for a label as JSON lines (read from Railway logs).

    python export_trades.py "gap:1h-wait1" [--compare "gap:1h-reject"]
"""
import json
import sys

import pandas as pd
from sqlalchemy import text

from database import engine


def latest(label: str) -> pd.DataFrame:
    with engine.connect() as c:
        rid = c.execute(text("SELECT run_id FROM backtest_trades WHERE label = :l "
                             "ORDER BY created_at DESC LIMIT 1"), {"l": label}).scalar()
        if rid is None:
            return pd.DataFrame()
        return pd.read_sql(text("SELECT * FROM backtest_trades WHERE label = :l AND run_id = :r "
                                "ORDER BY entry_time"), c, params={"l": label, "r": rid})


def main(argv):
    label = argv[0]
    cmp_label = argv[argv.index("--compare") + 1] if "--compare" in argv else None
    df = latest(label)
    if cmp_label:
        ref = latest(cmp_label)
        keys = set(zip(ref["symbol"], ref["entry_time"])) if len(ref) else set()
        df["extra_vs_" + cmp_label.split(":")[-1]] = [(s, e) not in keys
                                                       for s, e in zip(df["symbol"], df["entry_time"])]
    print(f"EXPORT START {label} rows={len(df)}", flush=True)
    for rec in df.to_dict("records"):
        print("TRADEROW " + json.dumps(rec, default=str), flush=True)
    print(f"EXPORT DONE {label} rows={len(df)}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
