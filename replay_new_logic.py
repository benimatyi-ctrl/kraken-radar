"""Fast replay of NEW scoring on backup DB.

Strategy: instead of scoring every (symbol, ts) pair (millions), score the
union of:
  1) every (symbol, ts) where shadow_alerts already existed (the OLD
     candidates) so we can compare apples-to-apples,
  2) every (symbol, 5m_ts) within +/- 60min of an OLD alert/shadow,
  3) a sample of "big mover starts" (symbols where forward 4h gain >=+8%).

For each scored point we run the NEW compute_signal + strict filters and
record the decision plus forward 4h/24h gain for outcome stats.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "kraken-radar" / "src"))

from kraken_radar.config import Settings
from kraken_radar.features.momentum import rsi_divergence
from kraken_radar.features.price import (
    breakout_score,
    micro_breakout_5m,
    momentum_15m,
    momentum_1h,
    position_in_1h_range,
    range_expansion,
)
from kraken_radar.features.regime import regime_gate  # noqa: F401
from kraken_radar.features.volume import (
    volume_acceleration,
    volume_zscore,
    volume_zscore_short,
)
from kraken_radar.signals.scoring import _apply_strict_filters, compute_signal

DB = Path(__file__).parent / "kraken_radar_backup.db"


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def safe_query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        cur = con.execute(sql, params)
        return cur.fetchall()
    except sqlite3.DatabaseError:
        return []


def main() -> None:
    config_path = Path(__file__).parent / "kraken-radar" / "config.yaml"
    cfg = Settings.from_sources(config_path)
    print(
        f"NEW config: alert_threshold={cfg.signals.alert_threshold} "
        f"shadow_margin={cfg.signals.shadow_score_margin} "
        f"strict.enabled={cfg.signals.strict.enabled}"
    )
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT MIN(ts) AS mn FROM shadow_alerts")
    start_ts = int(cur.fetchone()["mn"])
    cur.execute("SELECT MAX(ts) AS mx FROM candles_5m")
    end_ts = int(cur.fetchone()["mx"]) - 4 * 3600 * 1000
    print(f"Active window: {fmt_ts(start_ts)} -> {fmt_ts(end_ts)}")

    # Build the set of (symbol, ts) to score.
    eval_points: set[tuple[str, int]] = set()
    cur.execute(
        "SELECT symbol, ts FROM shadow_alerts WHERE ts BETWEEN ? AND ?",
        (start_ts, end_ts),
    )
    for r in cur.fetchall():
        eval_points.add((r["symbol"], int(r["ts"])))

    # Add a short rolling window around old alerts (look for earlier triggers)
    cur.execute(
        "SELECT symbol, ts FROM alerts WHERE ts BETWEEN ? AND ?",
        (start_ts, end_ts),
    )
    old_alerts = list(cur.fetchall())
    for r in old_alerts:
        ts = int(r["ts"])
        for delta_min in range(-90, 6, 5):
            eval_points.add((r["symbol"], ts + delta_min * 60 * 1000))

    # Sample big movers - pick (symbol, ts) where forward-4h max-high vs current close >= +8%
    print("Scanning for big movers in the active window...")
    big_movers = safe_query(
        con,
        """
        WITH base AS (
          SELECT symbol, ts, close FROM candles_5m WHERE ts BETWEEN ? AND ?
        )
        SELECT b.symbol, b.ts, b.close,
               (SELECT MAX(c2.high) FROM candles_5m c2
                  WHERE c2.symbol=b.symbol AND c2.ts BETWEEN b.ts+300000 AND b.ts+4*3600*1000)
               AS fmax
        FROM base b
        """,
        (start_ts, end_ts),
    )
    big_set: set[tuple[str, int]] = set()
    for r in big_movers:
        if r["fmax"] is None or r["close"] is None or r["close"] <= 0:
            continue
        if r["fmax"] / r["close"] - 1.0 >= 0.08:
            big_set.add((r["symbol"], int(r["ts"])))
    print(f"Big-mover candidate points: {len(big_set)}, shadow points: {len(eval_points)}")
    eval_points |= big_set
    print(f"Total points to score: {len(eval_points)}")

    # Group by symbol, then load ascending candle slices once per symbol.
    by_sym: dict[str, list[int]] = defaultdict(list)
    for sym, ts in eval_points:
        by_sym[sym].append(ts)
    for sym in by_sym:
        by_sym[sym].sort()

    new_alerts: list[dict] = []
    new_shadows = 0
    shadow_threshold = cfg.signals.alert_threshold - cfg.signals.shadow_score_margin

    n_sym = len(by_sym)
    print(f"Scoring across {n_sym} symbols...")
    last_progress = 0
    completed = 0
    for sym, tss in by_sym.items():
        completed += 1
        if completed - last_progress >= 50:
            last_progress = completed
            print(f"  ... {completed}/{n_sym} symbols processed, alerts so far: {len(new_alerts)}")

        # Load full 5m / 1h / 4h slices for this symbol once.
        try:
            df_5m = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM candles_5m "
                "WHERE symbol=? AND ts <= ? ORDER BY ts ASC",
                con,
                params=(sym, end_ts + 4 * 3600 * 1000),
            )
            df_1h = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM candles_1h "
                "WHERE symbol=? AND ts <= ? ORDER BY ts ASC",
                con,
                params=(sym, end_ts),
            )
            df_4h = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM candles_4h "
                "WHERE symbol=? AND ts <= ? ORDER BY ts ASC",
                con,
                params=(sym, end_ts),
            )
        except sqlite3.DatabaseError:
            continue

        if df_5m.empty:
            continue
        df_5m = df_5m.set_index("ts")
        if not df_1h.empty:
            df_1h = df_1h.set_index("ts")
        if not df_4h.empty:
            df_4h = df_4h.set_index("ts")

        last_alert_ts = 0
        for ts in tss:
            # Snap to nearest 5m candle <= ts
            sub_5m = df_5m.loc[:ts]
            if sub_5m.empty or len(sub_5m) < 30:
                continue
            real_ts = int(sub_5m.index[-1])
            if real_ts - last_alert_ts < 4 * 3600 * 1000 and last_alert_ts > 0:
                continue

            sub_1h = df_1h.loc[:real_ts] if not df_1h.empty else df_1h
            sub_4h = df_4h.loc[:real_ts] if not df_4h.empty else df_4h

            raw_volume_z = volume_zscore(sub_5m, lookback=288)
            raw_volume_z_short = volume_zscore_short(sub_5m, lookback=96)
            raw_volume_acc = volume_acceleration(sub_5m)
            raw_micro = micro_breakout_5m(sub_5m, lookback=12)
            raw_mom1h = momentum_1h(sub_5m)
            raw_mom15 = momentum_15m(sub_5m)
            raw_pos = position_in_1h_range(sub_5m, lookback=12)
            raw_re = range_expansion(sub_1h, lookback=24) if not sub_1h.empty else 0.0
            raw_bo = breakout_score(sub_1h, lookback=24) if not sub_1h.empty else 0.0
            raw_rsi = (
                rsi_divergence(sub_1h, sub_4h)
                if not sub_1h.empty and not sub_4h.empty
                else 0.0
            )

            features = {
                "symbol": sym,
                "ts": int(real_ts),
                "volume_z": raw_volume_z,
                "volume_z_short": raw_volume_z_short,
                "volume_acceleration": raw_volume_acc,
                "range_expansion": raw_re,
                "breakout_score": raw_bo,
                "micro_breakout_5m": raw_micro,
                "momentum_1h": raw_mom1h,
                "momentum_15m": raw_mom15,
                "pre_breakout_proximity": raw_pos,
                "rsi_divergence": raw_rsi,
                "social_velocity": 0.0,
            }
            regime = {"btc_1h_return": 0.0, "btc_volatility_24h": 0.0}
            result = compute_signal(features, regime, cfg)

            if result.score < shadow_threshold:
                continue
            new_shadows += 1

            result = _apply_strict_filters(
                result,
                frames={"5m": sub_5m, "1h": sub_1h, "4h": sub_4h},
                raw_volume_z=raw_volume_z,
                raw_volume_z_short=raw_volume_z_short,
                raw_volume_acceleration=raw_volume_acc,
                raw_momentum_60m_pct=raw_mom1h * 100.0,
                pos_1h=raw_pos,
                config=cfg,
            )

            if result.triggered:
                # Outcome
                cur_close = float(sub_5m.iloc[-1]["close"])
                fwd_4h = df_5m[(df_5m.index > real_ts) & (df_5m.index <= real_ts + 4 * 3600 * 1000)]
                fwd_24h = df_5m[(df_5m.index > real_ts) & (df_5m.index <= real_ts + 24 * 3600 * 1000)]
                g4 = float(fwd_4h["high"].max()) / cur_close - 1.0 if not fwd_4h.empty else None
                d4 = float(fwd_4h["low"].min()) / cur_close - 1.0 if not fwd_4h.empty else None
                g24 = float(fwd_24h["high"].max()) / cur_close - 1.0 if not fwd_24h.empty else None
                new_alerts.append(
                    {
                        "symbol": sym,
                        "ts": int(real_ts),
                        "score": result.score,
                        "g4h": g4,
                        "d4h": d4,
                        "g24h": g24,
                        "raw_micro": raw_micro,
                        "raw_vol_acc": raw_volume_acc,
                        "raw_pos_1h": raw_pos,
                        "raw_mom_15m_pct": raw_mom15 * 100,
                        "raw_volume_z_short": raw_volume_z_short,
                    }
                )
                last_alert_ts = real_ts

    print(f"\n=== NEW logic: {len(new_alerts)} triggered, {new_shadows} shadow candidates ===")

    # Dedupe alerts to one per symbol per 4h
    seen: set[tuple[str, int]] = set()
    deduped: list[dict] = []
    for a in sorted(new_alerts, key=lambda x: x["ts"]):
        key = (a["symbol"], a["ts"] // (4 * 3600 * 1000))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)

    print(f"After 4h-bucket dedup: {len(deduped)} triggered alerts")
    print(
        f"\n  {'time':<17} {'symbol':<14} {'score':>6} {'g4h%':>7} {'d4h%':>7} "
        f"{'micro':>5} {'vAcc':>5} {'pos1h':>6} {'m15%':>6} {'vzS':>5}"
    )
    n_eval = 0
    h5 = 0
    h10 = 0
    for a in deduped:
        if a["g4h"] is None:
            print(
                f"  {fmt_ts(a['ts']):<17} {a['symbol']:<14} {a['score']:>6.1f} {'n/a':>7} {'n/a':>7}  "
                f"{a['raw_micro']:>5.2f}  {a['raw_vol_acc']:>4.2f}  {a['raw_pos_1h']:>5.2f}  "
                f"{a['raw_mom_15m_pct']:>5.2f}  {a['raw_volume_z_short']:>5.2f}"
            )
            continue
        n_eval += 1
        if a["g4h"] >= 0.05:
            h5 += 1
        if a["g24h"] is not None and a["g24h"] >= 0.10:
            h10 += 1
        print(
            f"  {fmt_ts(a['ts']):<17} {a['symbol']:<14} {a['score']:>6.1f} "
            f"{a['g4h']*100:>6.2f}  {(a['d4h'] or 0)*100:>6.2f}  "
            f"{a['raw_micro']:>5.2f}  {a['raw_vol_acc']:>4.2f}  {a['raw_pos_1h']:>5.2f}  "
            f"{a['raw_mom_15m_pct']:>5.2f}  {a['raw_volume_z_short']:>5.2f}"
        )

    if n_eval > 0:
        print(
            f"\nNEW hit-rates: hit>=+5%/4h = {h5}/{n_eval} = {h5/n_eval*100:.1f}%, "
            f"hit>=+10%/24h = {h10}/{n_eval} = {h10/n_eval*100:.1f}%"
        )

    # OLD alerts comparison
    print(f"\n=== OLD logic actually sent: {len(old_alerts)} ===")
    cur.execute(
        "SELECT a.symbol, a.ts, a.score, o.max_gain_4h, o.max_dd_4h, o.max_gain_24h "
        "FROM alerts a LEFT JOIN outcomes o ON o.alert_id = a.id "
        "WHERE a.ts BETWEEN ? AND ? ORDER BY a.ts ASC",
        (start_ts, end_ts),
    )
    old_with_out = list(cur.fetchall())
    o5 = 0
    o10 = 0
    on = 0
    print(f"  {'time':<17} {'symbol':<14} {'score':>6} {'g4h%':>7} {'d4h%':>7}")
    for r in old_with_out:
        g4 = r["max_gain_4h"]
        d4 = r["max_dd_4h"]
        g24 = r["max_gain_24h"]
        if g4 is None:
            print(
                f"  {fmt_ts(int(r['ts'])):<17} {r['symbol']:<14} {r['score']:>6.1f} "
                f"{'n/a':>7} {'n/a':>7}"
            )
            continue
        on += 1
        if g4 >= 0.05:
            o5 += 1
        if g24 is not None and g24 >= 0.10:
            o10 += 1
        print(
            f"  {fmt_ts(int(r['ts'])):<17} {r['symbol']:<14} {r['score']:>6.1f} "
            f"{g4*100:>6.2f}  {(d4 or 0)*100:>6.2f}"
        )
    if on > 0:
        print(
            f"\nOLD hit-rates: hit>=+5%/4h = {o5}/{on} = {o5/on*100:.1f}%, "
            f"hit>=+10%/24h = {o10}/{on} = {o10/on*100:.1f}%"
        )

    # Earliness: for each OLD alert, did NEW fire on same symbol within prior 4h, earlier?
    print("\n=== Earliness: NEW vs OLD on the same symbol/event ===")
    new_by_sym: dict[str, list[int]] = defaultdict(list)
    for a in deduped:
        new_by_sym[a["symbol"]].append(a["ts"])
    for r in old_with_out:
        old_ts = int(r["ts"])
        sym = r["symbol"]
        candidates = [t for t in new_by_sym.get(sym, []) if old_ts - 4 * 3600 * 1000 <= t <= old_ts + 30 * 60 * 1000]
        if candidates:
            t0 = min(candidates)
            mins_diff = (old_ts - t0) // 60000
            label = f"{mins_diff} min earlier" if mins_diff > 0 else (f"{-mins_diff} min later" if mins_diff < 0 else "same time")
            print(f"  {sym:<14}  old={fmt_ts(old_ts)}  new earliest={fmt_ts(t0)}  -> {label}")
        else:
            print(f"  {sym:<14}  old={fmt_ts(old_ts)}  NEW did not fire near this event")


if __name__ == "__main__":
    main()
