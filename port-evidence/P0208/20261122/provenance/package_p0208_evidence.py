#!/usr/bin/env python3
"""Package already verified P0208 evidence; does not create records or publish."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--phase1-evidence", type=Path,
                        help="Explicit successful evidence directory, including a repaired first phase")
    parser.add_argument("--patches", type=Path, help="Verified candidate target-patch directory")
    parser.add_argument("--phase1-only", action="store_true")
    args = parser.parse_args()
    root, out = args.workspace.resolve(), args.out.resolve()
    if out.exists():
        raise SystemExit(f"Refusing to overwrite existing evidence package: {out}")
    gate_command = [sys.executable, "-B", str(root / "port-tools/verify_p0208_evidence.py"),
                    "--workspace", str(root)]
    gate_command += ["--phase1-only"] if args.phase1_only else ["--combine"]
    phase1_evidence = (args.phase1_evidence or root / "port-P0208-verified/evidence").resolve()
    patches = (args.patches or root / "alpoge-rh-replay/port/patches").resolve()
    if args.phase1_evidence:
        gate_command += ["--phase1-evidence", str(phase1_evidence)]
    if args.patches:
        gate_command += ["--patches", str(patches)]
    gate = subprocess.run(gate_command, capture_output=True, text=True)
    if gate.returncode:
        sys.stdout.write(gate.stdout)
        sys.stderr.write(gate.stderr)
        raise SystemExit(gate.returncode)

    evidence = out / "files"
    evidence.mkdir(parents=True)
    phase_sources = [("phase-1", phase1_evidence, "P0208-receipt.json")]
    if not args.phase1_only:
        phase_sources.append(("phase-2", root / "evidence-extension", "P0208-extension-receipt.json"))
    phases, modules = [], []
    for name, source, receipt_name in phase_sources:
        shutil.copytree(source, evidence / name)
        receipt = json.loads((source / receipt_name).read_text())
        phases.append({"name": name, "receipt": f"{name}/{receipt_name}",
                       "receipt_sha256": digest(source / receipt_name),
                       "accepted_commit": receipt["accepted_commit"],
                       "fresh_project_objects": receipt["fresh_project_objects"],
                       "target_count": len(receipt["modules"]),
                       "batch_log": f"{name}/batch-build.log",
                       "audit_log": f"{name}/islattice-audit.log"})
        for module in receipt["modules"]:
            modules.append({**module, "phase": name, "receipt": f"{name}/{receipt_name}",
                            "log": f"{name}/logs/{module['id']}.log",
                            "shared_log": f"{name}/batch-build.log",
                            "audit_log": f"{name}/islattice-audit.log"})
    remaining = receipt["remaining"]
    p1 = json.loads((phase_sources[0][1] / phase_sources[0][2]).read_text())
    provenance = evidence / "provenance"
    provenance.mkdir()
    driver_names = ["packet_driver.py", "packet_extension.py", "verify_p0208_evidence.py",
                    "package_p0208_evidence.py"]
    if p1["schema"] == "rh-port-repaired-batch-receipt-v1":
        driver_names.append("packet_repair.py")
    for name in driver_names:
        shutil.copyfile(root / "port-tools" / name, provenance / name)
    patched = [row for row in modules if row["patch_sha256"] is not None]
    if patched:
        target_patches = provenance / "target-patches"
        target_patches.mkdir()
        notes = json.loads((root / "port-candidate-patches/notes.json").read_text())
        for row in patched:
            patch = patches / (row["id"] + ".patch")
            if digest(patch) != row["patch_sha256"]:
                raise SystemExit(f"Final target patch differs from evidence: {row['id']}")
            shutil.copyfile(patch, target_patches / patch.name)
        write_json(target_patches / "notes.json", {row["id"]: notes[row["id"]] for row in patched})
    tree = root / "port-P0208-verified/lean"
    for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        shutil.copyfile(tree / name, provenance / name)
    shutil.copyfile(root / "port-update-base/port/target.json", provenance / "port-target.json")
    (provenance / "verification.log").write_text(
        "Command: " + " ".join(gate_command) + "\n" + gate.stdout + gate.stderr +
        f"Exit code: {gate.returncode}\n")
    module_index = {row["id"]: row for row in modules}
    if len(module_index) != len(modules):
        raise SystemExit("Duplicate target ID across evidence phases")
    index = {
        "schema": "rh-port-packet-evidence-index-v1", "packet": "P0208",
        "issue": "https://github.com/rohanarun/alpoge-rh-replay/issues/28141",
        "lean": "leanprover/lean4:v4.36.0", "toolchain": p1["toolchain"],
        "mathlib": p1["mathlib"], "phases": phases,
        "submitted_count": len(modules), "packet_live_count": 64,
        "submitted_ids": sorted(module_index), "modules": sorted(modules, key=lambda row: row["id"]),
        "remaining": remaining, "pruned": p1["pruned"],
        "all_submitted_successful": all(row["successful"] for row in modules),
        "packet_complete": False,
        "source_method": "Upstream's pinned GitHub mirror; every source checked against tracker/modules.jsonl.gz. No full archive extraction is claimed.",
        "transcript_note": "Per-module logs are explicitly labelled excerpts of shared batch invocations. The complete shared transcripts and audit logs are included.",
        "replay_credit": False,
    }
    write_json(evidence / "packet-index.json", index)
    count = len(modules)
    unchanged = sum(row["patch_sha256"] is None for row in modules)
    phase_lines = "\n".join(
        f"- **{phase['name']}**: {phase['target_count']} targets; "
        f"[receipt]({phase['receipt']}), [full build transcript]({phase['batch_log']}), "
        f"[audit transcript]({phase['audit_log']})."
        for phase in phases
    )
    target_lines = "\n".join(
        f"| {row['id']} | `{row['module']}` | [{row['phase']} log]({row['log']}) |"
        for row in index["modules"]
    )
    remaining_lines = "\n".join(
        f"| {row['id']} | `{row['module']}` | {', '.join(row['missing_imports'])} |"
        for row in remaining
    )
    initial_method = (
        "The original fresh batch found mechanical proof errors. Its complete failed transcript and receipt are retained within phase 1. The corrected targets were rebuilt in the same tree; successful outputs were retained with before/after digests. A successful invocation requesting all 48 targets and the 48-target audit then established the repaired result."
        if p1["schema"] == "rh-port-repaired-batch-receipt-v1" else
        "Phase 1 compiled all selected targets successfully in its original fresh batch."
    )
    (evidence / "README.md").write_text(f"""# P0208 port build evidence

This evidence covers **{count} of 64 live packet modules** on Lean 4.36.0 and Mathlib `fc4f59e577feb1a4956671bc04ac18aaaf8cd842` (v4.36.0). All {count} submitted builds and their required audits passed. {unchanged} target sources are unchanged from their pinned bytes. No replay pins or replay credit are affected.

Claim: [Port P0208, issue 28141](https://github.com/rohanarun/alpoge-rh-replay/issues/28141).

## Build phases

{phase_lines}

Phase 1 began with no project objects. Only the exact target dependency packages and their Mathlib cache were shared. {initial_method} A later continuation, when present, retains the verified first-phase project objects and records their before/after hashes. It does not claim a second fresh build of the whole packet. Earlier receipts and transcripts remain unchanged.

Sources were obtained through the repository's pinned source-mirror helper, with every file's SHA-256 checked against `tracker/modules.jsonl.gz`. Accepted import patches were applied without offsets or fuzz. The receipts identify their accepted upstream commits and all source, patch, object and log digests. No full archive extraction is claimed.

The full shared transcripts contain every compiler diagnostic. Each module log below is explicitly labelled as an excerpt from shared invocations, not a separate invocation. Compiler warnings are retained. Successful records require an actual target `Built` line in the producing attempt, a verified output object, a successful complete final validation batch, no sorry diagnostic in accepted output evidence, preserved inputs, and a zero-hit `IsLattice` audit.

## Review and reproduction information

[packet-index.json](packet-index.json) maps every target to its receipt, module excerpt, full transcript, audit and digests. [SHA256SUMS](SHA256SUMS) covers every evidence file except itself. The receipts preserve exact commands, times, pins, helper identities and resource measurements. A reported largest-child RSS is not a sum of simultaneous process memory.

The `provenance/` directory contains the executed driver sources, the independent evidence verifier, frozen Lean/Lake configuration and dependency manifest, target pins, and the verifier's successful transcript. Driver defaults refer to the original workspace; pass the recorded workspace arguments or recreate that directory structure when checking the retained build tree. The phase selections and input manifests identify exactly which pinned sources and accepted patches were used.

The elaborated audit covers the submitted packet targets. The repository-wide `Main` build, full axiom audit, statement comparison and independent checker run remain separate maintainer work.

## Submitted module transcripts

| ID | Module | Transcript excerpt |
|---|---|---|
{target_lines}

## Remaining packet members at the final phase baseline

| ID | Module | Missing accepted imports |
|---|---|---|
{remaining_lines}

Pruned packet IDs: {', '.join(p1['pruned']) or 'none'}. This is a partial packet result; its remaining state is recorded at each phase's frozen upstream baseline.
""")
    files = sorted(path for path in evidence.rglob("*") if path.is_file())
    (evidence / "SHA256SUMS").write_text("".join(
        f"{digest(path)}  {path.relative_to(evidence).as_posix()}\n" for path in files))
    prefix = "port-evidence/P0208/20261122"
    elements = [{"path": prefix + "/" + path.relative_to(evidence).as_posix(),
                 "mode": "100644", "type": "blob", "content": path.read_text()}
                for path in sorted(evidence.rglob("*")) if path.is_file()]
    payload = out / "tree-elements.json"
    write_json(payload, elements)
    print(json.dumps({"targets": count, "unchanged": unchanged, "files": len(elements),
                      "directory": str(evidence), "payload": str(payload),
                      "payload_bytes": payload.stat().st_size, "payload_sha256": digest(payload)}))


if __name__ == "__main__":
    main()
