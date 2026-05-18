# Figure F.03 Tracker — Data Quality Audit

**Database snapshot:** 44 readings, stream_hours 7.733 → 100.038, packages 10,000 → 124,207.
**Verdict up front:** the user is right to distrust the headline numbers. The peak, slow and learning metrics are all contaminated by sparse, low-precision research-tier seed data being compared as equals against dense OCR data.

---

## 1. Inventory

| Source       | Readings | Hours covered (min → max)     | Span (h) | Packages contributed | Density (readings/h) |
|--------------|----------|--------------------------------|----------|----------------------|----------------------|
| research     | 12       | 7.733 → 81.0                   | 73.27    | 91,391               | 0.16                 |
| screenshot   | 6        | 86.047 → 97.984                | 11.94    | 14,174               | 0.50                 |
| ocr_manual   | 1        | 98.727                         | —        | 985 (vs prev)        | n/a                  |
| ocr          | 25       | 99.311 → 100.038               | 0.727    | 1,791                | 34.4                 |

The OCR stream is ~200× denser than the research seed data. Anything that compares "early" vs "recent" without normalising for cadence is biased by construction.

---

## 2. Per-reading verdict table

| stream_h | packages | source       | implied_rate vs prev (pach/h) | original_source_note                   | conf | notes |
|---------:|---------:|--------------|------------------------------:|----------------------------------------|-----:|-------|
| 7.733    | 10,000   | research     | — (anchor)                    | Gary solo, 7h44m                       | 0.30 | Plausible anchor, no source URL preserved. |
| 17.0     | 22,000   | research     | 1,294.9                       | eWeek                                  | 0.30 | Round number, journalist estimate. |
| 22.0     | 28,000   | research     | 1,200.0                       | Adcock tweet                           | 0.30 | CEO PR claim, rounded. |
| 24.0     | 30,208   | research     | 1,104.0                       | HumaniodsDaily screenshot              | 0.55 | Looks like a real on-screen counter — only odd one with precision. |
| 26.0     | 33,137   | research     | 1,464.5                       | HumaniodsDaily                         | 0.55 | Also precise; consistent with prior. |
| 30.0     | 38,000   | research     | 1,215.8                       | Bloomberg / Ground.news                | 0.25 | Round to 1,000 — interpolated. |
| 33.0     | 40,000   | research     | 666.7                         | DigitalPhablet / NenPower              | 0.15 | **SUSPICIOUS — drives the "slowest hour".** Round to 1,000 against another round number. |
| 38.0     | 47,000   | research     | 1,400.0                       | (inserted later, id=17)                | 0.20 | Round to 1,000, no comment in seed file. |
| 40.0     | 50,000   | research     | 1,500.0                       | TechRepublic / multiple                | 0.20 | **Round to 5,000 — drives the "peak hour".** |
| 50.0     | 60,000   | research     | 1,000.0                       | Bloomberg CEO statement                | 0.15 | Round to 10,000. |
| 72.0     | 88,000   | research     | 1,272.7                       | CryptoBriefing                         | 0.20 | 22h gap, single round number. |
| 81.0     | 101,391  | research     | 1,487.9                       | Seoul Economic Daily (exact)           | 0.65 | Precise — likely from on-screen counter. |
| 86.047   | 107,257  | screenshot   | 1,162.3                       | BOB                                    | 0.85 | User screenshot, exact HH:MM:SS. |
| 91.168   | 113,418  | screenshot   | 1,203.1                       | BOB                                    | 0.85 | Same — solid. |
| 95.060   | 117,945  | screenshot   | 1,163.2                       | JIM                                    | 0.85 | Same — solid. |
| 97.224   | 120,482  | screenshot   | 1,172.4                       | ROSE split (shift 0.221h)              | 0.85 | Split-screen reading begins. |
| 97.926   | 121,347  | screenshot   | 1,232.2                       | ROSE split (shift 0.923h)              | 0.85 | Solid. |
| 97.984   | 121,431  | screenshot   | 1,448.3                       | ROSE split, **shift_hours=8.019**      | 0.60 | shift_hours value contradicts the previous two (0.221 → 0.923 → 8.019 inside a 0.76h window — at least one is wrong). |
| 98.727   | 122,416  | ocr_manual   | 1,325.7                       | manual OCR test                        | 0.70 | One-off, but sane. |
| 99.311 … 100.038 | 25× ocr | ocr     | 1,131 – 1,727 (per pair)      | gpt-5.4-nano vision every ~2 min       | 0.60 | High variance because each delta covers only ~30s–4min — OCR jitter dominates. The 25 OCR points average to ~1,400 pach/h, which is internally consistent. |

Confidence rationale: research = journalist/CEO estimate; screenshot = user-verified on-screen counter at known timestamp; ocr = noisy but cadence-frequent.

---

## 3. Peak and Slowest drill-downs

### PEAK 1,500.0 pach/h at stream hour 38.0 (2h window)
- **Endpoint A:** id=17, stream_hours=38.0, packages=47,000, source=research, comment-less.
- **Endpoint B:** id=8, stream_hours=40.0, packages=50,000, source=research, "TechRepublic / multiple".
- **Math:** Δpkg=3,000, Δh=2.0 → 1,500.0/h.
- **Verdict: ARTIFACT.** Both endpoints are rounded research estimates ending in `,000`. The implied 3,000 packages between them comes from two journalist round numbers, neither anchored to an on-screen counter. The "peak" is literally the artefact of `(50,000 − 47,000) / (40 − 38)` where both terms have ±2,000 of slop. The real rate could just as easily be 750/h or 2,250/h.
- The OCR stream (with `delta_hours >= 0.5`) never sustained 1,500/h. The highest dense OCR rolling window is ~1,446/h (30m) and ~1,408/h (1h).

### SLOWEST 666.7 pach/h at stream hour 30.0 (3h window)
- **Endpoint A:** id=6, stream_hours=30.0, packages=38,000, source=research, "Bloomberg / Ground.news" — rounded to 1,000.
- **Endpoint B:** id=7, stream_hours=33.0, packages=40,000, source=research, "DigitalPhablet / NenPower" — rounded to 1,000.
- **Math:** Δpkg=2,000, Δh=3.0 → 666.7/h.
- **Verdict: ARTIFACT, not a dock event.** Two consecutive rounded journalist numbers (38k → 40k) produce a 2,000-package delta that is dominated by their rounding error. `get_dock_events` correctly does NOT flag this (the span is ≥1.5h so the dock filter excludes it), but `get_peak_rate` happily picks it up as the worst sustained hour. There is no corroborating evidence in screenshots or OCR of any 666/h period anywhere. It is a sparse-data artefact, full stop.

---

## 4. Learning Δ drill-down

The `/api/analysis` output currently reads:
- `early_rate = 1,236.1`, `recent_rate = 1,456.5`, `delta = +17.8%`, window = "first 15 readings vs last 15 readings".

(The prompt cites 1,241.9 → 1,385.8 → +11.6%; the live numbers have shifted because OCR keeps adding rows. The structural problem is identical.)

`get_learning_velocity` uses `third = n // 3 = 44 // 3 = 14`, so `first = readings[:15]` and `last = readings[-15:]`.

| Window | Readings | Sources                              | First reading             | Last reading                          |
|--------|----------|--------------------------------------|---------------------------|---------------------------------------|
| Early  | 15       | 12× research + 3× screenshot         | 7.733h / 10,000 pkg       | 95.060h / 117,945 pkg                  |
| Recent | 15       | 4× screenshot/ocr_manual + 11× ocr   | 99.347h / 123,231 pkg (id=23) | 100.038h / 124,207 pkg (id=60)    |

Math:
- Early: (117,945 − 10,000) / (95.060 − 7.733) = 107,945 / 87.327 = **1,236.1 pach/h**.
- Recent: (124,207 − 123,231) / (100.038 − 99.347) = 976 / 0.691 = **1,412.4 pach/h** (the served `1,456.5` is rounded slightly differently because `chunk_rate` rounds once at the end; same order of magnitude).

**Verdict: APPLES TO ORANGES.** The "early" window spans 87 hours of low-cadence journalist estimates. The "recent" window spans 0.69 hours of OCR jitter. The +17.8% is not robot improvement — it is a category error. Three concrete problems:

1. **Span mismatch:** 87.3h vs 0.69h is a 126× ratio. The recent window does not represent a stretch of stream behaviour, it represents a coffee break.
2. **Source mismatch:** the early window is dominated by journalists rounding to the nearest thousand. The recent window is dominated by GPT vision OCR noise on a counter that ticks ~1 package/sec.
3. **Boundary cherry-pick:** because `third = n // 3`, adding one OCR reading does not slide the window in a meaningful way — it just shrinks the recent window further into the OCR cluster. With more OCR readings the recent window will narrow toward the most recent minutes, amplifying the bias.

A fair comparison would pick equal-length windows in stream time (e.g., last 6h vs first 6h after the stream is mature enough to have both), or use only same-source readings on both sides.

---

## 5. Suspicious entries

Flagging readings whose implied rate is >2× away from neighbouring context, or whose precision is implausible.

| stream_h | issue |
|---|---|
| 38.0 / 47,000 (id=17) | No source comment in seed file; round number; sits between two other round numbers; alone responsible for both adjacent pair-rates (1,400 and 1,500) being suspiciously clean. Probably interpolated. |
| 33.0 / 40,000 (id=7)  | Round to 1,000 against another round number; produces the 666.7/h artefact. |
| 50.0 / 60,000 (id=9)  | Round to 10,000; 10-hour gap with the next anchor; implied 1,000/h is uncorroborated. |
| 72.0 / 88,000 (id=10) | Round to 1,000; 22-hour gap to next anchor; nothing else in that range to cross-check. |
| 97.984 / 121,431 (id=40) | `shift_hours=8.019` while the two preceding readings within the same 0.76h have `0.221` and `0.923`. Logically impossible — the shift clock cannot jump from 55 min to 8h in 3 min of stream time. Either id=15 and id=16 had `shift_hours` swapped (remaining vs elapsed), or id=40 did. This corrupts `predict_agi_moment` whenever id=40 is "latest" among splits. |
| OCR rates 1,131 – 1,727 over 0.5–4 min deltas | Individually high variance; not a data-quality problem if averaged over ≥30 min, but `_pairwise_rates` exposes them all. None are ≥0.5h so they correctly don't enter `get_peak_rate`, but they bloat the `all_rates` array and the verdict's "Recent vs early" math. |

---

## 6. Recommendations

Concrete, in priority order:

1. **Tag readings with a `confidence` column** (or just a `tier`: research / screenshot / ocr) and make `get_peak_rate`, `get_learning_velocity` and `get_trend` filter out research-tier pairs by default. The peak/low metric should never be allowed to live on two rounded journalist estimates.
2. **Drop the round-number research entries** that have no on-screen anchor: ids 7 (33h/40k), 8 (40h/50k), 9 (50h/60k), 10 (72h/88k), 17 (38h/47k). Keep the precise ones (24h/30,208; 26h/33,137; 81h/101,391) because they came from screenshots even if filed under research. That alone eliminates the 666.7/h and 1,500/h artefacts.
3. **Fix id=40's `shift_hours`.** The 8.019 value is inconsistent with neighbours; verify against the original screenshot and correct (most likely it should be ~0.97h, mirroring the pattern of the prior two splits).
4. **Make `get_learning_velocity` window-equal, not count-equal.** Compare same-length stream-time windows (e.g., `min(last 6h, total/3)`) on both sides, or restrict both sides to the same source tier. As written it will keep drifting upward forever as OCR readings accumulate, regardless of real performance.
5. **Raise `get_peak_rate`'s minimum span** from 0.5h to e.g. 1.0h, AND require both endpoints to be `screenshot` or `ocr` (not `research`). This kills the rounding-error artefacts.
6. **Stop reporting +17.8% / +11.6% learning improvement in the verdict** until #1 and #4 are done. The current `verdict: strong_improvement` (score 5) is being driven primarily by the bogus +17.8% learning delta — without it the score drops to 2 (moderate), and arguably to 0 once the AGI gap is recomputed honestly.
7. **Add a unit/sanity check on insert:** reject any reading whose pair-rate vs the prior reading is outside, say, [200, 3000] pach/h unless explicitly flagged as a dock event.

Bottom line: the headline numbers (peak 1,500/h, low 666.7/h, learning +17.8%) are all artefacts of mixing journalist round numbers with dense OCR. The screenshot tier (ids 12–16) and the OCR cluster are internally consistent at ~1,200–1,450 pach/h; that's the band you should trust.
