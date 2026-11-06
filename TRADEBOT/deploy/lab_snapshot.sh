#!/usr/bin/env bash
# Opportunity Lab evidence snapshot (used by .github/workflows/opportunity-lab.yml). Writes status, report and the log
# tail to lab_evidence/, then commits it to the current branch so it can be read without downloading artifacts.
# Never fails the job. Usage: lab_snapshot.sh <label>
LABEL="$1"
D="${LAB_DATA_DIR:-data_lab}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p lab_evidence
OUT="lab_evidence/${TS}_${LABEL}.txt"
{
  echo "=== OPPORTUNITY LAB SNAPSHOT ${LABEL}  $(date -u +%FT%TZ)  run ${GITHUB_RUN_ID:-local}  (PAPER, SIMULATED FUNDS) ==="
  PID="$(cat "$D/lab.pid" 2>/dev/null)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then echo "lab: RUNNING (pid $PID)"; else echo "lab: NOT RUNNING"; fi
  echo; python -m opportunity_lab status 2>&1
  echo; python -m opportunity_lab report 2>&1
  echo; echo "--- log tail"; tail -40 "$D/run_stdout.log" 2>/dev/null
} > "$OUT" 2>&1
cp "$OUT" lab_evidence/latest.txt
cat "$OUT"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add lab_evidence
git commit -q -m "Lab evidence: ${LABEL} snapshot of run ${GITHUB_RUN_ID:-local}" || exit 0
for i in 1 2 3 4; do
  git pull -q --rebase origin "${GITHUB_REF_NAME}" && git push -q origin HEAD:"${GITHUB_REF_NAME}" && exit 0
  sleep $((i * 5))
done
echo "could not push the snapshot (it is still in the job log above)"
exit 0
