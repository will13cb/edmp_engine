#!/usr/bin/env bash
#
# PostToolUse hook: remind Claude to justify non-obvious pipeline code.
#
# Why this exists: in this repo a wrong comment is cheap but a wrong *assumption*
# is expensive. Temporal-leakage bugs are silent - no crash, no row-count change,
# just a quietly inflated ROC-AUC. Forcing the reasoning into a comment at edit
# time is the cheapest place to catch a bad assumption about what a given
# expression is allowed to see at time t.
#
# Scope: fires only for .py and .sql files. Everything else exits silently.
#
# Contract: advisory only. Always exits 0 so it can never fail an edit - a hook
# that breaks the edit loop over a style concern is a worse trade than an
# occasional missing comment.
#
# Uses /usr/bin/jq (system binary) rather than the project venv, since hooks run
# outside the activated environment and must work on a fresh clone.

# Deliberately no `set -e`: every failure path below should still exit 0.
set -u

JQ=/usr/bin/jq

# No jq, no reminder. Not worth failing over.
[ -x "$JQ" ] || exit 0

payload=$(cat)

file_path=$(printf '%s' "$payload" | "$JQ" -r '.tool_input.file_path // empty' 2>/dev/null)

case "$file_path" in
  *.py | *.sql) ;;
  *) exit 0 ;;
esac

read -r -d '' reminder <<'EOF'
Comment check for this edit:

- Comment the WHY, not the what. Skip anything self-evident from the code.
- If this touched features, labels, window frames, or the train/test split,
  state the point-in-time reasoning explicitly: what data is this expression
  allowed to see at time t, and why does it not reach past t?
- Features may only use data at or before t (trailing frames). Labels are the
  only place allowed to look forward. See "Point-in-time correctness" in
  CLAUDE.md.
- If you changed a rolling window's length, check EMBARGO_DAYS in
  python/train_baseline_logreg.py still matches the longest lookback.
EOF

# jq -n builds the JSON so the reminder text is escaped correctly.
"$JQ" -n --arg ctx "$reminder" \
  '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $ctx}}'

exit 0
