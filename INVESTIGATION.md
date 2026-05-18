# Benchmark Investigation — Figure AI Tracker

**Date:** 2026-05-18
**Stream snapshot:** 111.19h, 138,149 pkgs, 361 readings (342 OCR, 12 research, 6 screenshot, 1 ocr_manual).
**Phases observed:** unknown (research, 7.7→95.1h), split_challenge (97.2→106.0h), solo_active (106.05→111.19h+).

---

## 1. Current metrics — exactly how each is computed

| Metric | Algorithm | Inputs | Known bias |
|---|---|---|---|
| `total_packages` | `readings[-1].packages` | Latest row | None |
| `total_hours` | `readings[-1].stream_hours` | Latest row | None |
| `overall_rate` | `total_packages / total_hours` | All rows | **Mixes all phases** (research + challenge + solo) |
| `rates_by_window["1h"]` | `(pkg[-1] - pkg[where stream_h ≥ latest-1]) / Δh` | Last-N-hours readings | OK for dense OCR |
| `trend_slope` | `numpy.polyfit(hour, rate, deg=1)` over `_pairwise_rates` | All consecutive pairs | **Each pair weighted equally** — recent OCR has 100× more pairs than research, so slope is dominated by recent noise |
| `peak_rate` | `max(rate)` of pairs with `delta_hours ≥ 0.5` | All pairs | **Includes research-research pairs** → fake 1500/h peak (audit found this) |
| `low_rate` | `min(rate)` same filter | All pairs | Same problem — 666/h "dock" is two journalist round-numbers |
| `learning_velocity` | first ⅓ of N readings vs last ⅓ | All rows | **Count-equal, not time-equal**: early ⅓ = 90.3h (sparse), recent ⅓ = 0.54h (dense). 168× span mismatch — biased |
| `dock_events` | `rate < 400/h AND delta_hours < 1.5h` | All pairs | Catches sparse-data artefacts as "dock events" |
| `agi.human_rate` | `latest.human_packages / latest.shift_hours` | Last split-screen reading | Currently stale — challenge ended 5h ago; field still active |
| `verdict.score` | Sum of 5 weighted signals | All metrics above | **Inherits biases** of underlying metrics |

## 2. What we don't observe (but should)

### A. **Per-phase rates** — the most important miss
The 1,264/h overall hides 3 different regimes:

| Phase | Rate (Δpkg / Δh within phase) | Sample size |
|---|---|---|
| `unknown` (research, 7.7→95.1h) | 1,236/h | 15 reads, 87.3h |
| `split_challenge` (97.2→106.0h) | **1,289/h** | 196 reads, 8.79h |
| `solo_active` (106.05h→now) | **1,226/h** | 149 reads, 5.14h |

**Insight:** the robot worked ~5% harder during the challenge than alone. Currently NOT surfaced anywhere.

### B. **Time-of-day patterns** — a real signal we're hiding

From 342 OCR pair-deltas, mean rate per UTC hour:

| UTC hour | N | Mean rate/h |
|---:|---:|---:|
| 20 (evening) | 21 | **1,404** |
| 21 | 29 | 1,371 |
| 22 | 29 | 1,324 |
| 23 | 28 | 1,291 |
| 0 (overnight) | 27 | 1,087 |
| 1 | 28 | 1,358 |
| 4 | 29 | **1,141** |
| 7 (morning) | 30 | 1,244 |

The robot is ~25% faster in early evening UTC than at 04:00 UTC. We don't surface this.

### C. **Consistency / variance** — a learning signal
Currently: mean rate. Missing: stddev, percentiles, coefficient of variation.

Observed:
- Mean 1,264/h, stddev 195/h, **CV = 15.4%**
- Quartiles: p25=1154 / p50=1267 / p75=1385

If CV drops over time, robot is becoming more consistent — that's learning. We don't measure this.

### D. **Streaks** — longest runs above/below baseline
We never compute "longest stretch above 1,400/h" or "longest dip below 1,000/h". These are intuitive performance signals.

### E. **Predictive ETAs**
At current 1,264/h:
- 150,000 packages reached at stream ≈ 121.6h (in ~10.4h)
- 200,000 reached at ≈ 161.1h (in ~50h)

Useful for stakeholders. Not exposed.

### F. **Shift completion records**
The split_challenge phase ENDED at 106.01h. Final scores are accessible (human vs robot at last reading of phase). We don't record this as a discrete "shift result" object.

Last reading in challenge: human=5,??? robot=5,??? (need to query).

### G. **Handoff productivity cost**
When ROSE → GARY occurs (none yet, but coming), we should measure: rate over 10 min before handoff vs 10 min after. Quantifies team-rotation cost.

### H. **OCR identity noise**
"BOSE" appeared once at 110.39h — vision misread "ROSE". Need fuzzy canonicalisation (1-char Levenshtein) or whitelist with closest-match.

### I. **Confidence intervals**
"Rate is 1,264/h" should be "1,264 ± 23/h (95% CI)". Currently no error bars.

### J. **Phase coverage by source**
We have phase counts but not "what % of split_challenge data came from screenshot vs OCR". Quality differs per phase.

## 3. Bias fixes (correctness, not new params)

1. **`get_learning_velocity`** — change from count-equal to time-equal windows: use last 2h of OCR vs equivalent 2h slice of earlier data (when available). When not enough, mark `available: false` with reason.

2. **`get_peak_rate`** — exclude pairs where both endpoints are `source=research`. Add `peak_verified` (high-source-only) vs `peak_raw` (any source).

3. **`get_dock_events`** — require `source=ocr` only. Real dock events only happen with dense data.

4. **`predict_agi_moment`** — return `active: false` if no recent (last 30 min) split_challenge reading exists. Show LAST CHALLENGE RESULT instead.

5. **`trend_slope`** — weight regression by source confidence, or limit to recent N hours of dense OCR.

6. **`overall_rate`** — split into 3 numbers: `rate_research`, `rate_challenge`, `rate_solo`. Stop reporting the meaningless combined number.

## 4. New parameters (in priority order)

| # | Parameter | Question it answers | Formula |
|---|---|---|---|
| 1 | `rate_by_phase` | "How fast is robot in each regime?" | Δpkg / Δh within phase rows |
| 2 | `rate_distribution` | "How consistent?" | mean / stddev / p25/p50/p75 / CV |
| 3 | `time_of_day_pattern` | "Are evenings faster?" | Aggregate OCR Δrates by `datetime.utcfromisoformat(captured_at).hour` |
| 4 | `streaks` | "Longest sustained period at peak?" | Max run length where rate > p75; same for < p25 |
| 5 | `predictive_etas` | "When reaches 150k / 200k pkgs?" | `(target - current_pkgs) / current_rate` |
| 6 | `shift_records` | "How did past challenges end?" | Last reading per contiguous split_challenge run |
| 7 | `handoff_productivity` | "Cost of robot rotation?" | Rate (-10m..0m) vs (0m..+10m) around each handoff |
| 8 | `confidence_intervals` | "How precise is my rate estimate?" | mean ± 1.96·(stddev/√N) on rate per phase |
| 9 | `identity_canonicalization` | "Is 'BOSE' really Rose?" | Levenshtein ≤ 1 vs robot whitelist; flag if ambiguous |
| 10 | `phase_source_coverage` | "What sources cover each phase?" | Group-by phase × source counts |

## 5. Recommended implementation order

**Wave 1 (most impactful, low effort):**
- `rate_by_phase` + `rate_distribution` (1 function, 30 lines)
- `time_of_day_pattern` (1 function)
- Fix `get_learning_velocity` bias

**Wave 2 (predictive/diagnostic):**
- `streaks` + `predictive_etas`
- `shift_records`
- Fix `predict_agi_moment` stale-data bug

**Wave 3 (provenance/quality):**
- `identity_canonicalization`
- `handoff_productivity`
- `confidence_intervals`

**Wave 4 (presentation):**
- Frontend section "Patterns & Predictions" with time-of-day heatmap, consistency gauge, ETA banner
- Audit page extension: show phase × source quality matrix
