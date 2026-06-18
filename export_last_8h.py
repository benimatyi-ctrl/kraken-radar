"""Export the last 8 hours of data from kraken_radar_backup.db into a new SQLite file.

Reference point: the MAX(ts) across candle tables in the source DB (so the
cutoff is "8 hours before the most recent data in the backup", not "now").

Copied tables:
- candles_5m / candles_1h / candles_4h : rows where ts >= cutoff
- alerts                                : rows where ts >= cutoff
- shadow_alerts                         : rows where ts >= cutoff
- outcomes / outcomes_shadow            : rows linked to the copied alerts
- tokens / system_state                 : copied in full (no time column)

Usage:
    python export_last_8h.py [--hours 8] [--out kraken_radar_last_8h.db]
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SRC_DB = ROOT / "kraken_radar_backup.db"

SCHEMA_SQL = (ROOT / "kraken-radar" / "src" / "kraken_radar" / "db" / "schema.sql").read_text(
    encoding="utf-8"
)


def fmt_ts(ms: int | None) -> str:
    if ms is None:
        return "n/a"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=8.0, help="Window size in hours (default: 8).")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "kraken_radar_last_8h.db",
        help="Output .db file path.",
    )
    parser.add_argument(
        "--reference",
        choices=("max-ts", "now"),
        default="max-ts",
        help="Cutoff reference point (default: max-ts from source DB).",
    )
    args = parser.parse_args()

    if not SRC_DB.exists():
        print(f"Source DB not found: {SRC_DB}", file=sys.stderr)
        return 1

    out_path: Path = args.out
    if out_path.exists():
        out_path.unlink()

    fd, snap_str = tempfile.mkstemp(prefix="kr_snapshot_", suffix=".db")
    import os
    os.close(fd)
    snapshot_path = Path(snap_str)
    print(f"Creating snapshot (file copy) at: {snapshot_path}")
    shutil.copyfile(SRC_DB, snapshot_path)
    size_mb = snapshot_path.stat().st_size / (1024 * 1024)
    print(f"Snapshot ready ({size_mb:.1f} MB).")

    src = sqlite3.connect(f"file:{snapshot_path.as_posix()}?mode=ro&immutable=1", uri=True)
    src.row_factory = sqlite3.Row
    cur = src.cursor()

    try:
        cur.execute("PRAGMA integrity_check")
        ok = cur.fetchone()[0]
    except sqlite3.DatabaseError as exc:
        print(f"Snapshot integrity check failed: {exc}", file=sys.stderr)
        return 3
    print(f"Snapshot integrity_check: {ok}")

    if args.reference == "now":
        ref_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    else:
        cur.execute(
            "SELECT MAX(mx) FROM ("
            "  SELECT MAX(ts) AS mx FROM candles_5m"
            "  UNION ALL SELECT MAX(ts) FROM candles_1h"
            "  UNION ALL SELECT MAX(ts) FROM candles_4h"
            ")"
        )
        row = cur.fetchone()
        ref_ts = int(row[0]) if row and row[0] is not None else None
        if ref_ts is None:
            print("No candle data in source DB.", file=sys.stderr)
            return 2

    window_ms = int(args.hours * 3600 * 1000)
    cutoff = ref_ts - window_ms

    print("=" * 80)
    print(f"Source: {SRC_DB.name}  ({SRC_DB.stat().st_size / (1024*1024):.1f} MB)")
    print(f"Output: {out_path.name}")
    print(f"Reference ts : {fmt_ts(ref_ts)}  ({ref_ts})")
    print(f"Cutoff       : {fmt_ts(cutoff)}  ({cutoff})")
    print(f"Window       : last {args.hours:g} hour(s)")
    print("=" * 80)

    dst = sqlite3.connect(out_path)
    dst.executescript(SCHEMA_SQL)
    dst.commit()

    def copy_rows(query: str, params: tuple, insert_sql: str, label: str) -> int:
        local_cur = src.cursor()
        try:
            local_cur.execute(query, params)
        except sqlite3.DatabaseError as exc:
            print(f"  {label:<20s} execute FAILED: {exc}")
            return 0
        n_ok = 0
        n_skip = 0
        batch: list[tuple] = []
        while True:
            try:
                row = local_cur.fetchone()
            except sqlite3.DatabaseError as exc:
                n_skip += 1
                print(f"  {label:<20s} skipped a corrupt row: {exc} (continuing)")
                try:
                    local_cur.execute(query, params)
                    for _ in range(n_ok + n_skip):
                        try:
                            local_cur.fetchone()
                        except sqlite3.DatabaseError:
                            pass
                    continue
                except sqlite3.DatabaseError:
                    break
            if row is None:
                break
            batch.append(tuple(row))
            n_ok += 1
            if len(batch) >= 5000:
                dst.executemany(insert_sql, batch)
                dst.commit()
                batch.clear()
        if batch:
            dst.executemany(insert_sql, batch)
            dst.commit()
        skip_note = f"  (skipped {n_skip} corrupt rows)" if n_skip else ""
        print(f"  {label:<20s} {n_ok:,}{skip_note}")
        local_cur.close()
        return n_ok

    print("\nCopying rows...")

    candle_cols = "symbol, ts, open, high, low, close, volume"
    candle_ins = f"INSERT OR REPLACE INTO {{t}} ({candle_cols}) VALUES (?, ?, ?, ?, ?, ?, ?)"
    for tbl in ("candles_5m", "candles_1h", "candles_4h"):
        copy_rows(
            f"SELECT {candle_cols} FROM {tbl} WHERE ts >= ? ORDER BY symbol, ts",
            (cutoff,),
            candle_ins.format(t=tbl),
            tbl,
        )

    alert_cols = "id, symbol, ts, score, features_json, regime_json, created_at"
    n_alerts = copy_rows(
        f"SELECT {alert_cols} FROM alerts WHERE ts >= ? ORDER BY ts",
        (cutoff,),
        f"INSERT INTO alerts ({alert_cols}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        "alerts",
    )

    shadow_cols = (
        "id, symbol, ts, score, features_json, regime_json, "
        "sent_to_telegram, suppression_reason, created_at"
    )
    n_shadow = copy_rows(
        f"SELECT {shadow_cols} FROM shadow_alerts WHERE ts >= ? ORDER BY ts",
        (cutoff,),
        f"INSERT INTO shadow_alerts ({shadow_cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "shadow_alerts",
    )

    outcome_cols = (
        "alert_id, max_gain_1h, max_dd_1h, max_gain_4h, max_dd_4h, "
        "max_gain_24h, max_dd_24h, computed_at"
    )
    copy_rows(
        f"SELECT {outcome_cols} FROM outcomes "
        "WHERE alert_id IN (SELECT id FROM alerts WHERE ts >= ?)",
        (cutoff,),
        f"INSERT INTO outcomes ({outcome_cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "outcomes",
    )

    outcome_s_cols = (
        "shadow_alert_id, max_gain_1h, max_dd_1h, max_gain_4h, max_dd_4h, "
        "max_gain_24h, max_dd_24h, computed_at"
    )
    copy_rows(
        f"SELECT {outcome_s_cols} FROM outcomes_shadow "
        "WHERE shadow_alert_id IN (SELECT id FROM shadow_alerts WHERE ts >= ?)",
        (cutoff,),
        f"INSERT INTO outcomes_shadow ({outcome_s_cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        "outcomes_shadow",
    )

    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    src_tables = {r[0] for r in cur.fetchall()}
    if "tokens" in src_tables:
        copy_rows(
            "SELECT symbol, base, quote, active, min_volume_eur, last_seen_at, created_at FROM tokens",
            (),
            "INSERT OR REPLACE INTO tokens (symbol, base, quote, active, min_volume_eur, last_seen_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            "tokens",
        )
    if "system_state" in src_tables:
        copy_rows(
            "SELECT key, value, updated_at FROM system_state",
            (),
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
            "system_state",
        )

    dst.execute("VACUUM")
    dst.commit()
    dst.close()
    src.close()

    try:
        snapshot_path.unlink()
    except OSError:
        pass

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print("\nDone.")
    print(f"Output file: {out_path}  ({size_mb:.2f} MB)")
    print(f"Alerts copied: {n_alerts}   Shadow alerts copied: {n_shadow}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
