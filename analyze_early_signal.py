"""Look at the 8 sent alerts - reconstruct candle motion to see if
an EARLIER fire would have been possible.

For each sent alert we print a strip of 5m candles around the alert ts,
plus simple early indicators: 5m return, 15m return, vol ratio vs 24h median,
position within prior 1h range.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

DB = Path(__file__).parent / "kraken_radar_backup.db"


def fmt_hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M")


def fmt_full(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute(
        """
        SELECT a.symbol, a.ts, a.score, o.max_gain_4h
        FROM alerts a LEFT JOIN outcomes o ON o.alert_id = a.id
        ORDER BY a.ts ASC
        """
    )
    sent = cur.fetchall()
    for s in sent:
        sym = s["symbol"]
        ts = int(s["ts"])
        score = float(s["score"])
        g4 = (s["max_gain_4h"] or 0) * 100
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM candles_5m WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts ASC",
            (sym, ts - 90 * 60 * 1000, ts + 30 * 60 * 1000),
        )
        rows = cur.fetchall()
        if not rows:
            print(f"\n{sym} @ {fmt_full(ts)}: no candles around ts")
            continue
        cur.execute(
            "SELECT volume FROM candles_5m WHERE symbol=? AND ts BETWEEN ? AND ?",
            (sym, ts - 24 * 3600 * 1000, ts - 5 * 60 * 1000),
        )
        vols = [float(r["volume"]) for r in cur.fetchall() if r["volume"] is not None]
        med_v = median(vols) if vols else 0.0

        # Prior 1h close (for early momentum)
        cur.execute(
            "SELECT close FROM candles_5m WHERE symbol=? AND ts <= ? ORDER BY ts DESC LIMIT 13",
            (sym, ts),
        )
        last13 = [float(r["close"]) for r in cur.fetchall()]
        # All historical 5m closes, 24h, for an early-rising-bar reference
        print(
            f"\n=== {sym}  ts={fmt_full(ts)}  score={score:.1f}  4h_gain={g4:+.1f}%  med_vol_24h={med_v:.4f} ==="
        )
        print(
            f"  {'time':<6} {'close':>12} {'vol':>10} {'volR':>6} {'r_5m%':>7} {'r_15m%':>7} {'r_60m%':>7} {'pos1h':>6}"
        )
        rows_list = list(rows)
        for i, r in enumerate(rows_list):
            t = int(r["ts"])
            close = float(r["close"])
            vol = float(r["volume"])
            volR = vol / med_v if med_v > 0 else 0.0
            r5 = ((close / float(rows_list[i - 1]["close"]) - 1.0) * 100) if i >= 1 else 0.0
            r15 = ((close / float(rows_list[i - 3]["close"]) - 1.0) * 100) if i >= 3 else 0.0
            r60 = ((close / float(rows_list[i - 12]["close"]) - 1.0) * 100) if i >= 12 else 0.0
            past = rows_list[max(0, i - 12) : i]
            if past:
                rng_hi = max(float(x["high"]) for x in past)
                rng_lo = min(float(x["low"]) for x in past)
                pos = (close - rng_lo) / (rng_hi - rng_lo) if (rng_hi - rng_lo) > 0 else 0.0
            else:
                pos = 0.0
            mark = " <-ALERT" if t == ts else ""
            print(
                f"  {fmt_hm(t):<6} {close:>12.6f} {vol:>10.4f} {volR:>5.1f}x {r5:>6.2f}  {r15:>6.2f}  {r60:>6.2f}  {pos:>5.2f}{mark}"
            )


if __name__ == "__main__":
    main()
