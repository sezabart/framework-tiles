#!/usr/bin/env bash
# Start the vitals tile. Any flags are passed to host/oled_vitals.py.
#   ./run.sh                 live vitals from this machine
#   ./run.sh --simulate      fake local-AI workload (nice for demos)
#   ./run.sh --gif demo.gif  render the demo animation without hardware
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv"
  python3 -m venv --system-site-packages .venv
  ./.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
fi

exec ./.venv/bin/python -u host/oled_vitals.py "$@"
