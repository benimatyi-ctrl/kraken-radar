"""Validate the new scoring logic on the backup DB.

Compares OLD (already-stored alerts/outcomes) vs NEW (re-scored with the
updated weights, synergy bonus, high-conviction bypass and per-symbol
quality dampening).

To keep this tractable we re-score only the (symbol, ts) pairs that
either had a stored shadow_alert or an actually-sent alert in the backup
window. That captures every candidate the old pipeline observed; the
universe coverage problem (the 88% of big moves with no shadow row at
all) is a data-collection issue that this script cannot fix.

Run from project root with the venv that already has pandas installed:

    .\\kraken-radar\\.venv\\Scripts\\python.exe validate_new_logic.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "kraken-radar" / "src"))

from kraken_radar.config import Settings  # noqa: E402
from kraken_radar.features.momentum import rsi_divergence  # noqa: E402
from kraken_radar.features.price import (  # noqa: E402
    breakout_score,
    micro_breakout_5m,
    momentum_15m,
    momentum_1h,
    position_in_1h_range,
    range_expansion,
)
from kraken_radar.features.volume import (  # noqa: E402
    volume_acceleration,
    volume_zscore,
    volume_zscore_short,
)
from kraken_radar.signals.quality import (  # noqa: E402
    SymbolQualityStats,
    dampening_from_quality,
)
from kraken_radar.signals.scoring import (  # noqa: E402
    _apply_strict_filters,
    compute_signal,
)

DB = ROOT / "kraken_radar_backup.db"


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_symbol_quality_lookup(
    con: sqlite3.Connection, lookback_days: int
) -> dict[tuple[str, int], SymbolQualityStats]:
    """For each (symbol, day-bucket) precompute recent quality.

    Day-bucket = ts // 86400000. We resolve the symbol-quality at scoring
    time by snapping to the day-bucket of the candidate ts.
    """
    cur = con.cursor()
    cur.execute(
        "SELECT a.symbol, a.ts, o.max_gain_4h FROM alerts a "
        "JOIN outcomes o ON o.alert_id=a.id WHERE o.max_gain_4h IS NOT NULL"
    )
    history: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in cur.fetchall():
        history[r[0]].append((int(r[1]), float(r[2])))
    for sym in history:
        history[sym].sort()

    cur.execute("SELECT MIN(ts), MAX(ts) FROM shadow_alerts")
    mn, mx = cur.fetchone()
    if mn is None or mx is None:
        return {}
    day_ms = 24 * 3600 * 1000
    lookback_ms = lookback_days * day_ms

    out: dict[tuple[str, int], SymbolQualityStats] = {}
    for sym, items in history.items():
        for day in range(int(mn) // day_ms, int(mx) // day_ms + 1):
            now_ms = day * day_ms + day_ms - 1
            cutoff = now_ms - lookback_ms
            n = 0
            hit = 0
            for ts_a, g4 in items:
                if ts_a < cutoff or ts_a > now_ms:
                    continue
                n += 1
                if g4 >= 0.05:
                    hit += 1
            if n > 0:
                out[(sym, day)] = SymbolQualityStats(
                    n_matured=n, hit5_rate=hit / n if n else 0.0
                )
    return out


def main() -> None:
    cfg = Settings.from_sources(ROOT / "kraken-radar" / "config.yaml")
    print("Config snapshot:")
    print(f"  alert_threshold  = {cfg.signals.alert_threshold}")
    print(f"  shadow_margin    = {cfg.signals.shadow_score_margin}")
    print(f"  high_conviction  = enabled={cfg.signals.high_conviction.enabled} "
          f"threshold={cfg.signals.high_conviction.score_threshold}")
    print(f"  synergy          = enabled={cfg.signals.synergy.enabled} "
          f"bonus={cfg.signals.synergy.bonus_points}")
    print(f"  symbol_quality   = enabled={cfg.signals.symbol_quality.enabled} "
          f"lookback={cfg.signals.symbol_quality.lookback_days}d")
    weights = cfg.signals.weights
    print(
        "  weights          = mom1h=%.0f mom15=%.0f vz=%.0f vzS=%.0f vAcc=%.0f "
        "micro=%.0f re=%.0f bo=%.0f rsi=%.0f pre=%.0f"
        % (
            weights.momentum_1h, weights.momentum_15m, weights.volume_z,
            weights.volume_z_short, weights.volume_acceleration,
            weights.micro_breakout_5m, weights.range_expansion,
            weights.breakout_score, weights.rsi_divergence,
            weights.pre_breakout_proximity,
        )
    )

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("SELECT MIN(ts), MAX(ts) FROM shadow_alerts")
    mn_sh, mx_sh = cur.fetchone()
    cur.execute("SELECT MAX(ts) FROM candles_5m")
    mx_5m = cur.fetchone()[0]
    start_ts = int(mn_sh)
    end_ts = int(mx_5m) - 4 * 3600 * 1000
    print(f"\nReplay window: {fmt(start_ts)} -> {fmt(end_ts)}")

    print("Loading shadow candidates...")
    cur.execute(
        "SELECT symbol, ts FROM shadow_alerts WHERE ts BETWEEN ? AND ?",
        (start_ts, end_ts),
    )
    eval_points = {(r["symbol"], int(r["ts"])) for r in cur.fetchall()}
    cur.execute("SELECT symbol, ts FROM alerts WHERE ts BETWEEN ? AND ?", (start_ts, end_ts))
    for r in cur.fetchall():
        eval_points.add((r["symbol"], int(r["ts"])))
    print(f"  candidate (symbol, ts) pairs to re-score: {len(eval_points)}")

    print("Building rolling per-symbol quality lookup...")
    quality_lookup = load_symbol_quality_lookup(
        con, cfg.signals.symbol_quality.lookback_days
    )
    print(f"  quality buckets indexed: {len(quality_lookup)}")

    by_sym: dict[str, list[int]] = defaultdict(list)
    for sym, ts in eval_points:
        by_sym[sym].append(ts)
    for sym in by_sym:
        by_sym[sym].sort()

    day_ms = 24 * 3600 * 1000

    new_alerts: list[dict] = []
    new_shadows = 0
    n_high_conviction_bypass = 0
    n_synergy_applied = 0
    n_quality_dampened = 0
    n_sym = len(by_sym)
    completed = 0
    t_start = time.time()
    print(f"\nScoring across {n_sym} symbols...")

    for sym, tss in by_sym.items():
        completed += 1
        if completed % 100 == 0:
            elapsed = time.time() - t_start
            print(f"  {completed}/{n_sym} symbols ({elapsed:.0f}s)... NEW alerts so far={len(new_alerts)}")

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
            if result.meta.get("synergy_bonus"):
                n_synergy_applied += 1

            day_bucket = real_ts // day_ms
            quality = quality_lookup.get((sym, day_bucket))
            penalty = dampening_from_quality(quality, cfg)
            if penalty:
                result.score += penalty
                result.meta["quality_penalty"] = penalty
                n_quality_dampened += 1

            shadow_threshold = cfg.signals.alert_threshold - cfg.signals.shadow_score_margin
            if result.score < shadow_threshold:
                continue
            new_shadows += 1
            if result.score < cfg.signals.alert_threshold:
                continue
            result.triggered = True

            result_pre_strict_score = result.score
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
            if any(r.startswith("high_conviction:") for r in result.reasons):
                n_high_conviction_bypass += 1

            if not result.triggered:
                continue

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
                    "pre_strict": result_pre_strict_score,
                    "g4h": g4,
                    "d4h": d4,
                    "g24h": g24,
                    "synergy": float(result.meta.get("synergy_bonus", 0.0)),
                    "penalty": float(result.meta.get("quality_penalty", 0.0)),
                    "high_conv": any(
                        r.startswith("high_conviction:") for r in result.reasons
                    ),
                }
            )
            last_alert_ts = real_ts

    print(
        f"\nNEW logic raw counts: "
        f"triggered={len(new_alerts)}, shadow_seen={new_shadows}, "
        f"synergy_applied={n_synergy_applied}, "
        f"high_conviction_bypass={n_high_conviction_bypass}, "
        f"quality_dampened={n_quality_dampened}"
    )

    # Dedupe to one per symbol per 4h bucket
    seen: set[tuple[str, int]] = set()
    deduped: list[dict] = []
    for a in sorted(new_alerts, key=lambda x: x["ts"]):
        key = (a["symbol"], a["ts"] // (4 * 3600 * 1000))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    print(f"After 4h-bucket dedup: NEW={len(deduped)} triggered alerts")

    n_eval = 0
    h5 = 0
    h10 = 0
    sum_g4 = 0.0
    sum_d4 = 0.0
    for a in deduped:
        if a["g4h"] is None:
            continue
        n_eval += 1
        sum_g4 += a["g4h"]
        sum_d4 += a["d4h"] or 0.0
        if a["g4h"] >= 0.05:
            h5 += 1
        if a["g24h"] is not None and a["g24h"] >= 0.10:
            h10 += 1

    print("\n=== NEW LOGIC SUMMARY ===")
    if n_eval > 0:
        print(
            f"NEW: n={n_eval}  mean g4h={sum_g4/n_eval*100:.2f}%  mean d4h={sum_d4/n_eval*100:.2f}%  "
            f"hit>=+5%/4h={h5/n_eval*100:.1f}%  hit>=+10%/24h={h10/n_eval*100:.1f}%"
        )

    cur.execute(
        "SELECT a.symbol, a.ts, a.score, o.max_gain_4h, o.max_dd_4h, o.max_gain_24h "
        "FROM alerts a LEFT JOIN outcomes o ON o.alert_id=a.id "
        "WHERE a.ts BETWEEN ? AND ? ORDER BY a.ts ASC",
        (start_ts, end_ts),
    )
    old_rows = list(cur.fetchall())
    o_n = 0
    o5 = 0
    o10 = 0
    o_g = 0.0
    o_d = 0.0
    for r in old_rows:
        if r["max_gain_4h"] is None:
            continue
        o_n += 1
        o_g += r["max_gain_4h"]
        o_d += r["max_dd_4h"] or 0.0
        if r["max_gain_4h"] >= 0.05:
            o5 += 1
        if r["max_gain_24h"] is not None and r["max_gain_24h"] >= 0.10:
            o10 += 1
    print("\n=== OLD LOGIC (already-sent alerts) ===")
    if o_n > 0:
        print(
            f"OLD: n={o_n}  mean g4h={o_g/o_n*100:.2f}%  mean d4h={o_d/o_n*100:.2f}%  "
            f"hit>=+5%/4h={o5/o_n*100:.1f}%  hit>=+10%/24h={o10/o_n*100:.1f}%"
        )

    # NEW high-conviction subset
    hc = [a for a in deduped if a["high_conv"] and a["g4h"] is not None]
    if hc:
        hc_g = sum(a["g4h"] for a in hc) / len(hc) * 100
        hc_h5 = sum(1 for a in hc if a["g4h"] >= 0.05) / len(hc) * 100
        hc_h10 = sum(1 for a in hc if (a["g24h"] or 0) >= 0.10) / len(hc) * 100
        print(f"\nNEW high-conviction-only (score>=80, bypassed strict): "
              f"n={len(hc)} mean g4h={hc_g:.2f}% hit5={hc_h5:.1f}% hit10/24h={hc_h10:.1f}%")

    # Top 25 NEW alerts by 4h gain
    print("\nTop 25 NEW alerts by 4h gain:")
    print(f"  {'time':<17} {'symbol':<14} {'sc':>6} {'g4%':>8} {'hc':>3} {'syn':>4} {'pen':>5}")
    for a in sorted(
        (a for a in deduped if a["g4h"] is not None),
        key=lambda x: -x["g4h"],
    )[:25]:
        print(
            f"  {fmt(a['ts']):<17} {a['symbol']:<14} {a['score']:>6.1f} "
            f"{a['g4h']*100:>7.2f}  {'Y' if a['high_conv'] else 'n':>3} "
            f"{a['synergy']:>3.0f} {a['penalty']:>5.1f}"
        )


if __name__ == "__main__":
    main()
