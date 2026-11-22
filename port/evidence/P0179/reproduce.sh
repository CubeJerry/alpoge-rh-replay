#!/usr/bin/env bash
# Rebuild the four submitted P0179 targets from a checkout containing this PR.
# Usage: bash reproduce.sh /absolute/replay-checkout /absolute/new-build-directory
set -euo pipefail

port_repo_dir="${1:?supply the repository checkout containing the submitted records}"
port_build_dir="${2:?supply a new, empty build directory}"
port_evidence_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
port_repo_dir="$(cd -- "$port_repo_dir" && pwd)"
if [[ -e "$port_build_dir" ]]; then
  printf '%s\n' 'Use a new build directory; refusing to reuse project outputs.' >&2
  exit 1
fi
mkdir -p -- "$port_build_dir"
port_build_dir="$(cd -- "$port_build_dir" && pwd)"

command -v elan >/dev/null
elan toolchain install leanprover/lean4:v4.36.0
python3 "$port_repo_dir/scripts/port-ci-build.py" \
  --workspace "$port_build_dir" --prepare-only \
  --patch-ids M022910 M023055 M026102 M027599

cd -- "$port_build_dir"
elan run leanprover/lean4:v4.36.0 lake update
elan run leanprover/lean4:v4.36.0 lake exe cache get
test "$(git -C .lake/packages/mathlib rev-parse HEAD)" = fc4f59e577feb1a4956671bc04ac18aaaf8cd842
test ! -d .lake/build/lib/lean

# Lake's worker budget is not a per-compiler memory limit.
export LEAN_NUM_THREADS="${LEAN_NUM_THREADS:-2}"
mkdir -p transcripts
while IFS=$'\t' read -r port_module_id port_module_name; do
  elan run leanprover/lean4:v4.36.0 lake --no-ansi --no-cache \
    build "+${port_module_name}:olean" 2>&1 | tee "transcripts/${port_module_id}.log"
done < "$port_evidence_dir/targets.tsv"

mapfile -t port_audit_modules < "$port_evidence_dir/closure-modules.txt"
for port_audit_name in islattice reused; do
  elan run leanprover/lean4:v4.36.0 lake --no-ansi env lean --run \
    "$port_evidence_dir/${port_audit_name}_audit.lean" "${port_audit_modules[@]}" \
    2>&1 | tee "transcripts/${port_audit_name}-audit.log"
done
