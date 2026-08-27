---
name: doc-audit
description: Verify that a change is actually reflected in README.md, CLAUDE.md and docs/design_decisions.md, rather than merely accompanied by some doc edit. Use before committing any change to sql/, python/, config/, the Makefile or .claude/, and after any run that produces new measured numbers. Also use when docs are suspected stale — a limitation that reads as open but was fixed, a result table describing an older universe, a build-log row still marked uncommitted.
---

# Documentation audit

Docs in this project rot in one direction: a change lands, and the sections describing measured
results, limitations, phase status or the build log go on describing the system as it used to be.
Nothing fails. The pipeline is green, the tests pass, and the repository quietly starts lying —
which matters here more than in most projects, because the docs *are* the deliverable. The
reasoning is the thing being demonstrated.

**This audit exists because a hook cannot do it.** A hook can see that a `.md` file was touched;
it cannot tell a results table updated with current fold numbers from one still describing a
three-asset warehouse. `docs/design_decisions.md` §9 states the standard: a check that passes
unconditionally is worse than no check, because it manufactures confidence. So the whole job here
is to compare what the docs *claim* against what is *true*, by reading both.

Your output must end in one of two forms:

- **A list of stale locations** — `file:line`, what it currently claims, and what is actually
  true now.
- **"Documentation is current"**, plus which obligations below you actually checked.

Never conclude "docs updated" because a `.md` file appears in the diff. That is the exact
reasoning this skill replaces. If you did not verify a claim, say so rather than implying
coverage you do not have.

## 1. Establish what changed

```bash
git diff                 # uncommitted
git diff HEAD~1          # last commit, if auditing a completed step
git status --short
git diff --stat
```

Sort the changed files into the categories in §3 below. A change can be in several at once.

## 2. Who owns what

Route each obligation to the right file. Putting the right sentence in the wrong document is its
own failure — it makes the other document wrong by omission.

| File | Holds | Voice |
| --- | --- | --- |
| `README.md` | what the system does, how to run it, project structure, features/labels lists, phase roadmap and status, the validation-layer table | outward-facing; a reader who has never seen the repo |
| `CLAUDE.md` | conventions and constraints for *modifying* the code, commands, warehouse keys, known model status, "how to add X" workflows | instructions to whoever edits next, human or agent |
| `docs/design_decisions.md` | **why** — architecture rationale, measured results (§8), testing strategy (§9), known limitations (§10), deliberate deferrals (§11), chronological build log (§13) | argued; states what was ruled out and why |
| `docs/course_validation_and_backtesting.md` | the concepts behind Phases B–D, with ✅/⬜ status markers in §6 | teaching; written against this project's real tables |

## 3. Change type to obligation

Work through every category the diff touches. These are obligations, not suggestions — each one
exists because it has been missed before.

**A new or changed feature / label**
- `sql/00_schema.sql` column, the transform script, `sql/50_training_dataset.sql` SELECT **and**
  its NULL filter, `FEATURE_COLUMNS` in `python/train_baseline_logreg.py`
- README "Features Computed" / "Labels Generated" lists
- `design_decisions.md` §4 (feature table: what it captures, why it earns its place) or §5
- CLAUDE.md "Adding a new feature or label" if the workflow itself changed
- If the lookback exceeds 60, `EMBARGO_DAYS` must rise — and §6 and the embargo test both say so

**A new assertion or validation**
- README's two-layer validation table, `design_decisions.md` §9 table — both enumerate what each
  file guards, so both go stale together
- §9 prose if the check covers a *new class* of failure rather than another instance
- CLAUDE.md's validation paragraph
- Was it proven able to fail? §9 requires it. If the commit does not say so, that is a finding.

**Measured numbers changed** (any training run, universe change, feature change)
- `design_decisions.md` §8 — the results table, and the prose beneath it that interprets the
  numbers. Updating the table and leaving "mean 0.59" in the paragraph below is the common miss.
- README Phase B/C result lines
- CLAUDE.md "Known model status"
- Verify against reality, do not trust the diff:
  ```bash
  psql -d edmp_engine -c "SELECT count(*) AS rows, count(DISTINCT asset_id) AS assets,
    count(DISTINCT trading_date) AS dates, max(trading_date) AS latest
    FROM analytics.v_training_dataset;"
  ```
  Then confirm every row count, asset count and date in the docs matches.

**A limitation was resolved**
- `design_decisions.md` §10. **Rewrite it, do not delete it.** §10's purpose is to say what is
  still weak; a limitation that was reduced rather than eliminated must still be stated in its
  reduced form. Silently dropping an entry turns the section into marketing.
- Check whether the limitation is also asserted elsewhere (README, CLAUDE.md) and now contradicts.

**A decision was deliberately not taken**
- `design_decisions.md` §11, with the reasoning and what would change the answer. An unexplained
  absence reads as an oversight; §11 is what makes it a choice.

**A phase completed**
- README "Implementation Roadmap" status and "Planned extensions"
- `course_validation_and_backtesting.md` §6 checkbox
- CLAUDE.md's status paragraph near the top

**Files, directories, commands, or Make targets added or removed**
- README "Project Structure" tree
- CLAUDE.md command list
- Any prose that names the old path — grep for it:
  ```bash
  grep -rn "<old-name>" README.md CLAUDE.md docs/ Makefile
  ```

**Any commit at all**
- `design_decisions.md` §13 build log: date, commit hash, what landed and why it mattered.
  Rows saying *(uncommitted)* must be resolved to a real hash once pushed.

## 4. Verify, do not assume

The point of this pass is checking claims against reality. Actually run these.

```bash
# Numbers, paths and counts asserted anywhere in the docs
grep -rnE "[0-9]+,[0-9]{3} (training )?rows|[0-9]+ ETFs|ROC-AUC|0\.[0-9]{2,4}" README.md CLAUDE.md docs/*.md

# Stale markers
grep -rn "uncommitted\|not started\|planned\|TODO\|coming soon" README.md CLAUDE.md docs/*.md

# Referenced files that no longer exist
grep -rnoE "(sql|python|tests|config|docs)/[a-z_0-9/]+\.(sql|py|csv|md)" README.md CLAUDE.md docs/*.md \
  | awk -F: '{print $NF}' | sort -u | while read -r f; do [ -e "$f" ] || echo "MISSING: $f"; done
```

Cross-document contradiction is its own category and the easiest to miss: the same fact is often
stated in two files, and a change updates one. Model status appears in both CLAUDE.md and §8. The
validation layers appear in both README and §9. The universe size appears in §8, §10 and the
README structure tree.

## 5. What is not a finding

Avoid false positives; they train people to skip this.

- **Historical figures kept on purpose.** §8 retains the 3-ETF numbers beside the current ones
  because the comparison is itself a result. Superseded numbers presented *as* history are
  correct; superseded numbers presented as current are the bug.
- **The build log describing the past.** §13 rows are meant to be historical.
- **Deliberately unimplemented things.** `analytics.backtest_results` being unused is documented
  and true until Phase D lands. The missing-file check in §4 flags
  `python/backtest_from_predictions.py` for the same reason: the roadmap names it as future work,
  so its absence is the documentation being accurate. Only treat a missing file as a finding when
  the prose describes it as something that already exists.
- **Comments in code.** Those belong to `comment_reminder.sh` and review, not here.

## 6. Report

List each stale location as `file:line`, what it claims, what is true. Rank by how misleading it
is: a wrong measured number outranks a missing build-log row, because someone could act on the
first. If a change is genuinely internal and creates no documentation obligation, say that
explicitly and name the categories in §3 you ruled out — that is a real result, not a shrug.
