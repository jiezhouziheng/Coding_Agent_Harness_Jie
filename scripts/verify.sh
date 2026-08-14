#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
python -m pytest
python -m ruff check .
python -m mypy --strict src
python -m build --no-isolation
