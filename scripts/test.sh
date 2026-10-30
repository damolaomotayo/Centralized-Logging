#!/usr/bin/env bash
# Runs all unit tests (no Docker needed): app logic, configs, repo sanity, scripts with stubs.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m pytest -q tests "$@"
