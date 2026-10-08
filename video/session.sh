#!/usr/bin/env bash
# Plays one demo session: types each command at human speed, then really runs it.
export PATH=/opt/opentax-test/video:$PATH PS1='$ '
cd /opt/opentax-test/bridge
type_run() {
  printf '\033[1;32m$\033[0m '
  for ((i = 0; i < ${#1}; i++)); do printf '%s' "${1:i:1}"; sleep 0.045; done
  sleep 0.4; printf '\n'; eval "$1"; sleep "${2:-2.5}"
}
case $1 in
  naive)    type_run "k1-bridge --naive" 3 ;;
  blocked)  type_run "k1-bridge --facts=no-schedule-b.yaml" 3.5 ;;
  governed) type_run "k1-bridge" 1.5
            type_run "grep '| ok |' runs/governed/audit.md" 3
            type_run "ledger" 3.5 ;;
  infra)    type_run "uptime -p; nproc; free -h | awk '/Mem/{print \$2, \"RAM\"}'" 2
            type_run "ls /opt/opentax-test/bridge" 1.5
            type_run "ls -lh ../opentax/opentax | awk '{print \$5, \$9}'" 1.5
            type_run "../venv/bin/python -m pytest -q tests 2>&1 | tail -2" 3 ;;
esac
