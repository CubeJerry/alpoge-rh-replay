#!/usr/bin/env python3
"""Continue the verified P0208 build with 15 newly ready targets; never write port records.

--plan performs read-only checks and works while the phase-1 receipt is pending.
Execution requires the completed successful receipt, preserves every old project
object and source, and keeps the existing Lake configuration byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
import packet_driver as driver

ACCEPTED = "82f522b09a3e0ef920716dada3753e9b30f385bf"
PACKET = "P0208"
NEW_IDS = set("M131597 M131764 M131872 M133286 M133296 M133354 M133390 M133750 "
              "M133896 M133979 M134003 M134040 M134090 M134230 M134329".split())


def fail(message):
    raise SystemExit(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def built_lines(text, module):
    pattern = r"^\S+\s+\[\d+/\d+\]\s+Built\s+" + re.escape(module) + r"(?::olean)?(?:\s|$)"
    return [line for line in text.splitlines() if re.match(pattern, line)]


def object_path(tree, row):
    return tree / ".lake/build/lib/lean" / (row["module"].replace(".", "/") + ".olean")


def snapshot(tree, rows, h):
    result = {}
    for row in rows:
        source = tree / h.build.source_relpath(row)
        obj = object_path(tree, row)
        if not source.is_file() or not obj.is_file():
            fail(f"Missing retained source/object for {row['id']}")
        artifacts = [obj] + [Path(str(obj) + suffix) for suffix in (".private", ".server")
                             if Path(str(obj) + suffix).is_file()]
        result[row["id"]] = dict(module=row["module"], source_sha256=h.tasks.digest(source),
                                  artifacts={p.relative_to(tree).as_posix(): h.tasks.digest(p) for p in artifacts})
    return result


def repaired_chain(args, h, prior, tree, receipt, selection, rows, results, ledger):
    info = receipt["original_attempt"]
    original_path = Path(info["receipt_path"])
    if original_path != args.phase1 / "evidence/P0208-receipt.json" or info["archived_receipt_path"] != "original-attempt/P0208-receipt.json" or info["batch_log_path"] != "original-attempt/batch-build.log":
        fail("Repaired checkpoint does not identify the immutable initial attempt")
    for path in (original_path, prior / info["archived_receipt_path"]):
        driver.require_hash(path, info["receipt_sha256"], h)
    original = json.loads(original_path.read_text())
    initial_files = {p.relative_to(original_path.parent).as_posix(): h.tasks.digest(p) for p in original_path.parent.rglob("*") if p.is_file()}
    archive = prior / "original-attempt"
    archived_files = {p.relative_to(archive).as_posix(): h.tasks.digest(p) for p in archive.rglob("*") if p.is_file()}
    if initial_files != archived_files or receipt["original_attempt_unchanged"] is not True:
        fail("Original attempt was not preserved byte-for-byte")
    if original["schema"] != "rh-port-mirror-batch-receipt-v1" or original["fresh_project_objects"] is not True or original["all_successful"] or original["batch_build"]["exit_code"] in (None, 0):
        fail("Repaired checkpoint lacks a failed fresh original attempt")
    for key in ("accepted_commit", "project_config", "helper_sha256", "ledger_sha256", "target_sha256", "imports", "selection_sha256"):
        if original[key] != receipt[key]:
            fail(f"Repair changed original provenance: {key}")
    if info["batch_log_sha256"] != original["batch_build"]["log_sha256"] or info["batch_exit_code"] != original["batch_build"]["exit_code"] or info["fresh_project_objects"] is not True:
        fail("Original-attempt compilation link differs from its receipt")
    driver.require_hash(prior / info["batch_log_path"], info["batch_log_sha256"], h)
    original_text = (prior / info["batch_log_path"]).read_text()
    if h.ci.SORRY_DIAG_RE.search(original_text):
        fail("Initial attempt contains a sorry diagnostic")
    targets = {r["id"] for r in selection["selected"]}
    retained, rebuilt = set(receipt["retained_target_ids"]), set(receipt["rebuilt_target_ids"])
    if retained & rebuilt or retained | rebuilt != targets or not set(receipt["repaired_target_ids"]) <= rebuilt:
        fail("Repaired checkpoint target partition is inconsistent")
    for key in ("unexpected_rebuilt_retained", "changed_retained_outputs", "changed_sources", "changed_config", "changed_patches", "target_dependency_edges"):
        if receipt[key]:
            fail(f"Repaired checkpoint integrity check failed: {key}")
    for row in selection["selected"]:
        if set(h.ci.transitive_closure(row["imports"], ledger)) & targets:
            fail("A phase-1 target depends on another selected target")
    for name, key in (("reuse-before.json", "reuse_before_sha256"), ("reuse-after.json", "reuse_after_sha256"),
                      ("patch-notes.json", "patch_notes_sha256"), ("proof-only-reviews.json", "proof_only_reviews_sha256")):
        driver.require_hash(prior / name, receipt[key], h)
    before, after = [json.loads((prior / name).read_text()) for name in ("reuse-before.json", "reuse-after.json")]
    reuse_ids = retained | {r["id"] for r in selection["imports"]}
    if before != after or set(before) != reuse_ids or before != snapshot(tree, [r for r in rows if r["id"] in reuse_ids], h):
        fail("Retained original sources/objects differ from the repair checkpoint")
    old_results = {r["id"]: r for r in original["modules"]}
    for mid in retained:
        for key in ("olean_sha256", "built_source_sha256", "patch_sha256"):
            if results[mid][key] != old_results[mid][key]:
                fail(f"Retained target differs from original output: {mid}")
    return SimpleNamespace(text=original_text, original=original, retained=retained)


def preflight(args, h, require_phase1):
    tree, prior = args.phase1 / "lean", args.phase1_evidence or args.phase1 / "evidence"
    work = args.work or (tree / ".lake/packages").resolve().parents[2]
    selection_file = prior / "selection.json"
    if not selection_file.is_file():
        selection_file = work / f"{PACKET}-selection.json"
    selection = json.loads(selection_file.read_text())
    manifest_file = args.inputs / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    if selection["packet"] != PACKET or manifest["upstream_commit"] != ACCEPTED:
        fail("Wrong packet or extension accepted commit")
    if set(manifest["target_ids"]) != NEW_IDS:
        fail("Extension target set differs from the reviewed 15 targets")
    if driver.git(args.repo, "rev-parse", "HEAD").decode().strip() != ACCEPTED:
        fail(f"--repo must be checked out at accepted commit {ACCEPTED}")
    pins = h.build.target()
    if pins != selection["pins"]:
        fail("Port target changed since phase 1")
    driver.require_hash(args.repo / "tracker/modules.jsonl.gz", manifest["ledger_sha256"], h)
    driver.require_hash(args.repo / "tracker/modules.jsonl.gz", selection["ledger_sha256"], h)
    by_id = h.ci.load_ledger()
    closure_ids = h.ci.transitive_closure(sorted(NEW_IDS), by_id)
    indexed = {r["id"]: r for r in manifest["files"]}
    if set(indexed) != set(closure_ids) or len(indexed) != len(manifest["files"]):
        fail("Staged sources are not exactly the extension import closure")
    for mid in closure_ids:
        row, entry = by_id[mid], indexed[mid]
        rel = h.build.source_relpath(row)
        if (entry["module"] != row["module"] or entry["relative_path"] != rel
                or entry["source_sha256"] != row["source_sha256"]
                or entry["role"] != ("target" if mid in NEW_IDS else "import")):
            fail(f"Staged metadata mismatch: {mid}")
        driver.require_hash(args.inputs / rel, row["source_sha256"], h)
    old_rows = selection["selected"] + selection["imports"]
    old_ids = {r["id"] for r in old_rows}
    old_targets = {r["id"] for r in selection["selected"]}
    if len(old_targets) != 48 or len(old_ids) != 112 or NEW_IDS & old_ids:
        fail("Phase-1 selection differs from the reviewed 48-target/64-import scope")
    tracked = set(driver.git(args.repo, "ls-tree", "-r", "--name-only", ACCEPTED, "--", "port/patches").decode().splitlines())
    accepted_ids = {Path(p).stem for p in tracked if re.fullmatch(r"port/patches/M\d{6}\.json", p)}
    if (NEW_IDS | old_targets) & accepted_ids:
        fail("A requested target already has an accepted upstream record")
    compatibility = dict(baseline_commit=selection["accepted_commit"], accepted_commit=ACCEPTED,
                         files={}, existing_import_records=[])
    names = set(selection["helper_sha256"]) | {"port/target.json", "port/pruned.txt", "tracker/modules.jsonl.gz"}
    for name in sorted(names):
        old = driver.git(args.repo, "show", f"{selection['accepted_commit']}:{name}")
        new = driver.git(args.repo, "show", f"{ACCEPTED}:{name}")
        if old != new or (args.repo / name).read_bytes() != new:
            fail(f"Accepted baseline compatibility changed: {name}")
        if name in selection["helper_sha256"] and digest(new) != selection["helper_sha256"][name]:
            fail(f"Phase-1 helper fingerprint differs from its accepted source: {name}")
        compatibility["files"][name] = dict(identical=True, sha256=digest(new))
    for record in selection["accepted_import_records"]:
        mid = record["id"]
        for suffix, key in (("json", "record_sha256"), ("patch", "patch_sha256")):
            rel = f"port/patches/{mid}.{suffix}"
            data = driver.git(args.repo, "show", f"{ACCEPTED}:{rel}") if rel in tracked else None
            observed = digest(data) if data is not None else None
            if observed != record[key]:
                fail(f"Previously accepted import changed: {rel}")
            if data is not None and (args.repo / rel).read_bytes() != data:
                fail(f"Local accepted import differs from pinned Git blob: {rel}")
        compatibility["existing_import_records"].append(dict(id=mid, identical=True,
            record_sha256=record["record_sha256"], patch_sha256=record["patch_sha256"]))
    imports = [by_id[mid] for mid in closure_ids if mid not in NEW_IDS]
    import_records = []
    for row in imports:
        mid = row["id"]
        if mid not in accepted_ids:
            fail(f"Extension has an unaccepted project import: {mid}")
        path = args.repo / "port/patches" / f"{mid}.json"
        patch = path.with_suffix(".patch")
        for file in (path, patch):
            rel = file.relative_to(args.repo).as_posix()
            if rel in tracked:
                if file.read_bytes() != driver.git(args.repo, "show", f"{ACCEPTED}:{rel}"):
                    fail(f"Accepted input changed: {rel}")
            elif file.exists():
                fail(f"Unexpected unaccepted import patch: {file}")
        driver.validate_record(row, json.loads(path.read_text()), patch, pins, h)
        import_records.append(dict(id=mid, module=row["module"], source_sha256=row["source_sha256"],
            record_sha256=h.tasks.digest(path), patch_sha256=h.tasks.digest(patch) if patch.exists() else None))
    for name, value in selection["project_config"].items():
        driver.require_hash(tree / name, value, h)
    if (tree / "lean-toolchain").read_text().strip() != pins["lean"]:
        fail("Current project toolchain is not the port target")
    mathlib = tree / ".lake/packages/mathlib"
    if driver.git(mathlib, "rev-parse", "HEAD").decode().strip() != pins["mathlib"]:
        fail("Current Mathlib HEAD is not the port target")
    if driver.git(mathlib, "status", "--porcelain", "--untracked-files=no").strip():
        fail("Mathlib has tracked modifications")
    import tomllib
    libraries = tomllib.loads((tree / "lakefile.toml").read_text())["lean_lib"]
    for mid in closure_ids:
        module = by_id[mid]["module"]
        if not any(module in lib.get("roots", []) or module == lib["name"] or
                   any(g.endswith(".+") and module.startswith(g[:-1]) for g in lib.get("globs", []))
                   for lib in libraries):
            fail(f"Existing Lake configuration does not cover {module}")
    pruned = h.port.pruned()
    remaining = [dict(id=r["id"], module=r["module"],
                      missing_imports=[dep for dep in r["imports"] if dep not in accepted_ids | old_targets | NEW_IDS])
                 for r in by_id.values() if r["packet"] == PACKET and r["id"] not in accepted_ids | old_targets | NEW_IDS | pruned]
    if {r["id"] for r in remaining} != {"M133708"}:
        fail("Remaining packet scope differs from the reviewed M133708 blocker")
    new_rows = [by_id[mid] for mid in closure_ids if mid not in old_ids]
    for row in new_rows:
        if (tree / h.build.source_relpath(row)).exists() or object_path(tree, row).exists():
            fail(f"New extension source or object already exists: {row['id']}; review before retrying")
    receipt_path = prior / f"{PACKET}-receipt.json"
    phase1 = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
    ready = bool(phase1 and phase1.get("all_successful"))
    reuse = None
    if phase1:
        if not ready or phase1["accepted_commit"] != selection["accepted_commit"]:
            fail("Phase 1 is not a successful matching build")
        repaired = phase1["schema"] == "rh-port-repaired-batch-receipt-v1"
        if phase1["schema"] not in ("rh-port-mirror-batch-receipt-v1", "rh-port-repaired-batch-receipt-v1") or phase1["fresh_project_objects"] is not (not repaired):
            fail("Unexpected phase-1 receipt schema or freshness claim")
        driver.require_hash(selection_file, phase1["selection_sha256"], h)
        driver.require_hash(prior / "batch-build.log", phase1["batch_build"]["log_sha256"], h)
        driver.require_hash(prior / f"{PACKET}-build.log", phase1["build_log_sha256"], h)
        driver.require_hash(prior / "islattice-audit.log", phase1["aggregate_islattice_audit"]["log_sha256"], h)
        driver.require_hash(prior / "islattice_audit.lean", phase1["islattice_audit_sha256"], h)
        driver.require_hash(Path(driver.__file__), phase1["driver_sha256"], h)
        text = (prior / "batch-build.log").read_text(errors="replace")
        if phase1["batch_build"]["exit_code"] != 0 or h.ci.SORRY_DIAG_RE.search(text):
            fail("Phase-1 shared transcript is not a successful sorry-free build")
        if phase1["aggregate_islattice_audit"]["exit_code"] != 0 or "islattice-audit: 48 module(s), 0 hit(s)" not in (prior / "islattice-audit.log").read_text().splitlines():
            fail("Phase-1 aggregate audit did not report 48 modules and zero hits")
        if phase1["mathlib"] != pins["mathlib"] or not phase1["toolchain"].startswith("Lean (version " + pins["lean"].rsplit(":v", 1)[-1] + ","):
            fail("Phase-1 compiler pins do not match")
        results = {r["id"]: r for r in phase1["modules"]}
        if set(results) != old_targets or not all(r["successful"] for r in results.values()):
            fail("Phase-1 target results do not match the frozen 48 targets")
        chain = repaired_chain(args, h, prior, tree, phase1, selection, old_rows, results, by_id) if repaired else None
        old_import_records = {r["id"]: r for r in selection["accepted_import_records"]}
        for row in old_rows:
            mid = row["id"]
            producing_text = chain.text if repaired and (mid not in results or mid in chain.retained) else text
            if row != by_id[mid] or not built_lines(producing_text, row["module"]):
                fail(f"Missing phase-1 producing Built evidence or changed ledger row: {mid}")
            expected = results[mid]["built_source_sha256"] if mid in results else old_import_records[mid]["built_source_sha256"]
            driver.require_hash(tree / h.build.source_relpath(row), expected, h)
            if mid in results:
                record = results[mid]
                if record["source_sha256"] != row["source_sha256"]:
                    fail(f"Phase-1 pinned source mismatch: {mid}")
                if record["built_lines"] != built_lines(producing_text, row["module"]):
                    fail(f"Phase-1 target Built excerpt differs from its producing log: {mid}")
                if repaired:
                    attempt = "original" if mid in chain.retained else "retry"
                    producing = chain.original["batch_build"] if attempt == "original" else phase1["batch_build"]
                    if record["compilation_attempt"] != attempt or record["compilation_log_sha256"] != producing["log_sha256"] or record["compilation_batch_exit_code"] != producing["exit_code"]:
                        fail(f"Phase-1 producing attempt mismatch: {mid}")
                if record["patch_sha256"]:
                    patch = prior / "patches" / f"{mid}.patch" if repaired else args.repo / "port/patches" / f"{mid}.patch"
                    driver.require_hash(patch, record["patch_sha256"], h)
                    from verify_p0208_evidence import apply_diff
                    source = work / "pinned-sources" / h.build.source_relpath(row)
                    driver.require_hash(source, row["source_sha256"], h)
                    if digest(apply_diff(source.read_bytes(), patch.read_bytes(), h.build.source_relpath(row))) != record["built_source_sha256"]:
                        fail(f"Phase-1 target patch does not reconstruct the compiled source: {mid}")
                elif record["built_source_sha256"] != row["source_sha256"]:
                    fail(f"Phase-1 target changed source without a recorded patch: {mid}")
                driver.require_hash(object_path(tree, row), record["olean_sha256"], h)
                driver.require_hash(prior / "logs" / f"{mid}.log", record["log_sha256"], h)
        reuse = snapshot(tree, old_rows, h)
    elif require_phase1:
        fail(f"Wait for the successful phase-1 receipt: {receipt_path}")
    compatibility.update(all_compatible=True, existing_import_count=64,
                         extension_import_count=len(imports), new_source_count=len(new_rows),
                         reused_extension_import_ids=sorted(set(closure_ids) & old_ids),
                         new_import_patch_ids=[r["id"] for r in import_records if r["id"] not in old_ids and r["patch_sha256"]])
    return SimpleNamespace(tree=tree, prior=prior, work=work, pins=pins, selection=selection,
        selection_file=selection_file, manifest=manifest, manifest_file=manifest_file, phase1=phase1,
        phase1_ready=ready, receipt_path=receipt_path, compatibility=compatibility, reuse=reuse,
        old_rows=old_rows, new_rows=new_rows, targets=[by_id[mid] for mid in sorted(NEW_IDS)],
        imports=imports, import_records=import_records, remaining=remaining)


def execute(args, h, ctx):
    if args.out.exists():
        fail(f"Output already exists: {args.out}")
    env = driver.environment(SimpleNamespace(work=ctx.work, lean_threads=args.lean_workers), h)
    version, mathlib = h.build.toolchain(ctx.tree, env, ctx.pins)
    args.out.mkdir(parents=True)
    (args.out / "logs").mkdir()
    driver.write_json(args.out / "reuse-before.json", ctx.reuse)
    driver.write_json(args.out / "compatibility.json", ctx.compatibility)
    shutil.copyfile(ctx.receipt_path, args.out / "phase1-receipt.json")
    shutil.copyfile(ctx.manifest_file, args.out / "inputs-manifest.json")
    shutil.copyfile(ctx.selection_file, args.out / "phase1-selection.json")
    source_method = "Continuation in the same verified project tree: retained phase-1 outputs plus SHA-256-verified pinned mirror sources and newly accepted import patches."
    applied = []
    patch_hashes = {}
    for row in ctx.new_rows:
        rel = h.build.source_relpath(row)
        dest = ctx.tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.inputs / rel, dest)
        driver.require_hash(dest, row["source_sha256"], h)
        patch = args.repo / "port/patches" / f"{row['id']}.patch"
        patch_hashes[row["id"]] = h.tasks.digest(patch) if patch.exists() else None
        if patch.exists():
            errors = list(h.build.validate_patch_content(patch, rel))
            if errors:
                fail("\n".join(errors))
            print(h.build.apply_patch(ctx.tree, patch), flush=True)
            applied.append(dict(id=row["id"], patch_sha256=h.tasks.digest(patch)))
    closure = ctx.old_rows + ctx.new_rows
    sources_before = {r["id"]: h.tasks.digest(ctx.tree / h.build.source_relpath(r)) for r in closure}
    launcher = ["elan", "run", ctx.pins["lean"]]
    command = launcher + ["lake", "--no-ansi", "--no-cache", "build"] + [f"+{r['module']}:olean" for r in ctx.targets]
    print(f"Building {len(ctx.targets)} new targets with {len(ctx.new_rows) - len(ctx.targets)} new imports; preserving 112 prior project outputs.", flush=True)
    batch = driver.run_log(command, ctx.tree, env, args.out / "batch-build.log", measure=True)
    batch["log_sha256"] = h.tasks.digest(args.out / "batch-build.log")
    text = (args.out / "batch-build.log").read_text(errors="replace")
    sorry = bool(h.ci.SORRY_DIAG_RE.search(text))
    completion = [line for line in text.splitlines() if line.startswith("Build completed successfully")]
    built = {r["id"]: built_lines(text, r["module"]) for r in ctx.new_rows}
    unexpected_rebuilds = [r["id"] for r in ctx.old_rows if built_lines(text, r["module"])]
    build_ok = batch["exit_code"] == 0 and not sorry and bool(completion) and not unexpected_rebuilds and all(
        built[r["id"]] and object_path(ctx.tree, r).is_file() for r in ctx.new_rows)
    audit_source = args.out / "islattice_audit.lean"
    shutil.copyfile(args.repo / "port/islattice_audit.lean", audit_source)
    all_targets = sorted(ctx.selection["selected"] + ctx.targets, key=lambda r: r["id"])
    audit = dict(exit_code=None, status="not run because continuation build failed an evidence check")
    if build_ok:
        command = launcher + ["lake", "--no-ansi", "env", "lean", "--run", str(audit_source)] + [r["module"] for r in all_targets]
        audit = driver.run_log(command, ctx.tree, env, args.out / "islattice-audit.log", measure=True)
        audit["log_sha256"] = h.tasks.digest(args.out / "islattice-audit.log")
        audit["complete_zero_hit_summary"] = "islattice-audit: 63 module(s), 0 hit(s)" in (args.out / "islattice-audit.log").read_text().splitlines()
    audit_ok = audit["exit_code"] == 0 and audit.get("complete_zero_hit_summary", False)
    reuse_after = snapshot(ctx.tree, ctx.old_rows, h)
    driver.write_json(args.out / "reuse-after.json", reuse_after)
    changed_reused = [mid for mid in ctx.reuse if ctx.reuse[mid] != reuse_after[mid]]
    sources_after = {r["id"]: h.tasks.digest(ctx.tree / h.build.source_relpath(r)) for r in closure}
    changed_sources = [mid for mid in sources_before if sources_before[mid] != sources_after[mid]]
    changed_config = [name for name, value in ctx.selection["project_config"].items() if h.tasks.digest(ctx.tree / name) != value]
    changed_patches = [mid for mid, expected in patch_hashes.items()
                       if (h.tasks.digest(args.repo / "port/patches" / f"{mid}.patch")
                           if (args.repo / "port/patches" / f"{mid}.patch").exists() else None) != expected]
    mathlib_unchanged = driver.git(ctx.tree / ".lake/packages/mathlib", "rev-parse", "HEAD").decode().strip() == mathlib and not driver.git(ctx.tree / ".lake/packages/mathlib", "status", "--porcelain", "--untracked-files=no").strip()
    stable = not changed_reused and not changed_sources and not changed_config and not changed_patches and bool(mathlib_unchanged)
    rows = []
    for row in ctx.targets:
        mid = row["id"]
        ok = bool(build_ok and audit_ok and stable and built[mid] and object_path(ctx.tree, row).is_file())
        patch = args.repo / "port/patches" / f"{mid}.patch"
        log = args.out / "logs" / f"{mid}.log"
        log.write_text("\n".join([f"{mid} {row['module']}",
            "Target excerpt from one shared continuation build; this is not an independent invocation.",
            f"Full command and transcript: ../batch-build.log (SHA-256 {batch['log_sha256']})",
            *built[mid], *completion, f"Shared build exit: {batch['exit_code']}",
            f"Aggregate audit over all 63 packet targets exit: {audit['exit_code']}; transcript: ../islattice-audit.log",
            f"All retained outputs/sources/configuration preserved: {stable}", f"Module evidence successful: {ok}", ""]))
        rows.append(dict(id=mid, module=row["module"], successful=ok, build_mode="shared_continuation_batch",
            source_sha256=row["source_sha256"], built_source_sha256=sources_after[mid],
            patch_sha256=patch_hashes[mid],
            olean_sha256=h.tasks.digest(object_path(ctx.tree, row)) if object_path(ctx.tree, row).is_file() else None,
            built_lines=built[mid], shared_build_exit_code=batch["exit_code"],
            log_sha256=h.tasks.digest(log), shared_log_sha256=batch["log_sha256"],
            islattice_audit=dict(scope="all 63 packet targets", exit_code=audit["exit_code"], successful=audit_ok,
                                log_sha256=audit.get("log_sha256")),
            reused_names=[f"{n}:{name}" for n, name in h.build.reused_names((ctx.tree / h.build.source_relpath(row)).read_text())]))
    for result in (batch, audit):
        sidecar = result.get("resources", {}).get("gnu_time_sidecar")
        if sidecar:
            result["resources"]["gnu_time_sidecar_sha256"] = h.tasks.digest(args.out / sidecar)
    successful = sum(r["successful"] for r in rows)
    receipt = dict(schema="rh-port-continuation-receipt-v1", target=PACKET, issue=ctx.phase1.get("issue"),
        source_method=source_method, source_base_url=ctx.manifest["source_base_url"], accepted_commit=ACCEPTED,
        fresh_project_objects=False, new_project_module_count=len(ctx.new_rows), retained_project_module_count=112,
        phase1=dict(receipt_path=str(ctx.receipt_path), receipt_sha256=h.tasks.digest(ctx.receipt_path),
                    batch_log_sha256=ctx.phase1["batch_build"]["log_sha256"],
                    audit_log_sha256=ctx.phase1["aggregate_islattice_audit"]["log_sha256"],
                    target_ids=[r["id"] for r in ctx.selection["selected"]]),
        toolchain=version, mathlib=mathlib, project_config=ctx.selection["project_config"],
        lean_num_threads_environment=args.lean_workers,
        concurrency_note="LEAN_NUM_THREADS is the Lake worker budget, not the per-compiler thread count.",
        started_utc=batch["started_utc"], ended_utc=driver.utc(), host=h.tasks.host_resources(),
        batch_build=batch, aggregate_islattice_audit=audit, modules=rows, accepted_imports=ctx.import_records,
        built_imports=[dict(id=r["id"], module=r["module"], built_lines=built[r["id"]],
                           built_source_sha256=sources_after[r["id"]],
                           olean_sha256=h.tasks.digest(object_path(ctx.tree, r)) if object_path(ctx.tree, r).is_file() else None)
                       for r in ctx.new_rows if r["id"] not in NEW_IDS],
        applied_patches=applied, unexpected_rebuilt_existing=unexpected_rebuilds,
        changed_reused_outputs=changed_reused, changed_sources=changed_sources, changed_configuration=changed_config,
        changed_patches=changed_patches,
        mathlib_unchanged=bool(mathlib_unchanged), remaining=ctx.remaining,
        combined_target_count=48 + successful, packet_complete=False,
        input_manifest_sha256=h.tasks.digest(ctx.manifest_file), ledger_sha256=ctx.manifest["ledger_sha256"],
        compatibility=ctx.compatibility, compatibility_sha256=h.tasks.digest(args.out / "compatibility.json"),
        reuse_before_sha256=h.tasks.digest(args.out / "reuse-before.json"), reuse_after_sha256=h.tasks.digest(args.out / "reuse-after.json"),
        islattice_audit_sha256=h.tasks.digest(audit_source), driver_sha256=h.tasks.digest(Path(driver.__file__)),
        extension_driver_sha256=h.tasks.digest(Path(__file__)), all_successful=successful == len(rows))
    driver.write_json(args.out / "results.json", rows)
    driver.write_json(args.out / f"{PACKET}-extension-receipt.json", receipt)
    print(f"Continuation complete: {successful}/15 new targets; retained 48 phase-1 targets; remaining M133708. Evidence: {args.out}", flush=True)
    return 0 if receipt["all_successful"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "phase1", "inputs", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--work", type=Path, help="Prepared toolchain/cache work root; inferred from dependency-package link by default")
    parser.add_argument("--phase1-evidence", type=Path, help="Successful original or explicit repaired checkpoint; defaults to PHASE1/evidence")
    parser.add_argument("--lean-workers", type=int, default=4)
    parser.add_argument("--plan", "--dry-run", action="store_true", help="Read-only checks; no Lake commands or writes")
    args = parser.parse_args()
    for name in ("repo", "phase1", "inputs", "out", "work", "phase1_evidence"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    if args.lean_workers < 1:
        fail("--lean-workers must be positive")
    h = driver.helpers(args.repo)
    ctx = preflight(args, h, require_phase1=not args.plan)
    if args.plan:
        print(json.dumps(dict(phase1_ready=ctx.phase1_ready, accepted_commit=ACCEPTED,
            old_project_modules=len(ctx.old_rows), new_targets=len(ctx.targets), new_sources=len(ctx.new_rows),
            extension_imports=len(ctx.imports), audit_target_count=63,
            compatibility=ctx.compatibility, remaining=ctx.remaining), indent=2))
        return 0
    return execute(args, h, ctx)


if __name__ == "__main__":
    sys.exit(main())
