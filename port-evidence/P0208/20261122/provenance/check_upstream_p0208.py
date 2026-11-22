#!/usr/bin/env python3
"""Read-only moving-upstream input check for P0208; writes only its JSON report.

Uses the already-fetched local --ref (default origin/main); never fetches, invokes
Lean/Lake, edits a repository, or changes build/evidence inputs. Rerun after the
final fetch. Reports preserve the resolved commit and the compilation baselines.
"""
from __future__ import annotations
import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
BASE = "5a067f8d23e12b7c98be83b12add05a724e8177a"
EXT = "82f522b09a3e0ef920716dada3753e9b30f385bf"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--extension-evidence", type=Path,
                        help="Continuation evidence directory; may name a repaired continuation")
    args = parser.parse_args()
    root = args.workspace.resolve()
    repo = (args.repo or root / "alpoge-rh-replay").resolve()
    def git(*arguments):
        return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *arguments], stderr=subprocess.PIPE)
    current = git("rev-parse", "--verify", args.ref + "^{commit}").decode().strip()
    trees = {commit: set(git("ls-tree", "-r", "--name-only", commit).decode().splitlines()) for commit in (BASE, EXT, current)}
    cache = {}
    def blob(commit, name):
        key = (commit, name)
        if key not in cache:
            cache[key] = git("show", f"{commit}:{name}") if name in trees[commit] else None
        return cache[key]
    errors = []
    def require(condition, message):
        if not condition:
            errors.append(message)
    selection_path = root / "port-work/P0208-selection.json"
    selection = read_json(selection_path)
    phase1_path = root / "port-P0208-verified/evidence-repaired/P0208-receipt.json"
    phase1 = read_json(phase1_path)
    manifest_path = root / "port-extension-inputs/manifest.json"
    manifest = read_json(manifest_path)
    require(selection["accepted_commit"] == phase1["accepted_commit"] == BASE, "Phase-1 baseline differs")
    require(manifest["upstream_commit"] == EXT, "Extension input baseline differs")
    require(phase1["all_successful"] is True, "Phase-1 checkpoint is not successful")
    require(phase1["selection_sha256"] == sha(selection_path.read_bytes()), "Phase-1 selection fingerprint differs")
    old_targets = {r["id"] for r in selection["selected"]}
    new_targets = set(manifest["target_ids"])
    old_imports = {r["id"] for r in selection["imports"]}
    new_imports = {r["id"] for r in manifest["files"] if r["role"] == "import"}
    imports, targets = old_imports | new_imports, old_targets | new_targets
    require(len(old_targets) == 48 and len(new_targets) == 15 and len(targets) == 63, "Target scope is not 48+15 disjoint modules")
    require(len(old_imports) == 64 and len(new_imports) == 37 and len(imports) == 99, "Import scope is not 64+37 with two reused imports")
    old_records = {r["id"]: r for r in selection["accepted_import_records"]}
    require(set(old_records) == old_imports and phase1["imports"] == selection["accepted_import_records"], "Frozen phase-1 accepted imports differ")
    pins = selection["pins"]
    require(json.loads(blob(BASE, "port/target.json")) == pins, "Frozen target pins differ from baseline")
    preserved = {}
    names = sorted(set(selection["helper_sha256"]) | {"PORTING.md", "port/target.json", "port/pruned.txt", "tracker/modules.jsonl.gz"})
    for name in names:
        data = [blob(commit, name) for commit in (BASE, EXT, current)]
        same = data[0] is not None and data[0] == data[1] == data[2]
        preserved[name] = dict(identical=same, phase1_sha256=sha(data[0]) if data[0] else None,
                              extension_sha256=sha(data[1]) if data[1] else None,
                              current_sha256=sha(data[2]) if data[2] else None)
        require(same, f"Pinned/policy/helper file changed: {name}")
    ledger_data = blob(BASE, "tracker/modules.jsonl.gz")
    require(sha(ledger_data) == selection["ledger_sha256"] == manifest["ledger_sha256"], "Frozen ledger fingerprint differs")
    require(sha(blob(BASE, "port/target.json")) == selection["target_sha256"], "Frozen target fingerprint differs")
    wanted = imports | targets | {"M133708", "M139479", "M163509"}
    ledger = {}
    for line in gzip.GzipFile(fileobj=io.BytesIO(ledger_data)):
        row = json.loads(line)
        if row["id"] in wanted or row["packet"] == "P0208":
            ledger[row["id"]] = row
    require(wanted <= set(ledger), "Referenced modules are missing from the pinned ledger")
    for row in selection["selected"] + selection["imports"]:
        require(ledger[row["id"]] == row, f"Frozen ledger row differs: {row['id']}")
    accepted = {Path(name).stem for name in trees[current] if re.fullmatch(r"port/patches/M\d{6}\.json", name)}
    target_conflicts = sorted(mid for mid in targets if any(f"port/patches/{mid}.{suffix}" in trees[current] for suffix in ("json", "patch")))
    require(not target_conflicts, f"Targets already have upstream records/patches: {target_conflicts}")
    extension_receipt_path = (args.extension_evidence or root / "evidence-extension").resolve() / "P0208-extension-receipt.json"
    extension_receipt = read_json(extension_receipt_path) if extension_receipt_path.is_file() else None
    require(args.extension_evidence is None or extension_receipt is not None,
            "Requested continuation receipt is not available")
    extension_records = {r["id"]: r for r in extension_receipt["accepted_imports"]} if extension_receipt else None
    if extension_receipt:
        require(extension_receipt["accepted_commit"] == EXT and set(extension_records) == new_imports,
                "Extension receipt has different accepted inputs")
    comparisons = []
    for mid in sorted(imports):
        row = ledger[mid]
        used_by = {}
        current_record = blob(current, f"port/patches/{mid}.json")
        current_patch = blob(current, f"port/patches/{mid}.patch")
        for phase, commit, scope in (("phase1", BASE, old_imports), ("extension", EXT, new_imports)):
            if mid not in scope:
                continue
            record = blob(commit, f"port/patches/{mid}.json")
            patch = blob(commit, f"port/patches/{mid}.patch")
            require(record is not None, f"Missing accepted {phase} import record: {mid}")
            metadata = json.loads(record)
            for key, expected in (("module", row["module"]), ("source_sha256", row["source_sha256"]),
                                  ("lean", pins["lean"]), ("mathlib", pins["mathlib"])):
                require(metadata.get(key) == expected, f"Accepted {phase} import {key} mismatch: {mid}")
            require((metadata.get("notes") == "unchanged") == (patch is None), f"Accepted notes/patch mismatch: {mid}")
            fingerprints = dict(record_sha256=sha(record), patch_sha256=sha(patch) if patch is not None else None)
            if phase == "phase1":
                for key, value in fingerprints.items():
                    require(old_records[mid][key] == value, f"Frozen phase-1 {key} differs: {mid}")
            else:
                for suffix, data in (("json", record), ("patch", patch)):
                    local = root / "port-update-base/port/patches" / f"{mid}.{suffix}"
                    require((local.read_bytes() if local.is_file() else None) == data, f"Extension accepted input changed locally: {mid}.{suffix}")
                if extension_records:
                    for key, value in fingerprints.items():
                        require(extension_records[mid][key] == value, f"Extension receipt {key} differs: {mid}")
            identical = record == current_record and patch == current_patch
            require(identical, f"Current upstream differs from {phase} import record/patch: {mid}")
            used_by[phase] = dict(accepted_commit=commit, **fingerprints, identical_to_current=identical)
        comparisons.append(dict(id=mid, module=row["module"], used_by=used_by,
            current_record_sha256=sha(current_record) if current_record is not None else None,
            current_patch_sha256=sha(current_patch) if current_patch is not None else None))
    pruned = {line.strip() for line in blob(current, "port/pruned.txt").decode().splitlines() if line.strip() and not line.startswith("#")}
    remaining = []
    for mid, row in ledger.items():
        if row["packet"] == "P0208" and mid not in accepted | targets | pruned:
            remaining.append(dict(id=mid, module=row["module"],
                                  missing_imports=[dep for dep in row["imports"] if dep not in accepted | targets]))
    remaining.sort(key=lambda row: row["id"])
    require([r["id"] for r in remaining] == ["M133708"], "Remaining packet scope changed")
    require(len(remaining) == 1 and set(remaining[0]["missing_imports"]) == {"M139479", "M163509"},
            "Remaining M133708 import blockers changed")
    require(not (targets | {"M133708", "M139479", "M163509"}) & pruned, "A target or remaining blocker is now pruned")
    ref_after = git("rev-parse", "--verify", args.ref + "^{commit}").decode().strip()
    require(ref_after == current, "Local upstream ref moved during this check; rerun")
    now = dt.datetime.now(dt.timezone.utc)
    report = dict(schema="rh-port-upstream-compatibility-v1", packet="P0208", checked_utc=now.isoformat(),
        repository=str(repo), requested_ref=args.ref, current_commit=current, ref_after_check=ref_after,
        phase1_baseline=BASE, extension_baseline=EXT, pins=pins,
        selection_sha256=sha(selection_path.read_bytes()), phase1_receipt_sha256=sha(phase1_path.read_bytes()),
        extension_manifest_sha256=sha(manifest_path.read_bytes()),
        extension_receipt_path=str(extension_receipt_path),
        extension_receipt_sha256=sha(extension_receipt_path.read_bytes()) if extension_receipt else None,
        extension_receipt_status=("successful" if extension_receipt["all_successful"] else "unsuccessful") if extension_receipt else "not yet available",
        old_import_count=len(old_imports), extension_import_count=len(new_imports), unique_import_count=len(imports),
        overlapping_import_ids=sorted(old_imports & new_imports),
        patched_import_count=sum(r["current_patch_sha256"] is not None for r in comparisons),
        imported_modules=comparisons, preserved_files=preserved,
        target_count=len(targets), target_ids=sorted(targets), accepted_target_conflicts=target_conflicts,
        remaining=remaining, compatible=not errors, errors=errors,
        checker_sha256=sha(Path(__file__).read_bytes()),
        scope="Input compatibility only; compiler/audit results are independently verified by their evidence gates. No fetch or build was performed.")
    output = root / "port-publication" / f"P0208-upstream-compatibility-{current[:12]}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(dict(compatible=report["compatible"], checked_commit=current,
        imported_module_count=len(imports), patched_import_count=report["patched_import_count"],
        target_count=len(targets), accepted_target_conflicts=target_conflicts,
        remaining=remaining, errors=errors, report=str(output)), indent=2))
    return 0 if report["compatible"] else 1


if __name__ == "__main__":
    sys.exit(main())
