# Course: Calibration and Event Features

Picks up where `course_validation_and_backtesting.md` stops. That one ended with a working
backtest; this covers everything between there and a working event pipeline — the rest of
**Phase D** (calibration) and all of **Phase E** (events). Written against this project's actual
tables and columns.

---

## 1. What the backtest just taught us

Phase D produced a clean negative result: no strategy beats holding the market.

| Strategy | Sharpe | Total | Max DD | vs. benchmark |
| --- | --- | --- | --- | --- |
| `always_long` | 1.48 | 8.3% | −7.2% | — |
| `direction_threshold` | 0.66 | 2.1% | −2.5% | −0.82 |
| `large_move_filter` | 1.22 | 5.7% | −6.9% | −0.26 |

Two lessons matter for everything below.

**A real signal is not automatically a tradeable one.** `y_large_move_next` genuinely predicts
at ~0.57 AUC — that survived walk-forward validation with an embargo. It still lost money,
because the label is *unsigned*: it says "a big move is coming" without saying which way, so
stepping aside forgoes as much upside as downside. Nothing short of the backtest revealed this.

**Therefore: judge every future addition, events included, on the backtest, not the AUC.** The
question to keep asking is not "did AUC improve" but "did the return series improve against
`always_long`, net of costs".

---

## 2. Calibration: what it is

A probability is **calibrated** if it means what it says: of all the days the model called 0.60,
about 60% should actually resolve up. This is a different question from ranking.

- **Discrimination** (what ROC-AUC measures): does the model rank positives above negatives?
- **Calibration**: are the numbers themselves right?

You can have either without the other. A model that outputs `p = 0.5 + 0.001 × (true rank)` ranks
perfectly (AUC 1.0) and is hopelessly uncalibrated. A model that always outputs the base rate is
perfectly calibrated and has zero discrimination (AUC 0.5).

`train_baseline_logreg.py` already prints the bucket table showing this project is miscalibrated
where it matters — the 0.55–0.60 bucket resolves upward roughly 33–45% of the time, not ~57%. It
is **overconfident exactly in the range that would trigger a trade**.

### Why it matters here specifically

For a threshold rule (`p_up > 0.55` → long), calibration barely matters: only the *ordering*
around the threshold does. That is why Phase D works at all without it.

It becomes essential the moment position size depends on the probability — sizing by
`p_up − 0.5`, or any expected-value calculation. Then an overconfident 0.60 that really means 0.45
doesn't just size wrong, it sizes *biggest where the model is most wrong*. Kelly-style sizing is
the extreme case: it is exquisitely sensitive to probability error and will happily bankrupt a
correctly-ranked, badly-calibrated model.

---

## 3. Calibration: the two methods

Both learn a mapping from raw score to calibrated probability, `f: [0,1] → [0,1]`.

**Platt scaling** fits a one-dimensional logistic regression on the model's scores:
`p_calibrated = sigmoid(a × score + b)`. Two parameters. Works with a few hundred rows. It assumes
the distortion is sigmoid-shaped, which is often true for margin-based models and is a real
restriction otherwise — it cannot fix a non-monotone distortion.

**Isotonic regression** fits an arbitrary non-decreasing step function. Non-parametric, so it can
correct any monotone distortion — and correspondingly hungry for data and prone to overfitting,
producing wide flat steps when starved. Rule of thumb: below ~1,000 calibration rows, prefer
Platt.

This project's folds have thousands of test rows but calibration must be fitted on *training*
data (next section), where there is more. Either is defensible; Platt is the safer default given
the folds are only ~5 blocks and the miscalibration observed is monotone.

### Where you must fit it — the part that goes wrong

**A calibrator fitted on the same rows the model was trained on will look excellent and be
useless.** The model is overconfident *out of sample*, but on its own training rows it is roughly
right, so the calibrator learns "barely change anything" and the test-set overconfidence survives
untouched.

**A calibrator fitted on test rows is leakage**, full stop — it uses outcomes from the period
being evaluated, and every metric downstream becomes a lie.

The correct pattern, inside each fold:

```
fold training rows  ─┬─  model-fitting slice     → fit LogisticRegression
                     └─  calibration slice       → fit the calibrator on the model's
                                                    predictions for rows it never saw
fold test rows           → never touched by either; used only for evaluation
```

Because this is time series, the calibration slice should be the **latest** part of the training
window, not a random sample — it should resemble the test period as closely as possible, and a
random split would put future rows in the model-fitting slice.

This is the same rule the `StandardScaler` already follows: **the calibrator is part of the model**,
so it is refit inside every fold and never sees test data. `sklearn.calibration.CalibratedClassifierCV`
implements the whole pattern, but its default `cv` does a random split, which is wrong here — pass
an explicit time-ordered split.

### Measuring whether it worked

AUC cannot tell you: any strictly monotone recalibration leaves ranking, and therefore AUC,
exactly unchanged. That is a useful property — it means **calibration cannot manufacture signal**,
only state it honestly. Use instead:

- **Brier score** — mean squared error of the probabilities, `mean((p − y)²)`. Lower is better. It
  is a *proper scoring rule*: it is minimised only by reporting your true beliefs, so it cannot be
  gamed by hedging toward the base rate.
- **The bucket table already printed** — the direct reliability check, read as "does the realised
  column now track the predicted column".

Brier decomposes into *calibration* + *refinement*, which is exactly the split above: a model can
improve its Brier score purely by getting honest, without becoming any more informative.

### What to calibrate here, and what not to

**Do not calibrate the direction model.** `y_up_next_day` has no signal (AUC ~0.51). Calibration
makes a model's probabilities honest; it cannot create information. A perfectly calibrated
no-signal model correctly outputs the base rate every day, which is true, useless, and untradeable.

**Do calibrate the large-move model.** It has a real edge (~0.57), so making `p_large_move`
trustworthy is a prerequisite for ever sizing on it — and for the `large_move_filter` threshold to
mean something interpretable rather than being an arbitrary constant.

---

## 4. Why events are the next move rather than a better model

Phase D established that the current feature set does not produce a tradeable edge, and §11 of
`design_decisions.md` explains why the answer is not a bigger model: more capacity cannot
manufacture signal that is not in the features. The features are six price-derived numbers, and
the direction result says prices alone do not predict next-day direction — which is what an
efficient market is supposed to look like.

Events are the first genuinely *new information source* in the project. That is a different kind
of change from swapping logistic regression for XGBoost, and it is why the roadmap orders them
this way.

Realistic expectations, stated in advance so the result can be judged honestly: macro event
effects on broad ETFs are real but small and concentrated on a handful of days a year. Twelve CPI
releases and eight FOMC meetings is **20 event days out of ~250**. Even a large effect on those
days moves an annual metric only slightly, and the honest hope is a modest improvement, most
visible if you slice performance to event days specifically.

---

## 5. Event studies: the underlying idea

The classical event-study method asks: does an abnormal return cluster around an event?

1. Define an **estimation window** before the event and fit a normal-return model (often the
   market model: `r_asset = α + β·r_market + ε`).
2. Define an **event window** around it (say −5 to +5 days).
3. **Abnormal return** = actual − predicted-by-the-normal-model.
4. Sum across the window into **CAR** (cumulative abnormal return), average across events, test
   whether it differs from zero.

This project is not doing that, and it is worth being clear why. An event study is a *research
method* for testing whether an effect exists in a sample. This pipeline is a *prediction system*:
it needs a feature available at time `t` that helps forecast `t+1`. The concepts transfer — event
windows, alignment, normalising by what is expected — but the output is columns in
`analytics.features_daily`, not a t-statistic.

The one place a real event study is still worth running: as a **sanity check** before building
features. If CPI days show no abnormal return dispersion at all in your data, event features will
not help, and it is cheaper to learn that from a groupby than from a full feature pipeline.

---

## 6. Effective trading date alignment — where leakage will come from

Events happen at timestamps. Markets trade in sessions. Mapping one to the other is the single
most error-prone part of Phase E, and errors here are **lookahead**, not noise.

The rule to encode: **`event_date` is the first trading session whose close reflects the event.**

| Event | Timestamp (ET) | Session that reflects it |
| --- | --- | --- |
| CPI release | 08:30, before the open | **same day** |
| FOMC statement | 14:00, market open | **same day** |
| FOMC minutes | 14:00 | same day |
| Earnings, after close | 16:05+ | **next** session |
| Anything on a weekend/holiday | — | **next** session |

Getting this backwards by one day is a disaster in either direction:

- **Too early** (event assigned to the session before it happened): the feature knows the future.
  Silent leakage; the model will happily use it and every metric becomes a lie.
- **Too late**: no leakage, but the signal is smeared onto a day the market had already priced,
  and a real effect gets diluted into nothing.

In this project the computation belongs in `sql/20_staging_transform.sql` step 3, which currently
does `COALESCE(e.event_date, (e.event_ts AT TIME ZONE 'UTC')::date)` — a placeholder that is
wrong for after-hours events and for anything landing on a non-trading day. The trading calendar
is available as `SELECT DISTINCT trading_date FROM staging.prices_daily`, and the correct logic is
"the smallest `trading_date >= ` the event's effective date".

**Timezone trap**: `event_ts` is `timestamptz`. Casting to date in UTC silently shifts evening ET
events to the next calendar day, because 20:00 ET is 01:00 UTC tomorrow. Convert to
`America/New_York` before taking the date, exactly as `prepare_data.py` already does for the
settled-session cutoff.

---

## 7. Surprise, not level

Markets price expectations. A CPI print of 3.2% is not news if everyone forecast 3.2% — the
*unexpected component* is what moves prices.

```
surprise      = actual − forecast
surprise_pct  = (actual − forecast) / |forecast|
```

Both already exist in `staging.events`, computed in `20_staging_transform.sql`.

`surprise_pct` normalises across series of different scale, but it degrades badly when `forecast`
is near zero — a forecast of 0.1% and an actual of 0.2% gives a 100% surprise that means little.
The more robust normalisation is the **standardised surprise**: divide the raw surprise by the
standard deviation of that series' historical surprises. Then "a 2σ CPI surprise" is comparable to
"a 2σ payrolls surprise", and it is scale-free without the near-zero pathology.

Compute that standard deviation on a **trailing** window of past surprises only. Using the
full-sample standard deviation is a subtle lookahead: it encodes how surprising this release was
relative to a distribution that includes the future.

**Where forecasts come from is the practical bottleneck.** Actuals are freely available (BLS, the
Fed); consensus forecasts generally are not, and historical consensus is often paywalled. Options:
carry `forecast` as NULL and rely on anticipation features only; use the previous release as a
naive forecast (a weak but honest proxy); or hand-curate a partial history. The schema already
allows NULL and the surprise expressions already guard against it.

---

## 8. The point-in-time rule, extended to events

This is the section to reread before writing any event feature. Events introduce **two distinct
classes** of information, and conflating them is how leakage enters.

**Class 1 — schedule, known far in advance.** The CPI and FOMC calendars are published months
ahead. So `is_cpi_day`, `is_fomc_day`, `days_until_cpi`, `days_since_fomc` are all knowable at any
time `t`, including for future dates. They are safe anywhere, and they are the *only* event
features available for a date whose event has not yet occurred.

**Class 2 — outcome, known only at release.** `actual`, `surprise`, `surprise_pct` do not exist
until the release happens. A feature carrying them is valid only from `event_date` onward. The
natural construction — "the surprise of the most recent event with `event_date <= t`" — is safe;
anything that reaches the *next* event's surprise is not.

The trap is mechanical and easy to hit: joining events to the daily panel and forward-filling in
the wrong direction, or a window frame that says `FOLLOWING`. A CPI surprise attached to the three
days *before* the release looks like a plausible "pre-event" feature and is pure lookahead.

**The revision trap.** Macro statistics are revised. The CPI printed on release day is not the CPI
in the database a year later, and GDP is revised repeatedly. Building features from *revised*
values gives the model numbers no one had at the time — the exact analogue of the `adj_close`
restatement issue in `design_decisions.md` §10, and more dangerous, because the restatement is not
a uniform rescaling that cancels in a ratio. **Always store the initial print.** FRED's ALFRED
provides genuine real-time vintages if you need to reconstruct history.

**Applying the project's structural rule.** Features live in `analytics.features_daily`, labels in
`analytics.labels_daily`. Event features are features: they must be computable from data at or
before `t`. If a proposed event feature cannot be written with a trailing-only frame, it does not
belong in the features table.

---

## 9. Mapping events to assets

`staging.event_asset_map` exists so an event can affect several assets with different weights.
Today it is a `CROSS JOIN` at weight 1.0 — every event to every asset.

**For macro events, that is not a placeholder — it is correct.** A CPI print is information about
the whole economy; every ETF in this universe reprices against it. The cross-join becomes wrong
only when event types arrive that are *not* market-wide:

- **Earnings** → the issuing ticker only, and for an ETF, the constituents weighted by holding.
- **Sector-specific** (an OPEC decision, an FDA ruling) → a subset, plausibly weighted.

The `weight` column is what makes this expressible. A useful test of whether your mapping is real:
if every event maps to every asset at weight 1.0, the map contributes nothing a simple date join
would not, and it can wait.

---

## 10. Designing the features

A workable first set, grouped by the two classes above:

**Anticipation** (Class 1 — always safe)
- `is_cpi_day`, `is_fomc_day` — indicator for the session that absorbs the event
- `days_until_cpi`, `days_until_fomc` — trading days ahead, capped (`LEAST(n, 30)`) so a quiet
  stretch doesn't produce a huge magnitude the linear model over-weights
- `days_since_cpi`, `days_since_fomc` — decay since the last release

**Reaction** (Class 2 — valid from `event_date` onward)
- `last_cpi_surprise` (standardised), and `ABS(last_cpi_surprise)` — direction and magnitude
  behave differently, and magnitude is the better bet given what §1 taught about unsigned signals

**Interaction**
- `mom_20d × is_fomc_day` — lets the model say "momentum behaves differently around events",
  which a purely additive model cannot express. This is where linear models start to strain, and
  the point at which the roadmap's Phase F (gradient-boosted trees) becomes a real question rather
  than a reflex.

A note on the embargo: these names carry no `_<N>d` lookback suffix, so
`test_embargo_covers_the_longest_feature_lookback` will correctly ignore them — they are not
rolling price windows. If you add something like `cpi_surprise_mean_90d`, the embargo must rise to
match, and that test will tell you.

---

## 11. Multiple comparisons — the discipline that makes the result believable

You are about to add many candidate features and test them against the same label. **Some will
look significant by chance.** At the conventional 5% level, testing 20 useless features yields one
"significant" result on average — and it will be the one you remember.

This is the same trap `design_decisions.md` §11 refuses for hyperparameters ("iterating against
the same five folds until something clears 0.55 would be fitting the validation set"), and the
same one that made `LARGE_MOVE_EXIT` worth documenting so carefully in Phase D.

Three defences, in increasing strength:

1. **Bonferroni-style correction** — divide the threshold by the number of tests. Simple, very
   conservative, and it costs real power when features are correlated (which these are).
2. **A held-out event-feature set** — decide the feature set, then evaluate once on folds never
   used during design.
3. **Pre-registration** — write down the comparison before running it, then run it once.

This project's chosen approach is (3), and it is already scaffolded: `train_baseline_logreg.py`
gains `--feature-set {baseline,events}`, `model_runs.feature_set_version` records which, and the
comparison is *one* paired run across identical folds. Not "try five feature sets and report the
best" — that number would mean nothing.

---

## 12. How to judge whether events helped

Run the paired comparison, then apply Phase D's lesson.

**Per-fold deltas, not pooled averages.** Five folds give five paired differences. A mean
improvement of +0.02 built from `[+0.15, −0.05, +0.01, −0.02, +0.01]` is one lucky regime, not an
edge. Consistency across folds matters more than the mean.

**Then the backtest, which is the real bar.** AUC improving is necessary, not sufficient — that is
exactly what `large_move_filter` demonstrated. Rerun `make backtest` on the event model's
predictions and compare against `always_long` and against the baseline model's own backtest. If
the return series does not improve net of costs, events did not help, however good the AUC looks.

**Slice to event days.** Twenty event days in 250 means a real effect is invisible in an annual
aggregate. Compare performance *on and around event days* specifically — that is where the signal
would live if it exists, and averaging it into a year of quiet sessions is how you would miss it.

**A negative result is a result.** "Macro surprises do not improve next-day prediction for broad
ETFs" is a legitimate, publishable-quality finding — and given efficient markets price scheduled
releases within seconds, it is arguably the *expected* one. The project's value is that it can
tell the difference, honestly, which is what the last five phases were building.

---

## 13. Reading order for implementation

1. **Calibrate the large-move model** (§2–3): fit Platt inside each fold on a trailing slice of
   that fold's training rows, store the calibrated `p_large_move`, report Brier before and after.
   Leave the direction model uncalibrated and say why. ⬜ not started
2. **Curate the macro calendar** (§7, §12): `config/assets.csv`-style tracked CSV for CPI and FOMC,
   initial prints only, including scheduled future dates so anticipation features work. ⬜ not started
3. **Fix effective-date alignment** (§6): replace the `COALESCE(...::date)` placeholder in
   `20_staging_transform.sql` with the trading-calendar rule, in market time. ⬜ not started
4. **Replace the cross-join mapping** (§9) with an explicit rule — for CPI/FOMC that is still "all
   assets, weight 1.0", but stated as a decision rather than a default. ⬜ not started
5. **Add the features** (§10), anticipation first since they are unconditionally safe and need no
   forecast data, outcome features second. ⬜ not started
6. **Assert the new invariants** (§6, §8): `event_date` is always a real trading session at or
   after the release; `is_cpi_day` agrees with the calendar; surprise re-derives from
   `actual − forecast`. Each proven able to fail. ⬜ not started
7. **Run the pre-registered comparison once** (§11–12), report per-fold deltas and the backtest
   delta, and write down whatever it says. ⬜ not started
