#!/usr/bin/env bash
# Runs the repo's tests. Used by CI (.github/workflows/test.yml) and the review loop.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --quiet --no-project --with pyyaml python -m unittest discover -s tests -v
