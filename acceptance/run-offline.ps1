$ErrorActionPreference = "Stop"
$env:PVI_NETWORK_DISABLED = "1"
$env:PYTHONPATH = "src"

python -m ruff check src tests scripts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python scripts/validate_data.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python scripts/validate_openapi.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python scripts/check_forbidden_fields.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python scripts/validate_matrix_report.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Output "PVI Acceptance A offline suite passed."
