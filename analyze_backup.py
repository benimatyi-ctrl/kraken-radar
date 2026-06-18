"""Analyze kraken_radar_backup.db: signal effectiveness audit.

Mit néz:
- Tables, méretek, időszak (candles_5m/1h/4h, alerts, shadow_alerts, outcomes_*).
- Sent alerts vs shadow candidates: score-eloszlás, hit-rate, mean gain/dd.
- Suppression-okok bontása.
- Top példák: nagy árfolyammozgás 4h alatt VAN, de NINCS rá alert (false negatives).
- Top példák: alert/sent VAN, de a 4h hozam < 0 (false positives).
- Ár-emelkedés és komponensek korrelációja a kimenetellel.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).parent / "kraken_radar_backup.db"


def fmt_ts(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    print("=" * 90)
    print("DB:", DB, f"({DB.stat().st_size / (1024*1024):.1f} MB)")
    print("=" * 90)

    # ---- Schema ----
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r["name"] for r in cur.fetchall()]
    print("\nTables:", tables)

    # ---- Counts & coverage ----
    print("\n--- Coverage ---")
    for t in ("candles_5m", "candles_1h", "candles_4h"):
        if t not in tables:
            continue
        cur.execute(f"SELECT COUNT(*) AS n, MIN(ts) AS mn, MAX(ts) AS mx, COUNT(DISTINCT symbol) AS nsym FROM {t}")
        r = cur.fetchone()
        print(
            f"{t}: rows={r['n']:,}  symbols={r['nsym']:,}  range={fmt_ts(r['mn'])} -> {fmt_ts(r['mx'])}"
        )

    for t in ("alerts", "shadow_alerts", "outcomes", "outcomes_shadow", "tokens"):
        if t not in tables:
            continue
        cur.execute(f"SELECT COUNT(*) AS n FROM {t}")
        n = cur.fetchone()["n"]
        print(f"{t}: rows={n:,}")

    # ---- Alerts overview ----
    print("\n--- Alerts (sent_to_telegram path) ---")
    cur.execute("SELECT COUNT(*) AS n, MIN(ts) AS mn, MAX(ts) AS mx, AVG(score) AS avg_s, MIN(score) AS min_s, MAX(score) AS max_s FROM alerts")
    r = cur.fetchone()
    if r["n"]:
        print(f"alerts: n={r['n']}  range={fmt_ts(r['mn'])} -> {fmt_ts(r['mx'])}  score avg={r['avg_s']:.2f} min={r['min_s']:.2f} max={r['max_s']:.2f}")
    else:
        print("alerts: 0")

    # Outcomes for sent alerts
    cur.execute(
        """
        SELECT COUNT(*) AS n,
               AVG(o.max_gain_1h) AS g1, AVG(o.max_dd_1h) AS d1,
               AVG(o.max_gain_4h) AS g4, AVG(o.max_dd_4h) AS d4,
               AVG(o.max_gain_24h) AS g24, AVG(o.max_dd_24h) AS d24,
               AVG(CASE WHEN o.max_gain_1h >= 0.02 THEN 1.0 ELSE 0.0 END) AS hit2_1h,
               AVG(CASE WHEN o.max_gain_4h >= 0.05 THEN 1.0 ELSE 0.0 END) AS hit5_4h,
               AVG(CASE WHEN o.max_gain_24h >= 0.10 THEN 1.0 ELSE 0.0 END) AS hit10_24h
        FROM alerts a JOIN outcomes o ON o.alert_id = a.id
        """
    )
    r = cur.fetchone()
    if r and r["n"]:
        print(
            f"sent outcomes: n={r['n']}  "
            f"gain1h={r['g1']*100:.2f}% dd1h={r['d1']*100:.2f}%  "
            f"gain4h={r['g4']*100:.2f}% dd4h={r['d4']*100:.2f}%  "
            f"gain24h={(r['g24'] or 0)*100:.2f}% dd24h={(r['d24'] or 0)*100:.2f}%"
        )
        print(
            f"  hit-rates: >=+2%/1h: {r['hit2_1h']*100:.1f}%  >=+5%/4h: {r['hit5_4h']*100:.1f}%  >=+10%/24h: {(r['hit10_24h'] or 0)*100:.1f}%"
        )

    # ---- Shadow alerts breakdown ----
    print("\n--- Shadow alerts (all candidates above shadow_threshold = 45) ---")
    cur.execute("SELECT COUNT(*) AS n, MIN(ts) AS mn, MAX(ts) AS mx FROM shadow_alerts")
    r = cur.fetchone()
    print(f"shadow: n={r['n']}  range={fmt_ts(r['mn'])} -> {fmt_ts(r['mx'])}")
    cur.execute("SELECT suppression_reason, COUNT(*) AS c FROM shadow_alerts GROUP BY suppression_reason ORDER BY c DESC")
    for row in cur.fetchall():
        print(f"  {str(row['suppression_reason']):40s}  {row['c']}")

    # Score histogram
    print("\nScore histogram (shadow_alerts):")
    cur.execute(
        """
        SELECT CAST(score/5 AS INT)*5 AS bin, COUNT(*) AS c
        FROM shadow_alerts GROUP BY bin ORDER BY bin
        """
    )
    rows = cur.fetchall()
    if rows:
        mxc = max(r["c"] for r in rows)
        for r in rows:
            bar = "#" * int(40 * r["c"] / mxc)
            print(f"  {r['bin']:>3}-{r['bin']+5:<3}  {r['c']:>6}  {bar}")

    # Outcomes for shadow alerts (effectiveness if we had sent them)
    print("\n--- Shadow alert outcomes (what we WOULD have caught) ---")
    cur.execute(
        """
        SELECT COUNT(*) AS n,
               AVG(o.max_gain_4h) AS g4, AVG(o.max_dd_4h) AS d4,
               AVG(CASE WHEN o.max_gain_4h >= 0.05 THEN 1.0 ELSE 0.0 END) AS hit5_4h,
               AVG(CASE WHEN o.max_gain_24h >= 0.10 THEN 1.0 ELSE 0.0 END) AS hit10_24h
        FROM shadow_alerts s JOIN outcomes_shadow o ON o.shadow_alert_id = s.id
        """
    )
    r = cur.fetchone()
    if r and r["n"]:
        print(
            f"shadow outcomes: n={r['n']}  gain4h={(r['g4'] or 0)*100:.2f}% dd4h={(r['d4'] or 0)*100:.2f}%  "
            f"hit>=+5%/4h: {(r['hit5_4h'] or 0)*100:.1f}%  hit>=+10%/24h: {(r['hit10_24h'] or 0)*100:.1f}%"
        )

    # Score band -> hit rate
    print("\nScore band -> shadow outcomes (does higher score == better outcome?)")
    cur.execute(
        """
        SELECT CAST(s.score/5 AS INT)*5 AS bin, COUNT(*) AS n,
               AVG(o.max_gain_4h)  AS g4,
               AVG(o.max_dd_4h)    AS d4,
               AVG(CASE WHEN o.max_gain_4h >= 0.05 THEN 1.0 ELSE 0.0 END) AS hit5_4h,
               AVG(CASE WHEN o.max_gain_24h >= 0.10 THEN 1.0 ELSE 0.0 END) AS hit10_24h
        FROM shadow_alerts s JOIN outcomes_shadow o ON o.shadow_alert_id = s.id
        GROUP BY bin ORDER BY bin
        """
    )
    rows = cur.fetchall()
    print(f"  {'bin':<8} {'n':>6} {'gain4h%':>9} {'dd4h%':>9} {'hit5/4h':>9} {'hit10/24h':>10}")
    for r in rows:
        print(
            f"  {r['bin']:>3}-{r['bin']+5:<3} {r['n']:>6}  "
            f"{(r['g4'] or 0)*100:>7.2f}  {(r['d4'] or 0)*100:>7.2f}  "
            f"{(r['hit5_4h'] or 0)*100:>7.1f}%  {(r['hit10_24h'] or 0)*100:>8.1f}%"
        )

    # ---- Component effectiveness (parse features_json) ----
    print("\n--- Component-by-outcome (sent alerts) ---")
    cur.execute(
        """
        SELECT a.symbol, a.ts, a.score, a.features_json,
               o.max_gain_1h, o.max_dd_1h, o.max_gain_4h, o.max_dd_4h, o.max_gain_24h, o.max_dd_24h
        FROM alerts a LEFT JOIN outcomes o ON o.alert_id = a.id
        ORDER BY a.ts ASC
        """
    )
    sent_rows = cur.fetchall()
    print(f"sent alert rows: {len(sent_rows)}")

    # Show 30 examples
    print("\nExamples of sent alerts (chrono):")
    print(f"  {'time':<17} {'symbol':<14} {'score':>6} {'g4h%':>7} {'d4h%':>7} {'top reasons'}")
    for r in sent_rows[:80]:
        feats = json.loads(r["features_json"] or "{}")
        comps = feats.get("components", {})
        reasons = feats.get("reasons", [])
        top = ", ".join(reasons[:3])
        g4 = (r["max_gain_4h"] or 0) * 100
        d4 = (r["max_dd_4h"] or 0) * 100
        print(f"  {fmt_ts(r['ts']):<17} {r['symbol']:<14} {r['score']:>6.1f} {g4:>6.2f}  {d4:>6.2f}  {top}")

    # ---- False negatives: big movers in 5m/1h candles, but NO alert ----
    print("\n--- Looking for big moves with no alert (last 30 days of data) ---")
    # Get last ts per symbol and find 4h forward gains globally
    # Approach: For each (symbol, ts) in candles_5m, compute future 4h max(high)/close >= 8% as 'big mover'
    # Check if there is an alert within +/- 30min for that symbol.
    # This is heavy; restrict by time window: last 14 days of available data.
    cur.execute("SELECT MAX(ts) AS mx FROM candles_5m")
    mx_ts = cur.fetchone()["mx"] or 0
    window_ms = 14 * 24 * 60 * 60 * 1000
    start_ts = mx_ts - window_ms
    print(f"window: {fmt_ts(start_ts)} -> {fmt_ts(mx_ts)}")

    cur.execute(
        """
        WITH base AS (
          SELECT symbol, ts, close FROM candles_5m
          WHERE ts BETWEEN ? AND ?
        )
        SELECT b.symbol, b.ts, b.close,
               (SELECT MAX(c2.high) FROM candles_5m c2
                  WHERE c2.symbol=b.symbol AND c2.ts BETWEEN b.ts+300000 AND b.ts+4*3600*1000)
               AS fmax
        FROM base b
        """,
        (start_ts, mx_ts - 4 * 3600 * 1000),
    )
    rows = cur.fetchall()
    movers: list[tuple[str, int, float]] = []  # (symbol, ts, gain%)
    for r in rows:
        if r["fmax"] is None or r["close"] is None or r["close"] <= 0:
            continue
        gain = r["fmax"] / r["close"] - 1.0
        if gain >= 0.08:
            movers.append((r["symbol"], int(r["ts"]), gain))
    print(f"big movers (4h forward >=+8%): {len(movers)}")

    # For each mover, check if alert in [-15min, +30min]; show top 30 by gain that we MISSED
    cur.execute("SELECT symbol, ts FROM alerts")
    alert_set = set()
    for r in cur.fetchall():
        alert_set.add((r["symbol"], int(r["ts"]) // 300000))  # 5m bucket

    missed = []
    for sym, ts, g in movers:
        bucket = ts // 300000
        # Check +/- 6 buckets (~30m)
        if any((sym, bucket + d) in alert_set for d in range(-3, 7)):
            continue
        missed.append((sym, ts, g))
    missed.sort(key=lambda x: -x[2])

    print(f"missed (no alert nearby): {len(missed)}")
    print("\nTop 25 missed big moves:")
    print(f"  {'time':<17} {'symbol':<14} {'4h gain%':>9}")
    for sym, ts, g in missed[:25]:
        print(f"  {fmt_ts(ts):<17} {sym:<14} {g*100:>8.2f}")

    # Also: does the system see them in shadow_alerts (just below threshold)?
    # Sample 10 missed cases and check shadow scores around that time.
    print("\nShadow scores around missed events (sample 10):")
    sample = missed[:10]
    for sym, ts, g in sample:
        cur.execute(
            """
            SELECT ts, score, suppression_reason FROM shadow_alerts
            WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts
            """,
            (sym, ts - 60 * 60 * 1000, ts + 30 * 60 * 1000),
        )
        srows = cur.fetchall()
        sc_str = ", ".join(f"{fmt_ts(r['ts'])[-5:]}:{r['score']:.0f}" for r in srows[:8]) or "n/a"
        print(f"  {fmt_ts(ts):<17} {sym:<14} +{g*100:>5.2f}%  shadow scores in [-60m,+30m]: {sc_str}")


if __name__ == "__main__":
    main()
