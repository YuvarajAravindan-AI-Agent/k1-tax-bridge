#!/usr/bin/env bash
# Side-by-side: an ungoverned wrapper vs the governed bridge, same K-1, same engine.
set -uo pipefail
cd "$(dirname "$0")/.."
: "${OPENTAX_BIN:?set OPENTAX_BIN to the opentax binary}"
: "${OTD_VALIDATOR:?set OTD_VALIDATOR to otd-spec/skills/k1-otd/scripts/validate_otd.py}"
PY="${PYTHON:-python3}"
OTD="${1:-tests/fixtures/synthetic-k1.otd.yaml}"
FACTS="${2:-demo/facts.synthetic.yaml}"
rm -rf runs && mkdir -p runs

echo "== 1. Plain wrapper: copy what fits, report what the engine says"
"$PY" -m k1bridge.bridge --otd "$OTD" --facts "$FACTS" --out runs/naive --naive
echo
echo "== 2. Governed bridge, Schedule B answers withheld"
"$PY" - "$FACTS" <<'EOF' > runs/facts.no-schedule-b.yaml
import sys, yaml
f = yaml.safe_load(open(sys.argv[1])); f.pop("schedule_b_part_iii")
print(yaml.safe_dump(f, sort_keys=False))
EOF
"$PY" -m k1bridge.bridge --otd "$OTD" --facts runs/facts.no-schedule-b.yaml --out runs/blocked
echo
echo "== 3. Governed bridge, full preparer facts"
"$PY" -m k1bridge.bridge --otd "$OTD" --facts "$FACTS" --out runs/governed
echo
echo "Audit reports: runs/*/audit.md"
