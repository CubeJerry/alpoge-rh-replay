#!/usr/bin/env python3
"""Repair two failed P0208 extension targets, preserving all prior evidence.

--plan is read-only. Execution requires pristine failed-target sources and a new
output directory; it never applies a patch twice and never writes port records.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
import packet_driver as driver
import packet_extension as extension
import packet_repair as repair

SCHEMA = "rh-port-repaired-continuation-receipt-v1"
REPAIRS = {"M131597", "M131764"}
M131764_PIN = "6e54cbb0fbd6e056155e66328795bcc7402538b658013da311e307cb5098d79f"
M131764_DELETIONS = [dict(original_line=32, text="      | zero => simp\n"),
                    dict(original_line=263, text="  | zero => simp\n")]
M131764_EQUIVALENCE = (
    "The dualDer data map and declared type remain byte-identical. Only the "
    "Prop-valued Leibniz-law proof passed to Derivation.mk' changes, so proof "
    "irrelevance preserves the derivation. TensorProduct.inductionOn now provides "
    "tmul and add cases and obtains zero from 0 tensor 0; deleting the obsolete "
    "zero alternatives preserves the theorem statements and definition data.")
fail, digest, files = repair.fail, repair.digest, repair.files
built_lines, object_path, snapshot = extension.built_lines, extension.object_path, extension.snapshot


def load(path):
    return json.loads(path.read_text())


def preflight(args, h):
    tree, prior, p1dir = args.phase1 / "lean", args.original, args.phase1_evidence
    work = args.work or (tree / ".lake/packages").resolve().parents[2]
    original_path = prior / "P0208-extension-receipt.json"
    original = load(original_path)
    p1path = p1dir / "P0208-receipt.json"
    p1, selection = load(p1path), load(p1dir / "selection.json")
    manifest = load(args.inputs / "manifest.json")
    pins = h.build.target()
    if driver.git(args.repo, "rev-parse", "HEAD").decode().strip() != extension.ACCEPTED:
        fail("--repo must remain at the accepted extension baseline")
    if original["schema"] != "rh-port-continuation-receipt-v1" or original["accepted_commit"] != extension.ACCEPTED or original["fresh_project_objects"] is not False or original["all_successful"] or original["batch_build"]["exit_code"] in (None, 0):
        fail("Require the completed unsuccessful original extension attempt")
    if p1["schema"] != "rh-port-repaired-batch-receipt-v1" or not p1["all_successful"] or pins != selection["pins"]:
        fail("Require the successful repaired phase-1 checkpoint on target pins")
    if original["phase1"]["receipt_path"] != str(p1path):
        fail("Original extension identifies a different phase-1 receipt")
    driver.require_hash(p1path, original["phase1"]["receipt_sha256"], h)
    driver.require_hash(prior / "phase1-receipt.json", original["phase1"]["receipt_sha256"], h)
    if load(prior / "phase1-selection.json") != selection:
        fail("Original extension's phase-1 selection changed")
    driver.require_hash(p1dir / "selection.json", p1["selection_sha256"], h)
    for name, expected in selection["project_config"].items():
        driver.require_hash(tree / name, expected, h)
    if original["project_config"] != p1["project_config"] or original["project_config"] != selection["project_config"]:
        fail("Project configuration provenance differs")
    for name, expected in selection["helper_sha256"].items():
        driver.require_hash(args.repo / name, expected, h)
    driver.require_hash(Path(driver.__file__), original["driver_sha256"], h)
    driver.require_hash(Path(extension.__file__), original["extension_driver_sha256"], h)
    driver.require_hash(Path(repair.__file__), p1["repair_driver_sha256"], h)
    driver.require_hash(args.repo / "tracker/modules.jsonl.gz", original["ledger_sha256"], h)
    if original["ledger_sha256"] != selection["ledger_sha256"] or manifest["ledger_sha256"] != original["ledger_sha256"]:
        fail("Ledger fingerprints differ")
    driver.require_hash(args.repo / "port/target.json", selection["target_sha256"], h)
    for name, expected in (("batch-build.log", original["batch_build"]["log_sha256"]),
                           ("compatibility.json", original["compatibility_sha256"]),
                           ("reuse-before.json", original["reuse_before_sha256"]),
                           ("reuse-after.json", original["reuse_after_sha256"]),
                           ("inputs-manifest.json", original["input_manifest_sha256"]),
                           ("islattice_audit.lean", original["islattice_audit_sha256"])):
        driver.require_hash(prior / name, expected, h)
    driver.require_hash(args.inputs / "manifest.json", original["input_manifest_sha256"], h)
    if load(prior / "compatibility.json") != original["compatibility"] or not original["compatibility"]["all_compatible"]:
        fail("Original accepted-import compatibility provenance differs")
    for key in ("unexpected_rebuilt_existing", "changed_reused_outputs", "changed_sources", "changed_configuration", "changed_patches"):
        if original[key]:
            fail(f"Original extension altered retained inputs: {key}")
    if not original["mathlib_unchanged"] or not p1["mathlib_unchanged"] or original["mathlib"] != pins["mathlib"] or original["toolchain"] != p1["toolchain"]:
        fail("Compiler/Mathlib provenance differs")
    mathlib = tree / ".lake/packages/mathlib"
    if driver.git(mathlib, "rev-parse", "HEAD").decode().strip() != pins["mathlib"] or driver.git(mathlib, "status", "--porcelain", "--untracked-files=no").strip():
        fail("Mathlib changed since compilation")
    for name, value in (("batch-build.log", p1["batch_build"]["log_sha256"]),
                         ("islattice-audit.log", p1["aggregate_islattice_audit"]["log_sha256"])):
        driver.require_hash(p1dir / name, value, h)
    if p1["batch_build"]["exit_code"] != 0 or p1["aggregate_islattice_audit"]["exit_code"] != 0 or "islattice-audit: 48 module(s), 0 hit(s)" not in (p1dir / "islattice-audit.log").read_text().splitlines():
        fail("Phase-1 successful build/audit is missing")
    ledger = h.ci.load_ledger()
    old_rows = selection["selected"] + selection["imports"]
    old_ids = {r["id"] for r in old_rows}
    target_ids = set(manifest["target_ids"])
    if target_ids != extension.NEW_IDS or manifest["upstream_commit"] != extension.ACCEPTED:
        fail("Wrong extension target/input baseline")
    closure = set(h.ci.transitive_closure(sorted(target_ids), ledger))
    entries = {r["id"]: r for r in manifest["files"]}
    if set(entries) != closure or len(entries) != len(manifest["files"]):
        fail("Extension source manifest is not its exact import closure")
    for mid, entry in entries.items():
        row, rel = ledger[mid], h.build.source_relpath(ledger[mid])
        if entry["module"] != row["module"] or entry["source_sha256"] != row["source_sha256"] or entry["relative_path"] != rel:
            fail(f"Extension manifest row differs: {mid}")
        driver.require_hash(args.inputs / rel, row["source_sha256"], h)
    new_rows = [ledger[mid] for mid in sorted(closure - old_ids)]
    targets = [ledger[mid] for mid in sorted(target_ids)]
    rows = old_rows + new_rows
    all_target_ids = target_ids | {r["id"] for r in selection["selected"]}
    imports = {r["id"] for r in selection["imports"]} | (closure - target_ids)
    if len(old_ids) != 112 or len(new_rows) != 50 or len(rows) != 162 or len(imports) != 99 or len(all_target_ids) != 63:
        fail("Unexpected 112+50 source / 99 import / 63 target scope")
    if set(h.ci.transitive_closure(sorted(all_target_ids), ledger)) != {r["id"] for r in rows} or any(ledger[r["id"]] != r for r in rows):
        fail("Combined source closure differs from the pinned ledger")
    edges = [[r["id"], dep] for r in rows for dep in sorted(set(h.ci.transitive_closure(r["imports"], ledger)) & REPAIRS)]
    if edges:
        fail(f"Retaining outputs is unsafe: a project module depends on a repaired target: {edges}")
    p1results = {r["id"]: r for r in p1["modules"]}
    if set(p1results) != all_target_ids - target_ids or not all(r["successful"] for r in p1results.values()):
        fail("Phase-1 target result scope differs")
    extension.repaired_chain(args, h, p1dir, tree, p1, selection, old_rows, p1results, ledger)
    old_snapshot = snapshot(tree, old_rows, h)
    if load(prior / "reuse-before.json") != old_snapshot or load(prior / "reuse-after.json") != old_snapshot:
        fail("Original extension's 112 retained outputs changed")
    accepted_sources = {}
    for records, commit, pinned in ((selection["accepted_import_records"], selection["accepted_commit"], work / "pinned-sources"),
                                     (original["accepted_imports"], extension.ACCEPTED, args.inputs)):
        tracked = set(driver.git(args.repo, "ls-tree", "-r", "--name-only", commit, "--", "port/patches").decode().splitlines())
        for record in records:
            mid, row = record["id"], ledger[record["id"]]
            rel = h.build.source_relpath(row)
            source = pinned / rel
            driver.require_hash(source, row["source_sha256"], h)
            recpath = args.repo / "port/patches" / f"{mid}.json"
            patch = recpath.with_suffix(".patch")
            for path, key in ((recpath, "record_sha256"), (patch, "patch_sha256")):
                relative = path.relative_to(args.repo).as_posix()
                blob = driver.git(args.repo, "show", f"{commit}:{relative}") if relative in tracked else None
                if (digest(blob) if blob is not None else None) != record[key] or (path.read_bytes() if path.exists() else None) != blob:
                    fail(f"Accepted import input changed: {mid} {key}")
            driver.validate_record(row, load(recpath), patch, pins, h)
            expected = repair.apply_diff(source.read_bytes(), patch.read_bytes(), rel) if patch.exists() else source.read_bytes()
            if mid in accepted_sources and accepted_sources[mid] != expected:
                fail(f"Overlapping accepted import differs between baselines: {mid}")
            if record.get("built_source_sha256", digest(expected)) != digest(expected):
                fail(f"Accepted import compiled-source fingerprint differs: {mid}")
            accepted_sources[mid] = expected
    if set(accepted_sources) != imports:
        fail("Accepted records do not cover exactly all 99 project imports")
    originals, source_original = {}, {}
    for row in rows:
        mid, rel = row["id"], h.build.source_relpath(row)
        if mid in accepted_sources:
            expected = accepted_sources[mid]
        elif mid in p1results:
            record = p1results[mid]
            pinned = work / "pinned-sources" / rel
            driver.require_hash(pinned, row["source_sha256"], h)
            expected = pinned.read_bytes()
            if record["patch_sha256"]:
                patch = p1dir / "patches" / f"{mid}.patch"
                driver.require_hash(patch, record["patch_sha256"], h)
                expected = repair.apply_diff(expected, patch.read_bytes(), rel)
            if digest(expected) != record["built_source_sha256"]:
                fail(f"Phase-1 target reconstruction differs: {mid}")
            driver.require_hash(object_path(tree, row), record["olean_sha256"], h)
        else:
            originals[mid] = expected = (args.inputs / rel).read_bytes()
        if (tree / rel).read_bytes() != expected:
            fail(f"Source changed since the failed extension; require pristine targets: {mid}")
        source_original[mid] = digest(expected)
    original_text = (prior / "batch-build.log").read_text()
    command = ["elan", "run", pins["lean"], "lake", "--no-ansi", "--no-cache", "build"] + [f"+{r['module']}:olean" for r in targets]
    if original["batch_build"]["command"] != command or h.ci.SORRY_DIAG_RE.search(original_text) or f"== exit_code: {original['batch_build']['exit_code']}" not in original_text.splitlines():
        fail("Original continuation command/exit differs or contains a sorry diagnostic")
    if original["aggregate_islattice_audit"]["exit_code"] is not None or (prior / "islattice-audit.log").exists():
        fail("The failed original extension must not claim an audit")
    results = {r["id"]: r for r in original["modules"]}
    if set(results) != target_ids or load(prior / "results.json") != original["modules"]:
        fail("Original extension result scope differs")
    retained, rebuilt = [], []
    for row in targets:
        mid, record = row["id"], results[row["id"]]
        if record["source_sha256"] != row["source_sha256"] or record["built_source_sha256"] != row["source_sha256"] or record["patch_sha256"] is not None or record["successful"]:
            fail(f"Original target is not the failed-batch unchanged source: {mid}")
        driver.require_hash(prior / "logs" / f"{mid}.log", record["log_sha256"], h)
        observed = built_lines(original_text, row["module"])
        if observed != record["built_lines"] or record["shared_log_sha256"] != original["batch_build"]["log_sha256"]:
            fail(f"Original target producing transcript mismatch: {mid}")
        if observed and record["olean_sha256"]:
            driver.require_hash(object_path(tree, row), record["olean_sha256"], h)
            retained.append(mid)
        else:
            if observed or record["olean_sha256"] is not None or object_path(tree, row).exists():
                fail(f"Inconsistent failed-target object evidence: {mid}")
            rebuilt.append(mid)
    if set(rebuilt) != REPAIRS or len(retained) != 13:
        fail("Original result is not the reviewed 13 retained / 2 failed target split")
    built_imports = {r["id"]: r for r in original["built_imports"]}
    if set(built_imports) != (closure - old_ids - target_ids) or len(built_imports) != 35:
        fail("Original new-import producer scope differs")
    for mid, record in built_imports.items():
        row = ledger[mid]
        if not record["built_lines"] or record["built_lines"] != built_lines(original_text, row["module"]) or record["built_source_sha256"] != source_original[mid]:
            fail(f"Original new import lacks valid producing evidence: {mid}")
        driver.require_hash(object_path(tree, row), record["olean_sha256"], h)
    reuse = snapshot(tree, [r for r in rows if r["id"] not in REPAIRS], h)
    if len(reuse) != 160:
        fail("Expected 160 retained project objects")
    notes = load(args.notes or args.patches / "notes.json")
    if {p.stem for p in args.patches.glob("M*.patch")} != REPAIRS or set(notes) != REPAIRS:
        fail("Candidate folder and notes must contain exactly the two failed extension targets")
    patches, patched, reviews = {}, {}, {}
    for mid in sorted(REPAIRS):
        row, path = ledger[mid], args.patches / f"{mid}.patch"
        errors = list(h.build.validate_patch_content(path, h.build.source_relpath(row)))
        if errors:
            fail("\n".join(errors))
        if not isinstance(notes[mid], str) or "proof" not in notes[mid].lower() or "statement" not in notes[mid].lower():
            fail(f"{mid}: notes must explain proof changes and preserved statements")
        patches[mid] = path.read_bytes()
        patched[mid] = repair.apply_diff(originals[mid], patches[mid], h.build.source_relpath(row))
        before, after = repair.proof_skeleton(originals[mid]), repair.proof_skeleton(patched[mid])
        review = dict(notes=notes[mid], nonproof_skeleton_sha256=digest(before),
            statement_and_definition_text_unchanged=True, source_sha256=row["source_sha256"],
            built_source_sha256=digest(patched[mid]), patch_sha256=digest(patches[mid]))
        if mid == "M131764":
            if row["source_sha256"] != M131764_PIN or digest(originals[mid]) != M131764_PIN:
                fail("M131764 proof-field exception requires its exact reviewed pinned source")
            lines = originals[mid].decode().splitlines(keepends=True)
            removed = {change["original_line"] for change in M131764_DELETIONS}
            for change in M131764_DELETIONS:
                if lines[change["original_line"] - 1] != change["text"]:
                    fail("M131764 reviewed deletion differs from its original complete line")
            transformed = "".join(line for number, line in enumerate(lines, 1) if number not in removed).encode()
            if transformed != patched[mid]:
                fail("M131764 differs from the exact two reviewed full-line deletions")
            review.update(review_method="exact_reviewed_line_deletions", reviewed_deletions=M131764_DELETIONS,
                equivalence_note=M131764_EQUIVALENCE, statement_and_definition_text_unchanged=False,
                theorem_statement_text_unchanged=True, definition_and_instance_data_unchanged=True,
                patched_nonproof_skeleton_sha256=digest(after))
        elif before != after:
            fail(f"{mid}: changes outside a theorem/lemma proof")
        if patched[mid] == originals[mid] or h.build.reused_names(patched[mid].decode()):
            fail(f"{mid}: empty change or reused-name semantic review needed")
        metadata = load(args.patches / f"{mid}.notes.json")
        for key, value in (("module_id", mid), ("source_sha256", row["source_sha256"]),
                           ("patched_source_sha256", digest(patched[mid])), ("patch_sha256", digest(patches[mid]))):
            if key in metadata and metadata[key] != value:
                fail(f"Candidate metadata mismatch: {mid} {key}")
        reviews[mid] = review
    return SimpleNamespace(tree=tree, prior=prior, p1dir=p1dir, work=work, original=original,
        original_path=original_path, original_text=original_text, p1=p1, p1path=p1path,
        selection=selection, manifest=manifest, pins=pins, rows=rows, old_rows=old_rows,
        targets=targets, all_targets=[ledger[mid] for mid in sorted(all_target_ids)],
        retained=retained, rebuilt=rebuilt, reuse=reuse, edges=edges,
        source_original=source_original, patches=patches, patched=patched, reviews=reviews, notes=notes)


def execute(args, h, ctx):
    if args.out.exists():
        fail(f"Output already exists; refusing a second application: {args.out}")
    env = driver.environment(SimpleNamespace(work=ctx.work, lean_threads=args.lean_workers), h)
    version, mathlib = h.build.toolchain(ctx.tree, env, ctx.pins)
    original_files, phase1_files = files(ctx.prior, h), files(ctx.p1dir, h)
    args.out.mkdir(parents=True)
    shutil.copytree(ctx.prior, args.out / "original-attempt")
    if files(args.out / "original-attempt", h) != original_files:
        fail("Failed extension archive is not byte-identical")
    for name in ("phase1-receipt.json", "phase1-selection.json", "inputs-manifest.json", "compatibility.json", "islattice_audit.lean"):
        shutil.copyfile(ctx.prior / name, args.out / name)
    for name in ("logs", "patches"):
        (args.out / name).mkdir()
    driver.write_json(args.out / "reuse-before.json", ctx.reuse)
    driver.write_json(args.out / "sources-original.json", ctx.source_original)
    driver.write_json(args.out / "patch-notes.json", ctx.notes)
    driver.write_json(args.out / "proof-only-reviews.json", ctx.reviews)
    for row in ctx.targets:
        mid = row["id"]
        if mid not in REPAIRS:
            continue
        source = ctx.tree / h.build.source_relpath(row)
        driver.require_hash(source, row["source_sha256"], h)
        patch = args.out / "patches" / f"{mid}.patch"
        patch.write_bytes(ctx.patches[mid])
        print(h.build.apply_patch(ctx.tree, patch), flush=True)
        if source.read_bytes() != ctx.patched[mid]:
            fail(f"Applied target differs from strict pinned reconstruction: {mid}")
    before = {r["id"]: h.tasks.digest(ctx.tree / h.build.source_relpath(r)) for r in ctx.rows}
    driver.write_json(args.out / "sources-before.json", before)
    if {mid for mid in before if before[mid] != ctx.source_original[mid]} != REPAIRS:
        fail("Preparation changed sources outside the two reviewed targets")
    launcher = ["elan", "run", ctx.pins["lean"]]
    command = launcher + ["lake", "--no-ansi", "--no-cache", "build"] + [f"+{r['module']}:olean" for r in ctx.targets]
    print("Retrying all 15 continuation targets; retaining 160 project objects; rebuilding only M131597 and M131764.", flush=True)
    batch = driver.run_log(command, ctx.tree, env, args.out / "batch-build.log", measure=True)
    batch["log_sha256"] = h.tasks.digest(args.out / "batch-build.log")
    text = (args.out / "batch-build.log").read_text()
    sorry = bool(h.ci.SORRY_DIAG_RE.search(text))
    completion = [line for line in text.splitlines() if line.startswith("Build completed successfully")]
    retry_built = {r["id"]: built_lines(text, r["module"]) for r in ctx.rows}
    unexpected = [mid for mid in ctx.reuse if retry_built[mid]]
    build_ok = bool(batch["exit_code"] == 0 and not sorry and completion and not unexpected
                    and all(retry_built[mid] for mid in REPAIRS)
                    and all(object_path(ctx.tree, r).is_file() for r in ctx.rows))
    audit_source = args.out / "islattice_audit.lean"
    audit = dict(exit_code=None, status="not run because repaired continuation failed an evidence check")
    if build_ok:
        audit_command = launcher + ["lake", "--no-ansi", "env", "lean", "--run", str(audit_source)] + [r["module"] for r in ctx.all_targets]
        audit = driver.run_log(audit_command, ctx.tree, env, args.out / "islattice-audit.log", measure=True)
        audit["log_sha256"] = h.tasks.digest(args.out / "islattice-audit.log")
        audit_text = (args.out / "islattice-audit.log").read_text()
        audit["complete_zero_hit_summary"] = "islattice-audit: 63 module(s), 0 hit(s)" in audit_text.splitlines()
        audit["sorry_diagnostic"] = bool(h.ci.SORRY_DIAG_RE.search(audit_text))
    audit_ok = audit["exit_code"] == 0 and audit.get("complete_zero_hit_summary", False) and not audit.get("sorry_diagnostic", False)
    reuse_after = snapshot(ctx.tree, [r for r in ctx.rows if r["id"] in ctx.reuse], h)
    driver.write_json(args.out / "reuse-after.json", reuse_after)
    changed_reused = [mid for mid in ctx.reuse if ctx.reuse[mid] != reuse_after[mid]]
    after = {r["id"]: h.tasks.digest(ctx.tree / h.build.source_relpath(r)) for r in ctx.rows}
    driver.write_json(args.out / "sources-after.json", after)
    changed_sources = [mid for mid in before if before[mid] != after[mid]]
    changed_config = [name for name, value in ctx.selection["project_config"].items() if h.tasks.digest(ctx.tree / name) != value]
    changed_patches = [mid for mid, data in ctx.patches.items() if (args.out / "patches" / f"{mid}.patch").read_bytes() != data]
    original_unchanged = files(ctx.prior, h) == files(args.out / "original-attempt", h) == original_files
    phase1_unchanged = files(ctx.p1dir, h) == phase1_files
    mathlib_dir = ctx.tree / ".lake/packages/mathlib"
    mathlib_unchanged = driver.git(mathlib_dir, "rev-parse", "HEAD").decode().strip() == mathlib and not driver.git(mathlib_dir, "status", "--porcelain", "--untracked-files=no").strip()
    stable = not changed_reused and not changed_sources and not changed_config and not changed_patches and original_unchanged and phase1_unchanged and bool(mathlib_unchanged)
    rows = []
    for row in ctx.targets:
        mid, retained = row["id"], row["id"] in ctx.retained
        producer = ctx.original["batch_build"] if retained else batch
        lines = built_lines(ctx.original_text, row["module"]) if retained else retry_built[mid]
        producing_path = "../original-attempt/batch-build.log" if retained else "../batch-build.log"
        obj = object_path(ctx.tree, row)
        ok = bool(build_ok and audit_ok and stable and lines and obj.is_file())
        log = args.out / "logs" / f"{mid}.log"
        log.write_text("\n".join([f"Module: {mid} {row['module']}",
            "Target excerpt from shared continuation attempts, not an independent invocation.",
            f"Producing attempt: {'original (retained)' if retained else 'retry (rebuilt)'}",
            f"Full producing transcript: {producing_path} (SHA-256 {producer['log_sha256']})",
            *lines, f"Producing batch exit: {producer['exit_code']}",
            "The original shared batch failed on other targets; this successful output is retained unchanged." if retained else "This target was freshly rebuilt during the retry.",
            f"The all-15 retry: ../batch-build.log (SHA-256 {batch['log_sha256']})",
            *completion, f"Shared retry exit: {batch['exit_code']}",
            f"Aggregate audit over all 63 targets exit: {audit['exit_code']}; ../islattice-audit.log",
            f"All 160 retained source/object snapshots and configuration preserved: {stable}",
            f"Module evidence successful: {ok}", ""]))
        rows.append(dict(id=mid, module=row["module"], successful=ok, build_mode="shared_repaired_continuation_batch",
            compilation_attempt="original" if retained else "retry", compilation_log_sha256=producer["log_sha256"],
            compilation_batch_exit_code=producer["exit_code"], built_lines=lines,
            shared_build_exit_code=batch["exit_code"], shared_log_sha256=batch["log_sha256"],
            source_sha256=row["source_sha256"], built_source_sha256=after[mid],
            patch_sha256=digest(ctx.patches[mid]) if mid in ctx.patches else None,
            olean_sha256=h.tasks.digest(obj) if obj.is_file() else None, log_sha256=h.tasks.digest(log),
            source_changed_during_build=before[mid] != after[mid], patch_changed_during_build=mid in changed_patches,
            sorry_diagnostic=sorry, reused_names=[f"{n}:{name}" for n, name in h.build.reused_names((ctx.tree / h.build.source_relpath(row)).read_text())],
            islattice_audit=dict(scope="all 63 packet targets", exit_code=audit["exit_code"], successful=audit_ok,
                                log_sha256=audit.get("log_sha256"))))
    for result in (batch, audit):
        sidecar = result.get("resources", {}).get("gnu_time_sidecar")
        if sidecar:
            result["resources"]["gnu_time_sidecar_sha256"] = h.tasks.digest(args.out / sidecar)
    successful = sum(r["successful"] for r in rows)
    receipt = dict(ctx.original)
    receipt.update(schema=SCHEMA,
        source_method="Repair continuation in the same verified tree: 160 retained project objects, two reviewed target patches reconstructed strictly from pinned mirror bytes, and an all-15 retry followed by the all-63 audit.",
        fresh_project_objects=False, phase1_project_module_count=112, retained_project_module_count=160,
        project_source_count=162, accepted_import_count=99, toolchain=version, mathlib=mathlib,
        lean_num_threads_environment=args.lean_workers,
        started_utc=batch["started_utc"], ended_utc=driver.utc(), host=h.tasks.host_resources(),
        batch_build=batch, aggregate_islattice_audit=audit, modules=rows,
        original_attempt=dict(receipt_path=str(ctx.original_path),
            archived_receipt_path="original-attempt/P0208-extension-receipt.json",
            receipt_sha256=h.tasks.digest(ctx.original_path), batch_log_path="original-attempt/batch-build.log",
            batch_log_sha256=ctx.original["batch_build"]["log_sha256"],
            batch_exit_code=ctx.original["batch_build"]["exit_code"], fresh_project_objects=False),
        original_attempt_unchanged=original_unchanged, phase1_unchanged=phase1_unchanged,
        retained_target_ids=ctx.retained, rebuilt_target_ids=ctx.rebuilt, repaired_target_ids=sorted(REPAIRS),
        target_dependency_edges=ctx.edges, unexpected_rebuilt_retained=unexpected,
        unexpected_rebuilt_existing=unexpected, changed_retained_outputs=changed_reused,
        changed_reused_outputs=changed_reused, changed_sources=changed_sources,
        changed_config=changed_config, changed_configuration=changed_config, changed_patches=changed_patches,
        mathlib_unchanged=bool(mathlib_unchanged), proof_only_reviews=ctx.reviews,
        proof_only_reviews_sha256=h.tasks.digest(args.out / "proof-only-reviews.json"),
        patch_notes_sha256=h.tasks.digest(args.out / "patch-notes.json"),
        reuse_before_sha256=h.tasks.digest(args.out / "reuse-before.json"),
        reuse_after_sha256=h.tasks.digest(args.out / "reuse-after.json"),
        sources_original_sha256=h.tasks.digest(args.out / "sources-original.json"),
        sources_before_sha256=h.tasks.digest(args.out / "sources-before.json"),
        sources_after_sha256=h.tasks.digest(args.out / "sources-after.json"),
        source_changes_for_repair=sorted(REPAIRS), helper_sha256=ctx.selection["helper_sha256"],
        repair_driver_sha256=ctx.p1["repair_driver_sha256"],
        extension_repair_driver_sha256=h.tasks.digest(Path(__file__)),
        built_imports=[dict(record, compilation_attempt="original",
            compilation_log_sha256=ctx.original["batch_build"]["log_sha256"],
            compilation_batch_exit_code=ctx.original["batch_build"]["exit_code"])
            for record in ctx.original["built_imports"]],
        combined_target_count=48 + successful, all_successful=successful == len(rows))
    driver.write_json(args.out / "results.json", rows)
    driver.write_json(args.out / "P0208-extension-receipt.json", receipt)
    print(f"Repaired continuation: {successful}/15 successful; combined targets {48 + successful}; evidence: {args.out}", flush=True)
    return 0 if receipt["all_successful"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "phase1", "patches"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("phase1-evidence", "original", "inputs", "work", "notes", "out"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--lean-workers", type=int, default=4)
    parser.add_argument("--plan", "--dry-run", action="store_true", help="Read-only checks; no writes or Lean/Lake commands")
    args = parser.parse_args()
    for name in ("repo", "phase1", "patches", "phase1_evidence", "original", "inputs", "work", "notes", "out"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    args.phase1_evidence = args.phase1_evidence or args.phase1 / "evidence-repaired"
    args.original = args.original or args.phase1.parent / "evidence-extension"
    args.inputs = args.inputs or args.phase1.parent / "port-extension-inputs"
    args.out = args.out or args.phase1.parent / "evidence-extension-repaired"
    if args.lean_workers < 1:
        fail("--lean-workers must be positive")
    h = driver.helpers(args.repo)
    ctx = preflight(args, h)
    if args.plan:
        print(json.dumps(dict(schema=SCHEMA, source_count=len(ctx.rows), retained_project_objects=len(ctx.reuse),
            phase1_project_modules=len(ctx.old_rows), accepted_import_count=99,
            retained_target_ids=ctx.retained, rebuilt_target_ids=ctx.rebuilt, target_dependency_edges=ctx.edges,
            audit_target_count=len(ctx.all_targets), proof_only_reviews=ctx.reviews, output=str(args.out)), indent=2))
        return 0
    return execute(args, h, ctx)


if __name__ == "__main__":
    sys.exit(main())
