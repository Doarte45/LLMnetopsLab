#!/usr/bin/env bash
# Run the v2 agent batch unattended, one run after another (planning/agent-runs.md).
#
# Run in a WSL terminal and leave it open (closing every WSL window shuts WSL down):
#     bash /mnt/c/Users/andre/Projects/LLMnetopsLab/v2/scripts/run_batch.sh          # up to run 50
#     bash /mnt/c/Users/andre/Projects/LLMnetopsLab/v2/scripts/run_batch.sh 10       # up to run 10
#
# Starts at the first run number that doesn't exist yet. Stops when:
#   - a run is invalid (usage limit, API error, timeout): its files are removed so the same
#     number is redone next time. Start the script again after the limit resets.
#   - the lab can't be reset or broken (exit 1): nothing is removed; check the log.
# One summary line per run goes to ~/claudenetops/logs/batch.log.

set -u
LAST=${1:-50}
LABEL=opus
REPO_V2=/mnt/c/Users/andre/Projects/LLMnetopsLab/v2
ROOT=~/claudenetops
LOGS=$ROOT/logs
mkdir -p "$LOGS" "$ROOT/$LABEL"

next_run() {
    local n=1
    while [ -e "$ROOT/$LABEL/$(printf run_%03d "$n")" ]; do n=$((n + 1)); done
    echo "$n"
}

remove_run() {
    local name=$1
    rm -rf "$ROOT/$LABEL/$name" "$ROOT/.staging/$LABEL/$name" "$REPO_V2/results/runs/$LABEL/$name" \
           ~/.claude/projects/-home-doarte-claudenetops-$LABEL-${name//_/-}
}

summary() {
    python3 - "$ROOT/$LABEL/$1/results/meta.json" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
r, s = m["result"], m["score"]
flags = []
if m.get("answer_key_markers"): flags.append(f"answer-key markers {m['answer_key_markers']}")
if m.get("config_drift_after_agent"): flags.append("config drift")
if m.get("compactions"): flags.append(f"{m['compactions']} compactions")
print(f"{m['run']:03d}  fixed {s['fixed']}/12  workarounds {s['workarounds']}  collateral {s['collateral']}  "
      f"{m['wall_seconds'] / 60:.1f} min  ${r.get('total_cost_usd') or 0:.2f}  turns {r.get('num_turns')}"
      + (f"  CHECK: {'; '.join(flags)}" if flags else ""))
EOF
}

cd "$REPO_V2" || exit 1
while :; do
    n=$(next_run)
    if [ "$n" -gt "$LAST" ]; then
        echo "=== all runs up to $LAST done" | tee -a "$LOGS/batch.log"
        break
    fi
    name=$(printf run_%03d "$n")
    echo "=== $(date '+%F %T') starting $name" | tee -a "$LOGS/batch.log"
    python3 -u scripts/run_agent.py "$n" --label "$LABEL" 2>&1 | tee "$LOGS/$name.log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then
        summary "$name" | tee -a "$LOGS/batch.log"
    elif [ "$rc" -eq 2 ]; then
        reason=$(grep -o "You've hit your[^\"]*" "$ROOT/$LABEL/$name/results/stream.ndjson" 2>/dev/null | tail -1)
        echo "=== $(date '+%F %T') $name invalid (${reason:-see $LOGS/$name.log}); removed, stopping" \
            | tee -a "$LOGS/batch.log"
        mv "$LOGS/$name.log" "$LOGS/$name.invalid.log"
        remove_run "$name"
        break
    else
        echo "=== $(date '+%F %T') $name failed with exit $rc (lab problem?); see $LOGS/$name.log, stopping" \
            | tee -a "$LOGS/batch.log"
        rm -rf "$ROOT/.staging/$LABEL/$name"
        break
    fi
done
