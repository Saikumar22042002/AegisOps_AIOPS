#!/usr/bin/env bash
# AegisOps terraform workspace scanner — checkov + tfsec over every workspace.
# Single source of truth for CI and local runs.
#
# Waivers are per-workspace (.checkov.yaml skip-check / .tfsec/config.yml exclude),
# every entry commented at the site AND mirrored in the PROGRESS.md scanner ledger.
# Anything not waived fails the scan — new findings must be fixed or triaged.
#
# .terraform/ caches and downloaded modules are excluded: upstream registry modules
# are version-pinned and scanned by their own projects; our source is what we own.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/terraform-workspaces" && pwd)"
fail=0

for ws in "$ROOT"/*/; do
  name="$(basename "$ws")"
  compgen -G "${ws}"'*.tf' > /dev/null || continue

  echo "== checkov: ${name}"
  cargs=(--directory "$ws" --quiet --compact --skip-path '\.terraform')
  [ -f "${ws}.checkov.yaml" ] && cargs+=(--config-file "${ws}.checkov.yaml")
  checkov "${cargs[@]}" || fail=1

  echo "== tfsec: ${name}"
  targs=("$ws" --no-color --concise-output --exclude-downloaded-modules)
  [ -f "${ws}.tfsec/config.yml" ] && targs+=(--config-file "${ws}.tfsec/config.yml")
  tfsec "${targs[@]}" || fail=1
done

if [ "$fail" -ne 0 ]; then
  echo "SCAN FAILED: untriaged findings above — fix them or add a documented waiver" >&2
  exit 1
fi
echo "All workspaces clean (waivers per-workspace, ledger in PROGRESS.md)."
