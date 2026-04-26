#!/usr/bin/env bash
# check_docs.sh — CI entry point for KB drift + YAML validity.
#
# Runs in two passes:
#   1. `run_all.py --check` — every generator (modules, schema, ...)
#      must produce output identical to what's on disk.
#   2. `validate_all_yaml.py` — every ```yaml block in every .md, plus
#      every standalone .yaml under examples/ and cookbook/, must be
#      valid YAML and (if shaped like a full app) must pass Pydantic
#      AppDefinition validation.
#
# Exit 0 iff both pass. CI should block merge on any failure.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

PY="${PY:-py -3.12}"

echo "═══ Pass 1: generator drift ═══"
$PY -m knowledge_base.generators.run_all --check

echo ""
echo "═══ Pass 2: YAML validity ═══"
$PY knowledge_base/validate_all_yaml.py

echo ""
echo "═══ check_docs: ALL GREEN ═══"
