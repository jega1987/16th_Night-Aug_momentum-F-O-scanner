"""
Does a learned filter add anything? A meta-labeling study on backtest trades.

    python ml_study.py --label mlpool-uncapped

The scanner proposes trades; a model estimates the probability that each one
ends positive, from what was known at entry (check values, market context,
VIX regime, time of day). Validation is walk-forward by month: the model for
month M is trained only on months before M, so every reported number is
out-of-sample. Plain logistic regression (numpy, L2) - with a few hundred
trades anything more flexible would mostly memorise noise.

Reported: out-of-sample AUC (0.5 = coin flip), and the average R of trades the
model would have kept (top half / top third by probability) against all
trades in the same months. Nothing here touches the live scanner.
"""
import argparse
import json
import logging
import sys
from typing import Dict, List

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)-7s ml  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("ml")

FEATURES = ["composite", "adx", "rsi_dir", "vol_ratio", "squeeze_bars", "in_squeeze",
            "squeeze_width_pct", "multi_day", "structure", "sweep", "htf", "hour", "risk_pct",
            "is_long", "is_index", "is_box", "mkt_trend_al", "mkt_day_al", "mkt_adx", "vix_pct"]


def load(label: str) -> pd.DataFrame:
    from sqlalchemy import text
    from database import engine
    with engine.connect() as c:
        df = pd.read_sql(text("SELECT * FROM backtest_trades WHERE label = :l"), c, params={"l": label})
    return df


def features(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    d = np.where(df["direction"] == "LONG", 1.0, -1.0)
    num = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df else pd.Series(np.nan, index=df.index)
    out["composite"] = num("composite")
    out["adx"] = num("adx")
    out["rsi_dir"] = np.where(d > 0, num("rsi"), 100 - num("rsi"))
    out["vol_ratio"] = np.log1p(num("vol_ratio").clip(lower=0))
    out["squeeze_bars"] = np.log1p(num("squeeze_bars").clip(lower=0))
    out["in_squeeze"] = num("in_squeeze").astype(float) if "in_squeeze" in df else np.nan
    out["squeeze_width_pct"] = num("squeeze_width_pct")
    out["multi_day"] = df["multi_day"].astype(float) if "multi_day" in df else np.nan
    for c in ("structure", "sweep", "htf", "hour", "risk_pct", "mkt_adx", "vix_pct"):
        out[c] = num(c)
    out["is_long"] = (d > 0).astype(float)
    out["is_index"] = (df["asset"] == "INDEX").astype(float) if "asset" in df else 0.0
    out["is_box"] = (df["setup"] == "box").astype(float) if "setup" in df else 0.0
    out["mkt_trend_al"] = num("mkt_trend") * d
    out["mkt_day_al"] = num("mkt_day") * d
    return out[FEATURES]


def fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 3000, lr: float = 0.1):
    w = np.zeros(X.shape[1]); b = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ w + b)))
        g = p - y
        w -= lr * (X.T @ g / len(y) + l2 * w / len(y))
        b -= lr * g.mean()
    return w, b


def auc(score: np.ndarray, y: np.ndarray) -> float:
    pos, neg = score[y == 1], score[y == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    ranks = pd.Series(np.concatenate([pos, neg])).rank().values
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def summary(rs) -> Dict:
    a = np.asarray(rs, dtype=float)
    if not len(a):
        return {"n": 0}
    w, l = a[a > 0], a[a <= 0]
    return {"n": int(len(a)), "win%": round(len(w) / len(a) * 100, 1), "avgR": round(float(a.mean()), 3),
            "totalR": round(float(a.sum()), 1),
            "PF": round(float(w.sum() / -l.sum()), 2) if l.sum() < 0 else None}


def study(df: pd.DataFrame, min_train_months: int = 2) -> Dict:
    df = df.sort_values("entry_time").reset_index(drop=True)
    df["month"] = df["entry_time"].astype(str).str[:7]
    X_all = features(df)
    keep = [c for c in FEATURES if X_all[c].notna().mean() > 0.8 and X_all[c].std(skipna=True) > 0]
    X_all = X_all[keep]
    y_all = (pd.to_numeric(df["r"]) > 0).astype(float).values
    r_all = pd.to_numeric(df["r"]).values
    months = sorted(df["month"].unique())
    probs = np.full(len(df), np.nan)
    for i in range(min_train_months, len(months)):
        tr = df["month"].isin(months[:i]).values
        te = (df["month"] == months[i]).values
        Xtr, Xte = X_all[tr], X_all[te]
        med = Xtr.median()
        Xtr, Xte = Xtr.fillna(med), Xte.fillna(med)
        mu, sd = Xtr.mean(), Xtr.std().replace(0, 1)
        w, b = fit(((Xtr - mu) / sd).values, y_all[tr])
        probs[te] = 1 / (1 + np.exp(-(((Xte - mu) / sd).values @ w + b)))
    oos = ~np.isnan(probs)
    res = {"trades": int(len(df)), "oos_trades": int(oos.sum()), "features": keep,
           "oos_auc": round(auc(probs[oos], y_all[oos]), 3) if oos.any() else None,
           "all_oos_months": summary(r_all[oos])}
    for name, q in (("keep_top_half", 0.5), ("keep_top_third", 2 / 3)):
        kept = []
        for m in months[min_train_months:]:
            idx = (df["month"] == m).values & oos
            if idx.sum() == 0:
                continue
            cut = np.quantile(probs[idx], q)
            kept += list(r_all[idx & (probs >= cut)])
        res[name] = summary(kept)
    # Full-sample coefficients, for reading only (in-sample)
    X = X_all.fillna(X_all.median())
    mu, sd = X.mean(), X.std().replace(0, 1)
    w, _ = fit(((X - mu) / sd).values, y_all)
    res["coefficients_in_sample"] = {k: round(float(v), 3) for k, v in
                                     sorted(zip(keep, w), key=lambda kv: -abs(kv[1]))}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--min-train-months", type=int, default=2)
    args = ap.parse_args(argv)
    try:
        from backfill_history import start_health_server
        start_health_server()
    except Exception:
        pass
    df = load(args.label)
    if len(df) < 60:
        logger.warning("Only %d trades under label %s - too few to learn from", len(df), args.label)
    res = study(df, args.min_train_months)
    logger.info("MLRESULT %s", json.dumps(res, default=str))
    logger.info("ML DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
