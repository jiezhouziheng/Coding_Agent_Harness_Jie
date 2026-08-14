$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python -m mypy --strict src
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    python -m build --no-isolation
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
