"""Deep dive: only the time-window where shadow_alerts/scoring was active.

- Effective period: from MIN(ts) of shadow_alerts to MAX(ts) of candles_5m.
- For every (symbol, ts) in that window, compute the FUTURE 4h max gain.
- Bucket gains into thresholds (>=5%, >=10%, >=20%) and compare with the
  highest shadow score we logged for the same symbol in [-60min, +5min].
- This tells us: how much of each "future big move" did we even SEE in shadow?
- Then per-component analysis (volume_z, range_expansion, breakout_score,
  momentum_1h) on shadow_alerts -> outcome = good/bad.
- Finally: 'leading-feature' check - what features/values did we have at
  T-30min for events that were big at T+4h?
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
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

    cur.execute("SELECT MIN(ts) AS mn, MAX(ts) AS mx FROM shadow_alerts")
    sh = cur.fetchone()
    cur.execute("SELECT MAX(ts) AS mx FROM candles_5m")
    cn = cur.fetchone()
    start_ts = int(sh["mn"])
    end_ts = int(cn["mx"]) - 4 * 3600 * 1000  # need 4h forward
    print(f"Active window for shadow_alerts: {fmt_ts(start_ts)} -> {fmt_ts(end_ts)}")
    print(f"Hours of evaluable data: {(end_ts-start_ts)/3600000:.1f}")

    # Build mapping (symbol, 5m_bucket) -> max shadow score in that bucket
    cur.execute(
        "SELECT symbol, ts, score, suppression_reason, features_json "
        "FROM shadow_alerts WHERE ts BETWEEN ? AND ?",
        (start_ts, end_ts + 4 * 3600 * 1000),
    )
    sh_rows = cur.fetchall()
    print(f"shadow rows in window: {len(sh_rows)}")

    sh_by_sym: dict[str, list[tuple[int, float, str | None, str]]] = defaultdict(list)
    for r in sh_rows:
        sh_by_sym[r["symbol"]].append(
            (int(r["ts"]), float(r["score"]), r["suppression_reason"], r["features_json"])
        )
    for k in sh_by_sym:
        sh_by_sym[k].sort(key=lambda x: x[0])

    # Iterate (symbol, ts) per 5m and compute forward 4h gain
    cur.execute(
        "SELECT symbol, ts, close FROM candles_5m WHERE ts BETWEEN ? AND ? ORDER BY symbol, ts",
        (start_ts, end_ts),
    )
    base_rows = cur.fetchall()
    print(f"base 5m rows in window: {len(base_rows):,}")

    # Forward 4h max high lookup per symbol
    cur.execute(
        "SELECT symbol, ts, high, close FROM candles_5m WHERE ts BETWEEN ? AND ? ORDER BY symbol, ts",
        (start_ts, end_ts + 4 * 3600 * 1000),
    )
    all_rows = cur.fetchall()
    by_sym_ts: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for r in all_rows:
        by_sym_ts[r["symbol"]].append((int(r["ts"]), float(r["high"]), float(r["close"])))
    for k in by_sym_ts:
        by_sym_ts[k].sort(key=lambda x: x[0])

    # Compute big moves: forward 4h max high vs current close.
    big_moves: list[tuple[str, int, float, float]] = []  # symbol, ts, gain, close
    for r in base_rows:
        sym = r["symbol"]
        ts = int(r["ts"])
        close_now = float(r["close"])
        if close_now <= 0:
            continue
        seq = by_sym_ts.get(sym)
        if not seq:
            continue
        end_window_ts = ts + 4 * 3600 * 1000
        max_high = 0.0
        for t, h, c in seq:
            if t <= ts:
                continue
            if t > end_window_ts:
                break
            if h > max_high:
                max_high = h
        if max_high <= 0:
            continue
        gain = max_high / close_now - 1.0
        if gain >= 0.05:
            big_moves.append((sym, ts, gain, close_now))

    print(f"big moves (>=+5% in next 4h, active window): {len(big_moves)}")
    # Histogram of gains
    bins = [0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50, 1.00]
    counts = [0] * (len(bins) + 1)
    for _, _, g, _ in big_moves:
        for i, b in enumerate(bins):
            if g < b:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    print("Gain bucket counts:")
    edges = ["<5", "5-8", "8-10", "10-15", "15-20", "20-30", "30-50", "50-100", ">=100%"]
    for label, c in zip(edges, counts):
        print(f"  {label:>10s}: {c}")

    # For each big move, find the highest shadow score in [-60min, +0min]
    # and the score AT t (or earlier in [-15min,+0]); also find max_score in [-60,+5min]
    print("\nFor each big move, lookup shadow score window:")
    LB_60 = 60 * 60 * 1000
    LB_15 = 15 * 60 * 1000

    seen_movers = set()
    rows_out = []
    for sym, ts, g, _ in big_moves:
        # collapse: only count once per 4h window per symbol
        key = (sym, ts // (4 * 3600 * 1000))
        if key in seen_movers:
            continue
        seen_movers.add(key)
        sh_list = sh_by_sym.get(sym, [])
        before_max = 0.0
        before_max_15 = 0.0
        before_reason = None
        for t, sc, reason, _ in sh_list:
            if ts - LB_60 <= t <= ts + 5 * 60 * 1000:
                if sc > before_max:
                    before_max = sc
                    before_reason = reason
            if ts - LB_15 <= t <= ts + 5 * 60 * 1000:
                if sc > before_max_15:
                    before_max_15 = sc
        rows_out.append((sym, ts, g, before_max, before_max_15, before_reason))

    print(f"unique big moves (collapsed 4h): {len(rows_out)}")

    # Buckets of "max shadow score before" -> hit rate of big move
    bands = [(0, "no shadow"), (1, "<45 (no shadow)"), (45, "45-55"), (55, "55-65"), (65, "65-75"), (75, ">=75")]
    counts_by_band: dict[str, int] = defaultdict(int)
    big_by_band: dict[str, int] = defaultdict(int)
    big10_by_band: dict[str, int] = defaultdict(int)

    def band_label(mx: float) -> str:
        if mx <= 0:
            return "no shadow row"
        if mx < 45:
            return "<45"
        if mx < 55:
            return "45-55"
        if mx < 65:
            return "55-65"
        if mx < 75:
            return "65-75"
        return ">=75"

    counts_by_band_total: dict[str, int] = defaultdict(int)
    big_by_band_count: dict[str, int] = defaultdict(int)
    big_by_band_count10: dict[str, int] = defaultdict(int)

    for sym, ts, g, mx, mx15, reason in rows_out:
        lbl = band_label(mx)
        counts_by_band_total[lbl] += 1
        big_by_band_count[lbl] += 1 if g >= 0.05 else 0
        big_by_band_count10[lbl] += 1 if g >= 0.10 else 0

    # Now also count negatives - all moves that were NOT big in the same window
    # Build set of all (symbol, 4h-bucket) keys
    all_5m_keys = set()
    for r in base_rows:
        all_5m_keys.add((r["symbol"], int(r["ts"]) // (4 * 3600 * 1000)))

    # For each shadow_alert, classify by score band and check if it preceded a big move
    sa_by_band_total: dict[str, int] = defaultdict(int)
    sa_by_band_hit5: dict[str, int] = defaultdict(int)
    sa_by_band_hit10: dict[str, int] = defaultdict(int)

    cur.execute(
        """
        SELECT s.symbol, s.ts, s.score, o.max_gain_4h, o.max_gain_24h
        FROM shadow_alerts s LEFT JOIN outcomes_shadow o ON o.shadow_alert_id = s.id
        WHERE s.ts BETWEEN ? AND ?
        """,
        (start_ts, end_ts),
    )
    for r in cur.fetchall():
        sc = float(r["score"])
        lbl = band_label(sc)
        sa_by_band_total[lbl] += 1
        g4 = r["max_gain_4h"]
        g24 = r["max_gain_24h"]
        if g4 is not None and g4 >= 0.05:
            sa_by_band_hit5[lbl] += 1
        if g24 is not None and g24 >= 0.10:
            sa_by_band_hit10[lbl] += 1

    print("\nShadow-alert score band -> hit rate (the right way):")
    print(f"  {'band':<14} {'n':>6} {'hit>=5/4h':>11} {'hit>=10/24h':>13}")
    for band in ["<45", "45-55", "55-65", "65-75", ">=75"]:
        n = sa_by_band_total.get(band, 0)
        if n == 0:
            continue
        h5 = sa_by_band_hit5.get(band, 0)
        h10 = sa_by_band_hit10.get(band, 0)
        print(f"  {band:<14} {n:>6} {h5/n*100:>9.1f}%  {h10/n*100:>11.1f}%")

    # Key question: for the big moves, were they accompanied/preceded by score bumps?
    print("\n--- Were big movers detected? ---")
    print(f"  {'band':<18} {'#movers':>9}")
    for band, cnt in sorted(counts_by_band_total.items(), key=lambda x: x[0]):
        print(f"  {band:<18} {cnt:>9}")

    # Now show top 30 big movers AND their max shadow score in [-60min, 0]
    rows_out.sort(key=lambda x: -x[2])
    print("\nTop 30 big movers (collapsed 4h windows) with shadow context:")
    print(f"  {'time':<17} {'symbol':<14} {'gain%':>8} {'maxsh':>6} {'<=15':>6} {'reason'}")
    for sym, ts, g, mx, mx15, reason in rows_out[:30]:
        print(f"  {fmt_ts(ts):<17} {sym:<14} {g*100:>7.2f}  {mx:>5.1f}  {mx15:>5.1f}  {reason or '-'}")

    # ---- Per-component analysis on shadow alerts (weighted impact) ----
    print("\n--- Per-component effectiveness (parsed from shadow features_json) ---")
    rows = sh_rows
    # parse and collect (component_value_normalized, hit5_4h)
    comp_hit: dict[str, list[tuple[float, int]]] = defaultdict(list)
    cur.execute(
        """
        SELECT s.id, s.symbol, s.ts, s.score, s.features_json,
               o.max_gain_4h, o.max_dd_4h
        FROM shadow_alerts s LEFT JOIN outcomes_shadow o ON o.shadow_alert_id = s.id
        WHERE s.ts BETWEEN ? AND ?
        """,
        (start_ts, end_ts),
    )
    full = cur.fetchall()
    weighted_components: dict[str, list[tuple[float, float]]] = defaultdict(list)  # (component_value, gain4h)
    for r in full:
        feats = json.loads(r["features_json"] or "{}")
        comps = feats.get("components", {})
        g4 = r["max_gain_4h"]
        if g4 is None:
            continue
        for k, v in comps.items():
            weighted_components[k].append((float(v), float(g4)))

    print(f"  {'component':<18} {'n':>6} {'corr(value,g4)':>15}  {'mean v':>8}  {'mean g4%':>10}")
    for k, lst in weighted_components.items():
        n = len(lst)
        if n < 30:
            continue
        xs = [p[0] for p in lst]
        ys = [p[1] for p in lst]
        # Pearson correlation
        mx_, my_ = statistics.mean(xs), statistics.mean(ys)
        sx = sum((x - mx_) ** 2 for x in xs)
        sy = sum((y - my_) ** 2 for y in ys)
        sxy = sum((x - mx_) * (y - my_) for x, y in zip(xs, ys))
        corr = sxy / ((sx * sy) ** 0.5) if sx > 0 and sy > 0 else 0.0
        print(f"  {k:<18} {n:>6}  {corr:>13.3f}   {mx_:>7.2f}   {my_*100:>9.2f}")

    # ---- Conditional: high volume_z & high range_expansion only ----
    print("\n--- Combined component thresholds and outcomes ---")
    rules = [
        ("volume_z>=22 (z>=4.4)", lambda c: c.get("volume_z", 0) >= 22),
        ("range_expansion>=12 (re>=2.4)", lambda c: c.get("range_expansion", 0) >= 12),
        ("breakout_score>=12 (bo>=0.8)", lambda c: c.get("breakout_score", 0) >= 12),
        ("momentum_1h>=10 (mom>=6.7%)", lambda c: c.get("momentum_1h", 0) >= 10),
        ("vz>=22 AND mom>=8", lambda c: c.get("volume_z", 0) >= 22 and c.get("momentum_1h", 0) >= 8),
        ("vz>=15 AND re>=10 AND bo>=10", lambda c: c.get("volume_z", 0) >= 15 and c.get("range_expansion", 0) >= 10 and c.get("breakout_score", 0) >= 10),
        ("vz>=15 AND mom>=8", lambda c: c.get("volume_z", 0) >= 15 and c.get("momentum_1h", 0) >= 8),
    ]

    print(f"  {'rule':<40} {'n':>6} {'hit5/4h':>10} {'hit10/24h':>11} {'mean g4%':>9}")
    for name, rule in rules:
        n = 0
        hit5 = 0
        hit10 = 0
        gains = []
        for r in full:
            feats = json.loads(r["features_json"] or "{}")
            comps = feats.get("components", {})
            if not rule(comps):
                continue
            g4 = r["max_gain_4h"]
            g24 = r["max_gain_24h"] if "max_gain_24h" in r.keys() else None
            if g4 is None:
                continue
            n += 1
            if g4 >= 0.05:
                hit5 += 1
            if g24 is not None and g24 >= 0.10:
                hit10 += 1
            gains.append(g4)
        if n == 0:
            continue
        print(
            f"  {name:<40} {n:>6}  {hit5/n*100:>7.1f}%  {hit10/n*100:>9.1f}%  "
            f"{statistics.mean(gains)*100:>7.2f}"
        )


if __name__ == "__main__":
    main()
