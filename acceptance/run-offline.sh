#!/usr/bin/env sh
set -eu

export PVI_NETWORK_DISABLED=1
export PYTHONPATH=src

python -m ruff check src tests scripts
python scripts/validate_data.py
python scripts/validate_openapi.py
python scripts/check_forbidden_fields.py
python scripts/validate_matrix_report.py
python -m pytest

echo "PVI Acceptance A offline suite passed."
