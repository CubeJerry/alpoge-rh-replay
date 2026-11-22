#!/usr/bin/env python3
"""Verify and export existing P0179 build evidence; never run Lean or Lake.

The initial check was performed in a manually created empty project. Original
check/repair receipts and logs are preserved, including their mode-based
fresh_project_objects flags. No missing build, audit, or setup observation is
manufactured by this exporter. Git and GNU patch are used only for static input
verification, with patch application confined to temporary directories.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


BASE = Path("/workspace/scratch/1fdb83412789")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SORRY = re.compile(r"declaration uses 'sorry(?:Ax)?'")
WATCHED: dict[Path, str] = {}


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    path = Path(path)
    require(path.is_file(), f"Missing required evidence/input: {path}")
    data = path.read_bytes()
    value = digest(data)
    require(path not in WATCHED or WATCHED[path] == value, f"Input changed while verifying: {path}")
    WATCHED[path] = value
    return data


def js(path):
    return json.loads(read(path))


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    require(result.returncode == 0, f"Git inspection failed: {' '.join(args)}\n{result.stderr.decode(errors='replace')}")
    return result.stdout


def source_rel(row):
    return "src/" + row["module"].replace(".", "/") + ".lean"


def object_path(tree, row):
    return tree / ".lake/build/lib/lean" / (row["module"].replace(".", "/") + ".olean")


def field(text, name):
    values = re.findall(r"^== " + re.escape(name) + r": (.*)$", text, re.M)
    require(len(values) == 1, f"Expected exactly one completed transcript field: {name}")
    return values[0]


def built_lines(text, module):
    pattern = re.compile(r"^\S+\s+\[\d+/\d+\]\s+Built\s+" + re.escape(module) + r"(?::olean)?(?:\s|$)")
    return [line for line in text.splitlines() if pattern.match(line)]


def normalized_command(command):
    return [part for part in command if part != "--no-ansi"]


def roundtrip(original, current, relative, patch):
    if patch is None:
        require(current == original, f"Changed source lacks a patch: {relative}")
        return
    require(current != original, f"Unchanged source has a stale patch: {relative}")
    # Restrict the patch to its single published source before applying it.
    result = subprocess.run(["git", "apply", "--numstat", "-"], input=patch, capture_output=True)
    require(result.returncode == 0, f"Invalid unified patch for {relative}")
    stats = result.stdout.decode().splitlines()
    require(len(stats) == 1 and stats[0].split("\t")[-1] == relative,
            f"Patch must affect exactly {relative}: {stats}")
    with tempfile.TemporaryDirectory(prefix="port-source-roundtrip-") as tmp:
        root = Path(tmp)
        source = root / relative
        source.parent.mkdir(parents=True)
        source.write_bytes(original)
        for reverse, expected in ((False, current), (True, original)):
            command = ["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch", "-p1"]
            command += ["--reverse"] if reverse else ["--forward"]
            result = subprocess.run(command, input=patch, cwd=root, capture_output=True)
            report = (result.stdout + result.stderr).decode(errors="replace")
            require(result.returncode == 0 and not re.search(r"\b(?:offset|fuzz)\b", report, re.I),
                    f"Patch did not apply exactly for {relative}: {report}")
            require(source.read_bytes() == expected, f"Patch roundtrip mismatch: {relative}")


def dependency_state(tree, manifest, pins):
    packages = manifest["packages"]
    require(len(packages) == 9 and len({p["name"] for p in packages}) == 9,
            "Expected the frozen manifest's nine distinct dependencies")
    result = []
    for package in packages:
        require(package["type"] == "git", "Dependency is not a pinned Git checkout")
        directory = tree / ".lake/packages" / package["name"]
        head = git(directory, "rev-parse", "HEAD").decode().strip()
        require(head == package["rev"], f"Wrong dependency HEAD: {package['name']}")
        require(not git(directory, "status", "--porcelain", "--untracked-files=no").strip(),
                f"Tracked dependency modifications: {package['name']}")
        result.append(dict(name=package["name"], url=package["url"], manifest_revision=package["rev"],
                           head=head, tracked_files_clean=True))
    require(next(p["head"] for p in result if p["name"] == "mathlib") == pins["mathlib"],
            "Manifest Mathlib revision differs from the target")
    return result


def assemble(args):
    repo, work, out = args.repo, args.work, args.out
    tree = work / "lean"
    selection_bytes = read(args.selection)
    selection = json.loads(selection_bytes)
    packet = selection["packet"]
    require(packet == "P0179", "This evidence helper is scoped to P0179")
    targets, imports = selection["selected"], selection["imports"]
    require(len(targets) == 4 and len(imports) == 8, "Expected four targets and eight accepted imports")
    closure = targets + imports
    by_id = {row["id"]: row for row in closure}
    require(len(by_id) == 12, "Duplicate selected/imported module ID")
    accepted, pins = selection["accepted_commit"], selection["pins"]
    payloads: dict[str, bytes] = {}

    def put(relative, data):
        require(not Path(relative).is_absolute() and ".." not in Path(relative).parts,
                f"Invalid export path: {relative}")
        require(relative not in payloads or payloads[relative] == data, f"Conflicting export: {relative}")
        payloads[relative] = data

    def frozen(relative, expected=None):
        data = read(repo / relative)
        require(data == git(repo, "show", f"{accepted}:{relative}"), f"File differs from accepted baseline: {relative}")
        if expected is not None:
            require(digest(data) == expected, f"Frozen hash mismatch: {relative}")
        return data

    target_bytes = frozen("port/target.json", selection["target_sha256"])
    require(json.loads(target_bytes) == pins, "Selection pins differ from port/target.json")
    ledger_bytes = frozen("tracker/modules.jsonl.gz", selection["ledger_sha256"])
    ledger = {r["id"]: r for r in map(json.loads, gzip.decompress(ledger_bytes).splitlines())}
    for row in closure:
        require(all(ledger[row["id"]][key] == row[key] for key in
                    ("id", "module", "source_sha256", "imports", "depth", "packet")),
                f"Frozen selection disagrees with ledger: {row['id']}")
    needed, pending = set(), [r["id"] for r in targets]
    while pending:
        mid = pending.pop()
        if mid not in needed:
            needed.add(mid)
            pending.extend(ledger[mid]["imports"])
    require(needed == set(by_id), "Selection is not the complete project import closure")
    for relative, expected in selection["helper_sha256"].items():
        frozen(relative, expected)
    put("selection.json", selection_bytes)
    put("provenance/port-target.json", target_bytes)
    for name, expected in selection["project_config"].items():
        data = read(tree / name)
        require(digest(data) == expected, f"Project configuration changed: {name}")
        put(f"provenance/{name}", data)
    require(read(tree / "lean-toolchain").decode().strip() == pins["lean"], "Wrong project toolchain")
    manifest = js(tree / "lake-manifest.json")
    dependencies = dependency_state(tree, manifest, pins)

    import_records = {r["id"]: r for r in selection["accepted_import_records"]}
    require(set(import_records) == {r["id"] for r in imports}, "Accepted import inventory mismatch")
    inputs = {}
    for row in closure:
        mid, relative = row["id"], source_rel(row)
        original = read(work / "pinned-sources" / relative)
        current = read(tree / relative)
        require(digest(original) == row["source_sha256"], f"Pinned source hash mismatch: {mid}")
        if mid in import_records:
            accepted_record = import_records[mid]
            record_data = frozen(f"port/patches/{mid}.json", accepted_record["record_sha256"])
            record = json.loads(record_data)
            require(all(record[key] == (row[key] if key in ("module", "source_sha256") else pins[key])
                        for key in ("module", "source_sha256", "lean", "mathlib")), f"Bad accepted record: {mid}")
            patch_path = repo / f"port/patches/{mid}.patch"
            patch = frozen(f"port/patches/{mid}.patch", accepted_record["patch_sha256"]) if accepted_record["patch_sha256"] else None
            require(patch is not None or not patch_path.exists(), f"Unexpected accepted-import patch: {mid}")
            require((record["notes"] == "unchanged") == (patch is None), f"Accepted notes/patch mismatch: {mid}")
            require(digest(current) == accepted_record["built_source_sha256"], f"Accepted import source changed: {mid}")
            put(f"provenance/accepted-imports/{mid}.json", record_data)
            if patch is not None:
                put(f"provenance/accepted-imports/{mid}.patch", patch)
        else:
            patch_path = args.patches / f"{mid}.patch"
            patch = read(patch_path) if patch_path.exists() else None
            if patch is not None:
                put(f"provenance/target-patches/{mid}.patch", patch)
        roundtrip(original, current, relative, patch)
        output = read(object_path(tree, row))
        require(output, f"Empty output object: {mid}")
        inputs[mid] = dict(id=mid, module=row["module"], source_sha256=digest(original),
                           built_source_sha256=digest(current), patch_sha256=digest(patch) if patch is not None else None,
                           olean_sha256=digest(output), source_patch_roundtrip_verified=True)

    runs, transcripts = [], []
    check_dirs = [args.initial_check, *args.additional_check]
    require(len(set(check_dirs)) == len(check_dirs), "Duplicate build-history directory")
    for index, directory in enumerate(check_dirs):
        label = "initial-check" if index == 0 else f"additional-check-{index}"
        raw_bytes = read(directory / f"{packet}-receipt.json")
        raw = json.loads(raw_bytes)
        require(raw["schema"] == "rh-port-mirror-receipt-v1" and raw["target"] == packet,
                f"Unexpected original check receipt: {directory}")
        require(raw["selection_sha256"] == digest(selection_bytes) and raw["accepted_commit"] == accepted,
                f"Build used a different frozen selection: {directory}")
        require(raw["project_config"] == selection["project_config"] and raw["ledger_sha256"] == selection["ledger_sha256"]
                and raw["target_sha256"] == selection["target_sha256"], f"Build input identity mismatch: {directory}")
        require(raw["mathlib"] == pins["mathlib"] and raw["toolchain"].startswith(
            f"Lean (version {pins['lean'].rsplit(':v', 1)[-1]},"), f"Build target mismatch: {directory}")
        require(digest(read(directory / f"{packet}-build.log")) == raw["build_log_sha256"],
                f"Build summary hash mismatch: {directory}")
        recorded_ids = {r["id"] for r in raw["modules"]}
        require(recorded_ids and len(recorded_ids) == len(raw["modules"])
                and recorded_ids <= {r["id"] for r in targets}, f"Invalid check module inventory: {directory}")
        if index == 0:
            require(len(recorded_ids) == 4, f"Initial check receipt is not complete: {directory}")
        if (directory / "selection.json").exists():
            require(read(directory / "selection.json") == selection_bytes, f"History selection differs: {directory}")
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                require(not path.is_symlink() and path.suffix in (".json", ".log", ".txt", ".lean"),
                        f"Unexpected binary or symlink in evidence history: {path}")
                put(f"history/{label}/{path.relative_to(directory).as_posix()}", read(path))
        for result in raw["modules"]:
            mid = result["id"]
            require(result["module"] == by_id[mid]["module"] and result["source_sha256"] == by_id[mid]["source_sha256"],
                    f"Original module identity mismatch: {mid}")
            data = read(directory / "logs" / f"{mid}.log")
            require(digest(data) == result["log_sha256"], f"Original transcript hash mismatch: {directory}/{mid}")
            text = ANSI.sub("", data.decode(errors="replace"))
            command = shlex.split(field(text, "command"))
            expected = ["elan", "run", pins["lean"], "lake", "--no-cache", "build", f"+{result['module']}:olean"]
            require(normalized_command(command) == expected and command == result["command"], f"Unexpected build command: {mid}")
            require(Path(field(text, "cwd")).resolve() == tree.resolve(), f"Unexpected build tree: {mid}")
            require(int(field(text, "exit_code")) == result["exit_code"], f"Transcript exit-code mismatch: {mid}")
            require(field(text, "started_utc") == result["started_utc"] and field(text, "ended_utc") == result["ended_utc"],
                    f"Transcript time mismatch: {mid}")
            transcripts.append(dict(id=mid, result=result, text=text, data=data,
                                    history=f"history/{label}/logs/{mid}.log"))
        runs.append(dict(label=label, receipt=f"history/{label}/{packet}-receipt.json", receipt_sha256=digest(raw_bytes),
                         original_fresh_project_objects=raw["fresh_project_objects"],
                         original_all_successful=raw["all_successful"], driver_sha256=raw["driver_sha256"],
                         started_utc=raw["started_utc"], ended_utc=raw["ended_utc"], issue=raw.get("issue")))
    initial_driver = read(out / "provenance/initial_driver.py")
    require(digest(initial_driver) == runs[0]["driver_sha256"], "Initial driver does not match original receipt")
    put("provenance/initial_driver.py", initial_driver)

    submitted = []
    for row in targets:
        mid, current = row["id"], inputs[row["id"]]
        eligible = []
        for transcript in transcripts:
            r, text = transcript["result"], transcript["text"]
            if transcript["id"] != mid:
                continue
            if (r["exit_code"] == 0 and r["successful"] is True and not r["source_changed_during_build"]
                    and not r["sorry_diagnostic"] and not SORRY.search(text)
                    and all(r[key] == current[key] for key in ("built_source_sha256", "olean_sha256"))
                    and built_lines(text, row["module"])
                    and any(line.startswith("Build completed successfully") for line in text.splitlines())):
                eligible.append(transcript)
        require(eligible, f"No successful producing Built transcript matches current source and output: {mid}")
        chosen = eligible[-1]
        relative = f"logs/{mid}.log"
        put(relative, chosen["data"])
        submitted.append(dict(**current, successful=True, log=relative, log_sha256=digest(chosen["data"]),
                              original_history_log=chosen["history"], built_lines=built_lines(chosen["text"], row["module"]),
                              completion_lines=[line for line in chosen["text"].splitlines() if line.startswith("Build completed successfully")],
                              command=chosen["result"]["command"], exit_code=0,
                              producing_record_patch_sha256=chosen["result"]["patch_sha256"],
                              patch_provenance="The final patch is roundtrip-verified against pinned bytes and the compiled current source. Its file may have been generated after compilation; the original record's patch hash is retained separately.",
                              started_utc=chosen["result"]["started_utc"], ended_utc=chosen["result"]["ended_utc"]))
    accepted_imports = []
    for row in imports:
        producing = [t for t in transcripts if built_lines(t["text"], row["module"])]
        require(producing, f"No actual Built line for accepted project import: {row['id']}")
        chosen = producing[-1]
        accepted_imports.append(dict(**inputs[row["id"]], accepted_record_sha256=import_records[row["id"]]["record_sha256"],
                                     producing_history_log=chosen["history"], producing_log_sha256=digest(chosen["data"]),
                                     built_lines=built_lines(chosen["text"], row["module"]),
                                     output_hash_basis="Current stable object hash; the original check receipt records target output hashes only."))

    audits = {}
    modules = {r["module"] for r in closure}
    for name in ("islattice", "reused"):
        source = out / f"{name}_audit.lean"
        source_bytes = read(source)
        require(source_bytes == frozen(f"port/{name}_audit.lean"), f"Wrong audit source: {name}")
        data = read(out / f"{name}-audit.log")
        text = ANSI.sub("", data.decode(errors="replace"))
        command = shlex.split(field(text, "command"))
        normalized = normalized_command(command)
        prefix = ["elan", "run", pins["lean"], "lake", "env", "lean", "--run"]
        require(normalized[:len(prefix)] == prefix and len(normalized) == len(prefix) + 1 + 12,
                f"Unexpected aggregate audit command: {name}")
        require(Path(normalized[len(prefix)]).resolve() == source.resolve()
                and set(normalized[len(prefix) + 1:]) == modules, f"Wrong audit source or module scope: {name}")
        require(Path(field(text, "cwd")).resolve() == tree.resolve(), f"Audit used the wrong project: {name}")
        require(field(text, "exit_code") == "0" and not SORRY.search(text), f"Audit did not finish cleanly: {name}")
        summary = f"{name}-audit: 12 module(s), 0 hit(s)"
        require(summary in text.splitlines(), f"Missing complete zero-hit audit summary: {name}")
        audits[name] = dict(successful=True, command=command, exit_code=0, module_count=12, modules=sorted(modules),
                            source=source.name, source_sha256=digest(source_bytes), accepted_commit=accepted,
                            source_matches_accepted_commit=True,
                            source_hash_in_frozen_selection=f"port/{name}_audit.lean" in selection["helper_sha256"],
                            log=f"{name}-audit.log", log_sha256=digest(data), summary=summary,
                            started_utc=field(text, "started_utc"), ended_utc=field(text, "ended_utc"))
        put(source.name, source_bytes)
        put(f"{name}-audit.log", data)

    issue = args.issue or runs[0]["issue"]
    require(isinstance(issue, str) and re.fullmatch(r"https://github\.com/rohanarun/alpoge-rh-replay/issues/\d+", issue),
            "Supply the packet's reservation issue URL with --issue")
    driver = read(Path(__file__).resolve())
    put("provenance/finalize_evidence.py", driver)
    receipt = dict(
        schema="rh-port-initial-fresh-tree-evidence-v1", target=packet, issue=issue, all_successful=True,
        build_mode="initial_check_in_manually_created_fresh_tree", initial_project_objects_reused=False,
        accepted_commit=accepted, pins=pins, source_method=selection["source_method"],
        source_base_url=selection["source_base_url"], selection_sha256=digest(selection_bytes),
        ledger_sha256=selection["ledger_sha256"], target_sha256=selection["target_sha256"],
        helper_sha256=selection["helper_sha256"], project_config=selection["project_config"],
        initial_fresh_tree_provenance=dict(
            tree=str(tree), pinned_project_sources=12, selected_targets=4, accepted_imports=8,
            shared_dependency_packages=True, original_driver_mode="check", original_receipts_preserved=True,
            observation="The project was created empty during this session, with no project objects reused. All 12 original sources were SHA-256 verified before accepted import patches were applied. Only the exact target dependency packages were shared.",
            observation_limit="This records the session's setup observation. No saved pre-build inventory is claimed. Static checks corroborate it using frozen sources/configuration and actual producing Built lines for all 12 project modules.",
            original_fresh_flag_explanation="The initial driver's fresh_project_objects=false denotes check mode, not reuse of earlier project outputs. Original receipts are preserved byte-for-byte. Any repair runs are continuations of the same initially fresh tree."),
        original_runs=runs, modules=submitted, imports=accepted_imports, audits=audits,
        dependencies=dependencies, all_12_project_source_patch_roundtrips_verified=True,
        all_12_project_object_hashes_recorded=True, remaining=selection["remaining"],
        pruned=selection["pruned"], already_ported=selection["already_ported"],
        finalizer_sha256=digest(driver), no_build_or_audit_invoked_by_finalizer=True)
    put(f"{packet}-receipt.json", (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode())
    require(dependency_state(tree, manifest, pins) == dependencies, "Dependency state changed during verification")
    for path, expected in WATCHED.items():
        require(path.is_file() and digest(path.read_bytes()) == expected, f"Input changed during verification: {path}")
    for relative, data in payloads.items():
        destination = out / relative
        require(not destination.exists() or destination.read_bytes() == data,
                f"Refusing to overwrite different published evidence: {destination}")
        if args.verify_only:
            require(destination.is_file(), f"Export is incomplete: {destination}")
    if not args.verify_only:
        for relative, data in payloads.items():
            destination = out / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                with destination.open("xb") as stream:
                    stream.write(data)
    sums = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path != out / "SHA256SUMS":
            require(not path.is_symlink(), f"Unexpected symlink in export: {path}")
            sums.append(f"{digest(path.read_bytes())}  {path.relative_to(out).as_posix()}\n")
    checksum_data = "".join(sums).encode()
    if args.verify_only:
        require(read(out / "SHA256SUMS") == checksum_data, "Export SHA256SUMS is incomplete or incorrect")
    else:
        (out / "SHA256SUMS").write_bytes(checksum_data)
    print(f"PASS: {len(submitted)} target producing transcripts, {len(accepted_imports)} compiled imports, "
          f"two zero-hit 12-module audits, nine pinned clean dependencies; {'verified' if args.verify_only else 'exported'} {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=BASE / "replay")
    parser.add_argument("--work", type=Path, default=BASE / "port-work")
    parser.add_argument("--selection", type=Path, default=BASE / "port-work/P0179-selection.json")
    parser.add_argument("--initial-check", type=Path, default=BASE / "initial-check")
    parser.add_argument("--additional-check", type=Path, action="append", default=[])
    parser.add_argument("--patches", type=Path, help="Final target patches; defaults to REPO/port/patches")
    parser.add_argument("--out", type=Path, default=BASE / "port-publication/files")
    parser.add_argument("--issue", help="Reservation issue URL, otherwise taken from the initial receipt")
    parser.add_argument("--verify-only", action="store_true", help="Verify an existing export without writing files")
    args = parser.parse_args()
    for name in ("repo", "work", "selection", "initial_check", "out"):
        setattr(args, name, getattr(args, name).resolve())
    args.patches = (args.patches or args.repo / "port/patches").resolve()
    args.additional_check = [path.resolve() for path in args.additional_check]
    assemble(args)


if __name__ == "__main__":
    main()
