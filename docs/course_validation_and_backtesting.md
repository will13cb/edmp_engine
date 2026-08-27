# Course: Validating and Backtesting the Baseline Model

Covers the concepts needed for README **Phase B** (walk-forward validation + honest
evaluation) and **Phase D** (backtesting). Written against this project's actual tables and
columns so it's concrete, not abstract.

---

## 1. Why a single train/test split isn't enough

Phase A trained on one split: everything `<= train_end`, tested on everything after. That
gave one number (ROC-AUC 0.49 / 0.60). The problem: **one split is one sample**. Markets
have regimes — a model that looks fine in a calm 2019 test window can fall apart in a
volatile 2022 window. A single split can't tell you which one you got.

There's also a leakage risk specific to this project's features. `vol_20d`, `mom_20d`, and
`drawdown_60d` are rolling windows — the row for 2024-11-25 depends on the 20/60 days
before it. If `train_end` falls in the middle of that window, a handful of rows near the
boundary have features computed from a mix of train-side and test-side prices. Not full
label leakage, but it means adjacent rows aren't independent, which is what random k-fold
cross-validation assumes. That's exactly why the roadmap says "never random."

---

## 2. Walk-forward validation

Instead of one split, slide a window across the timeline and repeat the fit/evaluate cycle
many times:

```
Fold 1: train [2018 ----->  2021]          test [2021    -> 2021-Q2]
Fold 2: train [2018 ----------> 2021-Q2]   test [2021-Q2 -> 2021-Q3]
Fold 3: train [2018 --------------> 2021-Q3] test [2021-Q3 -> 2021-Q4]
...
```

Two flavors:

- **Expanding window** — train always starts at the same point and grows (fold 2's train set
  contains all of fold 1's). Uses all available history each time.
- **Rolling window** — train set is a fixed-length window that slides forward (e.g. always
  the trailing 3 years). Better if you suspect old regimes stop being relevant.

Either way, you end up with N separate ROC-AUC numbers instead of one. **That distribution
is the real result** — a mean of 0.55 with folds ranging 0.48–0.62 tells a very different
story than a mean of 0.55 with folds all sitting at 0.54–0.56.

### Purging / embargo

To fix the rolling-window leakage from Section 1: drop a buffer of rows around each fold's
train/test boundary — enough rows that no feature window in the kept test data overlaps a
training row. For this project, an embargo of **~60 trading days** (the longest lookback,
`drawdown_60d`) around each cut is a defensible starting point. It costs a little data at
each boundary in exchange for a clean guarantee that no test-set feature was computed from
a training-period price.

In code, concretely: this replaces the single `train_end` cutoff in
`train_baseline_logreg.py` with a loop that generates several
`(train_end, test_start, test_end)` triples, refits the model in each, and collects one AUC
per fold instead of printing one.

---

## 3. Honest evaluation

Once you have real out-of-sample predictions (from walk-forward, not one split), the
question changes from "does it work" to "how well, and is that believable."

### Confusion matrix at your threshold

`train_baseline_logreg.py` already picks a threshold implicitly via
`LONG_THRESHOLD = 0.55`. A confusion matrix at that threshold shows the actual trade-off:

|                     | Predicted up   | Predicted not-up |
| ------------------- | -------------- | ---------------- |
| **Actually up**     | True Positive  | False Negative   |
| **Actually not-up** | False Positive | True Negative    |

AUC summarizes *ranking* quality across every threshold; the confusion matrix tells you
what happens at the one threshold you're actually going to trade on. A model can have a
decent AUC and still be useless at 0.55 if all the separation happens at thresholds you'd
never use.

### Calibration

A probability of 0.60 should mean "resolves up about 60% of the time," not just "higher
than 0.50 cases." To check: bucket test predictions by `p_up` (e.g. 0.45–0.50, 0.50–0.55,
0.55–0.60…), and for each bucket compute the actual fraction of `y_up_next_day == True`.
Plot predicted-probability bucket vs. realized frequency — a perfectly calibrated model
sits on the diagonal.

This matters because `p_up` isn't just used for direction — Phase D turns it into position
size (`p_up - 0.5`), so if 0.60 actually resolves up only 51% of the time, every downstream
sizing decision is wrong even though the ranking (AUC) looked fine.

### Naive baseline comparison

Before trusting any AUC or accuracy number, compare against a model that knows nothing:
predict the majority class every time (or the historical base rate as a constant
probability). If ~53% of days in the dataset are "up," a naive model that always predicts
"up" gets 53% accuracy for free. Your logistic regression needs to clear *that* bar, not
just clear 50%. This is also a sanity check on AUC — a naive constant-probability model
always scores exactly 0.5 AUC, so "beats 0.5" and "beats the naive baseline" are actually
the same statement for AUC specifically, but not for accuracy, which is why both checks
matter.

---

## 4. From predictions to a backtest

`analytics.model_predictions` already has `p_up`, `p_large_move`, and `signal` (1/0/-1) per
`(asset_id, trading_date)`. Backtesting turns that into a return series.

### Signal → position

Two options, both mentioned in the roadmap:

- **Threshold rule** (what `signal` already encodes): `position = +1` if `p_up > 0.55`,
  `-1` if `p_up < 0.45`, else `0`.
- **Proportional sizing**: `position = p_up - 0.5` (scaled), so a 0.51 and a 0.65
  prediction don't get the same-sized bet. Requires calibration (Section 3) to be
  meaningful — sizing off an uncalibrated probability just encodes the model's
  overconfidence into your position size.

### Daily strategy return

```
strategy_return_t = position_t * ret_fwd_1d_t
```

`ret_fwd_1d` already exists in `analytics.labels_daily` — it's the realized forward return
the position is a bet on.

### Costs and slippage

Real trading isn't frictionless. Two costs to subtract before computing anything else:

- **Transaction cost**: a fixed or proportional fee per trade (e.g. 1–5 bps of notional for
  a liquid ETF like SPY). Only charged when `signal` *changes* from the prior day — holding
  a position costs nothing extra, flipping it does.
- **Slippage**: the gap between the price you modeled (close) and the price you'd actually
  fill at. A simple proxy: assume you always fill slightly worse than close (e.g.
  `close * (1 ± 0.0005)` depending on direction).

```
net_return_t = strategy_return_t - transaction_cost_t - slippage_t
```

Skipping this step is the single most common way a backtest lies to you — a strategy that
flips position every day can look great gross and be a loser net of costs.

### Risk metrics

**Sharpe ratio** — return per unit of risk taken:

```
Sharpe_annualized = sqrt(252) * mean(net_return) / std(net_return)
```

`sqrt(252)` annualizes from daily to yearly (≈252 trading days/year). Rough scale: 0 = no
edge, 0.5 = decent, 1.0 = strong, 2.0+ = very strong (or check for a bug/overfit).

**Max drawdown** — worst peak-to-trough decline in cumulative return, your risk-of-ruin
number:

```
cum_return_t   = cumulative product of (1 + net_return) up to t
running_peak_t = max(cum_return_0..t)
drawdown_t     = (cum_return_t - running_peak_t) / running_peak_t
max_drawdown   = min(drawdown_t)   # most negative value
```

Sharpe can look fine while max drawdown is brutal — always report both.

**Hit rate vs. expectancy** — hit rate alone (% of profitable days) is misleading if wins
and losses aren't the same size. Expectancy accounts for that:

```
expectancy = (hit_rate * avg_win) - ((1 - hit_rate) * avg_loss)
```

A 40% hit rate with wins 3x the size of losses is a good strategy; a 60% hit rate with
losses 3x the size of wins is not. Expectancy is the number that actually tells you whether
the strategy makes money on average.

---

## 5. Where this lands in the schema

- Walk-forward folds each produce their own row in `analytics.model_runs` (different
  `train_start`/`train_end`/`test_start`/`test_end`), with predictions in
  `analytics.model_predictions` keyed by `model_run_id` — the schema already supports this
  without changes.
- Backtest output (`net_return`, `cum_return`, `drawdown`, and the summary metrics) goes
  into `analytics.backtest_results`, keyed by `backtest_run_id`. The summary metrics (Sharpe, max
  drawdown, hit rate, expectancy) are one scalar per run rather than a daily series, so they live on
  `analytics.backtest_runs` alongside the assumptions that produced them — the strategy and the
  cost charged.

---

## 6. Reading order for implementation

1. Rewrite the train/test split in `train_baseline_logreg.py` as a loop over multiple
   `(train_end, test_start, test_end)` folds (Section 2). ✅ done
2. Add the confusion matrix, calibration bucket table, and naive-baseline comparison as
   print output per fold (Section 3) — this is what "evaluate honestly" means in
   practice. ✅ done
3. Write `backtest_from_predictions.py`: read `model_predictions` + `ret_fwd_1d`, apply
   costs/slippage, compute Sharpe/drawdown/expectancy, write to `backtest_results`
   (Section 4). ⬜ not started
