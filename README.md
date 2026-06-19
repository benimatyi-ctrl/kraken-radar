# kraken-radar

A live, data-driven **signal-detection and research system** for crypto spot markets. It ingests Kraken OHLCV data, computes a set of momentum / volume / breakout features, scores each symbol with an empirically calibrated two-tier model, and sends Telegram alerts — while logging every alert and its outcome so the model can be **backtested and recalibrated against its own track record**.

Built solo as an end-to-end project: data ingestion, feature engineering, signal scoring, alerting, paper-trading simulation, and an offline analysis/backtesting toolkit.

> **Honest note on performance.** This is a research and engineering project, not financial advice or a profitable trading system. On the recorded shadow window, the strongest single feature reached ~36% hit-rate (vs a ~9% base rate), which is still below a realistic break-even threshold once fees and slippage are considered. The value of the project is the **methodology** — measuring, validating, and recalibrating against real outcome data — not a profit claim.

---

## What it does

```
Kraken OHLCV (ccxt, 5m)
        │
        ▼
Feature engineering ── volume z-score, volume acceleration, range
        │              expansion, breakout score, micro-breakout,
        │              momentum (15m / 1h), pre-breakout proximity,
        │              RSI divergence, (optional) order-book / trade-flow
        ▼
Normalization (clip → [0,1] or signed) ── shared feature pass
        │
        ▼
Two-tier scoring
   ├─ Confirmed tier  → momentum-weighted + strict breakout filters
   └─ Early Watch tier → pre-breakout "coil" detection, relaxed gates
        │
        ▼
Regime gate (BTC volatility / trend) + synergy bonus + per-symbol
quality dampening
        │
        ▼
Telegram alert ── stored in SQLite with full score breakdown
        │
        ▼
Outcome tracking + paper-trading simulation
        │
        ▼
Offline analysis & backtesting (recalibrate weights from real outcomes)
```

---

## Key features

**Two-tier signal model.** A *Confirmed* tier (momentum-weighted, with mandatory breakout-confirmation filters) and an *Early Watch* tier (detects range compression / pressing highs **before** a confirmed breakout, with its own weights and relaxed gates). Both share a single normalized feature pass per symbol for efficiency.

**13 engineered features**, each clipped to a configured range and normalized to `[0,1]` (positive features) or `[-1,1]` (signed features) before weighting — so no single raw feature can dominate the score. Features include volume z-score (long and short lookback), volume acceleration, range expansion, breakout score, 5-minute micro-breakout, 15-minute and 1-hour momentum, pre-breakout proximity (a tent-shaped curve that rewards "coiled near highs" and penalizes "already broken out"), and RSI divergence.

**Data-driven weight calibration.** Weights are not guessed — they were recalibrated from a ~290-hour shadow-data study of recorded alerts and their outcomes. The analysis found 1-hour momentum to be the strongest single predictor (top-quintile ~36% hit-rate vs a ~9% base rate; ~5× lift in the strongest band), so it carries the highest weight, while features whose top quintile underperformed the base rate were down-weighted.

**Empirical refinements**, each tied to a documented observation in the outcome study:
- *Synergy bonus* — a small additive boost when the two strongest leading indicators (1h momentum and volume z-score) co-fire above thresholds that historically delivered a markedly higher hit-rate.
- *High-conviction bypass* — above an empirically safe score threshold, strict breakout filters are skipped (the regime gate still applies), because the study showed many high-scoring setups were being suppressed by overly strict gates.
- *Per-symbol quality dampening* — a rolling penalty for symbols that recently produced repeated zero-hit alerts, scaled by how far their recent hit-rate sits below target.

**Regime gating.** Alerts are suppressed during adverse BTC volatility / trend conditions, so the system stays quiet during market-wide stress.

**Outcome tracking + paper trading.** Every alert is stored with its full score breakdown; a simulated spot-buy paper-trading layer (virtual cash, stop-loss / take-profit / max-hold) lets the strategy be evaluated without risking real funds.

**Offline analysis toolkit.** Separate scripts for replaying the new scoring logic over recorded data, scanning alert thresholds, validating logic changes, and exporting recent windows — i.e. the tooling needed to *measure* the system, not just run it.

---

## Tech stack

- **Python 3.11+**, `asyncio` for the live polling loop
- **ccxt** for Kraken market-data ingestion
- **pandas** for feature engineering
- **SQLite** (`aiosqlite`) for candle storage, alert logging, and outcome tracking
- **Pydantic** / **pydantic-settings** for typed, validated configuration (YAML defaults + environment overrides; secrets handled via `SecretStr`)
- **aiohttp** for a small local control UI (start / stop / monitor with live log tail)
- **Telegram Bot API** for alert delivery
- Deployed on a **Hetzner VPS** for continuous operation

---

## Architecture notes

The code separates **pure strategy logic** (feature math and scoring, side-effect free) from the **I/O layers** (data ingestion, storage, alerting). Scoring functions take feature dictionaries and config in, and return a structured `SignalResult` (score, per-feature components, trigger decision, human-readable reasons, and a `meta` block recording exactly why the score moved). This makes the system observable — every alert can be explained — and testable, since the scoring can be exercised on recorded data without any live connection.

Configuration is fully typed and centralized: every threshold, clip range, lookback, and weight lives in `config.yaml` and is validated by Pydantic models, so experiments are reproducible and the calibration is transparent.

---

## Project structure

```
kraken_radar/
├── config.py            # Typed Pydantic settings (YAML + env)
├── main.py              # Live polling / orchestration entrypoint
├── logging_setup.py     # Rotating file + console logging
├── ui.py                # Local aiohttp control UI
├── signals/
│   ├── scoring.py       # Two-tier scoring engine (pure logic)
│   └── quality.py       # Synergy bonus + per-symbol quality dampening
├── features/            # Volume, price, momentum, regime feature math
config.yaml              # All weights / clips / thresholds (documented)
analyze_*.py             # Offline analysis scripts
replay_new_logic.py      # Backtest new scoring over recorded data
threshold_scan.py        # Alert-threshold sweep
validate_new_logic.py    # Validate logic changes
export_last_8h.py        # Data export
```

---

## What I learned

This project taught me the full loop of a data product: ingesting noisy real-world data, engineering features, and — most importantly — **measuring whether the model actually works and recalibrating it honestly against real outcomes** rather than trusting intuition. The hardest and most valuable lesson was statistical: distinguishing a genuine edge from noise, and accepting that a result below break-even is still a real, useful finding.

---

*This project is for research and educational purposes only. It is not financial advice and does not execute real trades.*
