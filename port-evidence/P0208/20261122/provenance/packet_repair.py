#!/usr/bin/env python3
"""Repair failed P0208 targets, retaining verified original outputs; never write port records.

--plan is read-only and may run before the initial attempt finishes. Execution
requires its completed failed receipt. Original evidence is never overwritten.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
import packet_driver as driver
from packet_extension import built_lines, object_path, snapshot
from verify_p0208_evidence import apply_diff

PACKET = "P0208"
SCHEMA = "rh-port-repaired-batch-receipt-v1"
M130315_PIN = "d2246966c28be0da4adf4c3cccbc8c61330fe0d84aebe26782412de810de950b"
M130315_REPLACEMENTS = [
    dict(old="Polynomial.coeff_monomial", new="Polynomial.coeff_monomial_apply", count=7),
    dict(old="AddSubmonoidClass.coe_finset_sum", new="AddSubmonoidClass.coe_finsetSum", count=1),
]
M130315_EQUIVALENCE = (
    "The polynomial data in comp and coord are byte-identical; only their subtype membership proofs change, "
    "so the resulting subtype values are equal by Subtype.ext rfl and proof irrelevance. "
    "In gradedRing, only the mul_mem proof field changes; all data and other instance fields remain "
    "byte-identical, so proof irrelevance preserves the instance.")


def fail(message):
    raise SystemExit(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def files(root, h):
    return {p.relative_to(root).as_posix(): h.tasks.digest(p)
            for p in sorted(root.rglob("*")) if p.is_file()}


def proof_skeleton(data):
    """Omit only indented bodies of top-level theorem/lemma `by` proofs."""
    result, header, body = [], False, False
    for line in data.decode().splitlines(keepends=True):
        if line.strip() and not line[0].isspace():
            body = False
            header = bool(re.match(r"^(?:(?:private|protected)\s+)*(?:theorem|lemma)\b", line))
        if body and (not line.strip() or line[0].isspace()):
            continue
        result.append(line)
        if header and re.search(r":=\s*by\s*(?:--[^\n]*)?\n?$", line):
            body, header = True, False
    return "".join(result).encode()


def preflight(args, h, require_original):
    tree, prior = args.phase1 / "lean", args.phase1 / "evidence"
    work = args.work or (tree / ".lake/packages").resolve().parents[2]
    selection_file = prior / "selection.json"
    if not selection_file.is_file():
        selection_file = work / f"{PACKET}-selection.json"
    selection = json.loads(selection_file.read_text())
    pins = h.build.target()
    if selection["packet"] != PACKET or selection["pins"] != pins:
        fail("Wrong frozen packet/toolchain selection")
    for name, expected in selection["helper_sha256"].items():
        driver.require_hash(args.repo / name, expected, h)
        if digest(driver.git(args.repo, "show", f"{selection['accepted_commit']}:{name}")) != expected:
            fail(f"Helper differs from accepted baseline: {name}")
    for name, expected in (("tracker/modules.jsonl.gz", selection["ledger_sha256"]),
                           ("port/target.json", selection["target_sha256"])):
        driver.require_hash(args.repo / name, expected, h)
    for name, expected in selection["project_config"].items():
        driver.require_hash(tree / name, expected, h)
    mathlib = tree / ".lake/packages/mathlib"
    if driver.git(mathlib, "rev-parse", "HEAD").decode().strip() != pins["mathlib"] or driver.git(mathlib, "status", "--porcelain", "--untracked-files=no").strip():
        fail("Mathlib no longer has the clean target commit")
    ledger = h.ci.load_ledger()
    targets, imports = selection["selected"], selection["imports"]
    rows = targets + imports
    ids, target_ids = {r["id"] for r in rows}, {r["id"] for r in targets}
    if len(target_ids) != 48 or len(imports) != 64 or len(ids) != 112:
        fail("Wrong reviewed 48-target/64-import scope")
    if set(h.ci.transitive_closure(sorted(target_ids), ledger)) != ids or any(ledger[r["id"]] != r for r in rows):
        fail("Frozen rows do not equal the pinned ledger import closure")
    edges = []
    for row in targets:
        dependencies = set(h.ci.transitive_closure(row["imports"], ledger))
        edges.extend([row["id"], dep] for dep in sorted(dependencies & target_ids))
    if edges:
        fail(f"Same-packet target dependencies prevent retention: {edges}")
    accepted = {r["id"]: r for r in selection["accepted_import_records"]}
    if set(accepted) != {r["id"] for r in imports}:
        fail("Accepted import record scope differs from the import closure")
    tracked = set(driver.git(args.repo, "ls-tree", "-r", "--name-only", selection["accepted_commit"], "--", "port/patches").decode().splitlines())
    originals = {}
    for row in rows:
        mid, rel = row["id"], h.build.source_relpath(row)
        original_path = work / "pinned-sources" / rel
        driver.require_hash(original_path, row["source_sha256"], h)
        originals[mid] = original = original_path.read_bytes()
        expected = original
        if mid in accepted:
            record = accepted[mid]
            recpath = args.repo / "port/patches" / f"{mid}.json"
            patch = recpath.with_suffix(".patch")
            for path, key in ((recpath, "record_sha256"), (patch, "patch_sha256")):
                relative = path.relative_to(args.repo).as_posix()
                blob = driver.git(args.repo, "show", f"{selection['accepted_commit']}:{relative}") if relative in tracked else None
                if (digest(blob) if blob is not None else None) != record[key]:
                    fail(f"Accepted import fingerprint changed: {relative}")
                if (blob is not None and path.read_bytes() != blob) or (blob is None and path.exists()):
                    fail(f"Local accepted import differs from its accepted Git blob: {relative}")
            driver.validate_record(row, json.loads(recpath.read_text()), patch, pins, h)
            if patch.exists():
                expected = apply_diff(original, patch.read_bytes(), rel)
            if digest(expected) != record["built_source_sha256"]:
                fail(f"Accepted patch does not reconstruct frozen source: {mid}")
        if (tree / rel).read_bytes() != expected:
            fail(f"Initial-attempt source changed before repair: {mid}")
    notes_path = args.notes or args.patches / "notes.json"
    notes = json.loads(notes_path.read_text())
    patch_ids = {p.stem for p in args.patches.glob("M*.patch")}
    if not patch_ids or not patch_ids <= target_ids or set(notes) != patch_ids:
        fail("Patches and notes must identify exactly a nonempty subset of selected targets")
    patches, reviews, patched = {}, {}, {}
    for mid in sorted(patch_ids):
        row, path = ledger[mid], args.patches / f"{mid}.patch"
        errors = list(h.build.validate_patch_content(path, h.build.source_relpath(row)))
        if errors:
            fail("\n".join(errors))
        note = notes[mid]
        if not isinstance(note, str) or "proof" not in note.lower() or "statement" not in note.lower():
            fail(f"{mid}: explain the proof-only change and unchanged statements in notes")
        patches[mid] = path.read_bytes()
        patched[mid] = apply_diff(originals[mid], patches[mid], h.build.source_relpath(row))
        before, after = proof_skeleton(originals[mid]), proof_skeleton(patched[mid])
        special_review = {}
        if mid == "M130315":
            if row["source_sha256"] != M130315_PIN or digest(originals[mid]) != M130315_PIN:
                fail("M130315 proof-field exception requires the exact reviewed pinned source")
            transformed = originals[mid].decode()
            for change in M130315_REPLACEMENTS:
                pattern = r"(?<![\w'.])" + re.escape(change["old"]) + r"(?![\w'.])"
                transformed, count = re.subn(pattern, change["new"], transformed)
                if count != change["count"]:
                    fail(f"M130315 reviewed replacement count differs: {change['old']}")
            if transformed.encode() != patched[mid]:
                fail("M130315 differs from the exact eight reviewed identifier substitutions")
            special_review = dict(review_method="exact_reviewed_identifier_replacements",
                reviewed_replacements=M130315_REPLACEMENTS, equivalence_note=M130315_EQUIVALENCE,
                statement_and_definition_text_unchanged=False, theorem_statement_text_unchanged=True,
                definition_and_instance_data_unchanged=True, patched_nonproof_skeleton_sha256=digest(after))
        elif before != after:
            fail(f"{mid}: changes outside an indented theorem proof")
        if originals[mid] == patched[mid]:
            fail(f"{mid}: changes outside an indented theorem proof, or empty patch")
        if h.build.reused_names(patched[mid].decode()):
            fail(f"{mid}: reused-name semantic review is required")
        metadata_path = args.patches / f"{mid}.notes.json"
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
            for key, expected in (("module_id", mid), ("source_sha256", row["source_sha256"]),
                                  ("patched_source_sha256", digest(patched[mid]))):
                if key in metadata and metadata[key] != expected:
                    fail(f"{mid}: candidate notes metadata differs in {key}")
        reviews[mid] = dict(notes=note, nonproof_skeleton_sha256=digest(before),
                            statement_and_definition_text_unchanged=True,
                            source_sha256=row["source_sha256"], built_source_sha256=digest(patched[mid]),
                            patch_sha256=digest(patches[mid]))
        reviews[mid].update(special_review)
    receipt_path = prior / f"{PACKET}-receipt.json"
    original = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
    retained, rebuilt, reuse, original_text = [], [], None, None
    if original:
        if original["schema"] != "rh-port-mirror-batch-receipt-v1" or original["fresh_project_objects"] is not True or original["all_successful"] or original["batch_build"]["exit_code"] in (None, 0):
            fail("Repair requires the completed failed initial fresh batch")
        for key in ("accepted_commit", "project_config", "helper_sha256", "ledger_sha256", "target_sha256"):
            if original[key] != selection[key]:
                fail(f"Original-attempt provenance mismatch: {key}")
        if original["imports"] != selection["accepted_import_records"] or original["changed_sources"] or original["changed_config"] or not original["mathlib_unchanged"]:
            fail("Initial attempt changed inputs or recorded different imports")
        driver.require_hash(selection_file, original["selection_sha256"], h)
        driver.require_hash(Path(driver.__file__), original["driver_sha256"], h)
        for name, value in (("batch-build.log", original["batch_build"]["log_sha256"]),
                            (f"{PACKET}-build.log", original["build_log_sha256"]),
                            ("islattice_audit.lean", original["islattice_audit_sha256"])):
            driver.require_hash(prior / name, value, h)
        original_text = (prior / "batch-build.log").read_text()
        command = ["elan", "run", pins["lean"], "lake", "--no-ansi", "--no-cache", "build"] + [f"+{r['module']}:olean" for r in targets]
        if original["batch_build"]["command"] != command or f"== exit_code: {original['batch_build']['exit_code']}" not in original_text.splitlines() or h.ci.SORRY_DIAG_RE.search(original_text):
            fail("Original command/exit metadata mismatch or sorry diagnostic")
        results = {r["id"]: r for r in original["modules"]}
        if set(results) != target_ids or json.loads((prior / "results.json").read_text()) != original["modules"]:
            fail("Initial result scope differs from selected targets")
        for row in targets:
            mid, result = row["id"], results[row["id"]]
            if result["source_sha256"] != row["source_sha256"] or result["built_source_sha256"] != row["source_sha256"] or result["patch_sha256"] is not None or result["source_changed_during_build"] or result["patch_changed_during_build"]:
                fail(f"Initial target is not the unchanged pinned source: {mid}")
            driver.require_hash(prior / "logs" / f"{mid}.log", result["log_sha256"], h)
            lines = built_lines(original_text, row["module"])
            if lines != result["built_lines"]:
                fail(f"Original Built excerpt differs from full transcript: {mid}")
            if result["olean_sha256"]:
                driver.require_hash(object_path(tree, row), result["olean_sha256"], h)
            if lines and result["olean_sha256"] and mid not in patch_ids:
                retained.append(mid)
            else:
                rebuilt.append(mid)
        for row in imports:
            if not built_lines(original_text, row["module"]) or not object_path(tree, row).is_file():
                fail(f"Missing successful original import evidence: {row['id']}")
        reuse = snapshot(tree, [r for r in rows if r["id"] in set(retained) | set(accepted)], h)
    elif require_original:
        fail(f"Wait for the initial attempt to finish and write {receipt_path}")
    return SimpleNamespace(tree=tree, prior=prior, work=work, selection=selection,
        selection_file=selection_file, pins=pins, targets=targets, imports=imports, rows=rows,
        originals=originals, patches=patches, patched=patched, notes=notes, reviews=reviews,
        original=original, original_text=original_text, receipt_path=receipt_path,
        retained=retained, rebuilt=rebuilt, reuse=reuse, edges=edges)


def execute(args, h, ctx):
    if args.out.exists():
        fail(f"Output already exists: {args.out}")
    env = driver.environment(SimpleNamespace(work=ctx.work, lean_threads=args.lean_workers), h)
    version, mathlib = h.build.toolchain(ctx.tree, env, ctx.pins)
    initial_files = files(ctx.prior, h)
    args.out.mkdir(parents=True)
    shutil.copytree(ctx.prior, args.out / "original-attempt")
    if files(args.out / "original-attempt", h) != initial_files:
        fail("Original evidence archive differs from the completed attempt")
    (args.out / "logs").mkdir()
    (args.out / "patches").mkdir()
    shutil.copyfile(ctx.selection_file, args.out / "selection.json")
    driver.write_json(args.out / "reuse-before.json", ctx.reuse)
    driver.write_json(args.out / "patch-notes.json", ctx.notes)
    driver.write_json(args.out / "proof-only-reviews.json", ctx.reviews)
    for row in ctx.targets:
        mid = row["id"]
        if mid not in ctx.patches:
            continue
        patch = args.out / "patches" / f"{mid}.patch"
        patch.write_bytes(ctx.patches[mid])
        print(h.build.apply_patch(ctx.tree, patch), flush=True)
        if (ctx.tree / h.build.source_relpath(row)).read_bytes() != ctx.patched[mid]:
            fail(f"Strict applied source differs from in-memory pinned reconstruction: {mid}")
    before = {r["id"]: h.tasks.digest(ctx.tree / h.build.source_relpath(r)) for r in ctx.rows}
    launcher = ["elan", "run", ctx.pins["lean"]]
    command = launcher + ["lake", "--no-ansi", "--no-cache", "build"] + [f"+{r['module']}:olean" for r in ctx.targets]
    print(f"Repair retry requests all 48 targets; retaining {len(ctx.retained)} targets and 64 imports; rebuilding {len(ctx.rebuilt)} targets.", flush=True)
    batch = driver.run_log(command, ctx.tree, env, args.out / "batch-build.log", measure=True)
    batch["log_sha256"] = h.tasks.digest(args.out / "batch-build.log")
    text = (args.out / "batch-build.log").read_text()
    sorry = bool(h.ci.SORRY_DIAG_RE.search(text))
    completion = [line for line in text.splitlines() if line.startswith("Build completed successfully")]
    retry_built = {r["id"]: built_lines(text, r["module"]) for r in ctx.rows}
    unexpected = [mid for mid in ctx.reuse if retry_built[mid]]
    build_ok = bool(batch["exit_code"] == 0 and not sorry and completion and not unexpected
                    and all(retry_built[mid] for mid in ctx.rebuilt)
                    and all(object_path(ctx.tree, row).is_file() for row in ctx.targets))
    audit_source = args.out / "islattice_audit.lean"
    shutil.copyfile(args.repo / "port/islattice_audit.lean", audit_source)
    audit = dict(exit_code=None, status="not run because retry failed an evidence check")
    if build_ok:
        audit_command = launcher + ["lake", "--no-ansi", "env", "lean", "--run", str(audit_source)] + [r["module"] for r in ctx.targets]
        audit = driver.run_log(audit_command, ctx.tree, env, args.out / "islattice-audit.log", measure=True)
        audit["log_sha256"] = h.tasks.digest(args.out / "islattice-audit.log")
        audit_text = (args.out / "islattice-audit.log").read_text()
        audit["complete_zero_hit_summary"] = "islattice-audit: 48 module(s), 0 hit(s)" in audit_text.splitlines()
        audit["sorry_diagnostic"] = bool(h.ci.SORRY_DIAG_RE.search(audit_text))
    audit_ok = audit["exit_code"] == 0 and audit.get("complete_zero_hit_summary", False) and not audit.get("sorry_diagnostic", False)
    reuse_after = snapshot(ctx.tree, [r for r in ctx.rows if r["id"] in ctx.reuse], h)
    driver.write_json(args.out / "reuse-after.json", reuse_after)
    changed_reused = [mid for mid in ctx.reuse if ctx.reuse[mid] != reuse_after[mid]]
    after = {r["id"]: h.tasks.digest(ctx.tree / h.build.source_relpath(r)) for r in ctx.rows}
    changed_sources = [mid for mid in before if before[mid] != after[mid]]
    changed_config = [name for name, value in ctx.selection["project_config"].items() if h.tasks.digest(ctx.tree / name) != value]
    changed_patches = [mid for mid, data in ctx.patches.items() if (args.out / "patches" / f"{mid}.patch").read_bytes() != data]
    original_unchanged = files(ctx.prior, h) == files(args.out / "original-attempt", h) == initial_files
    mathlib_dir = ctx.tree / ".lake/packages/mathlib"
    mathlib_unchanged = driver.git(mathlib_dir, "rev-parse", "HEAD").decode().strip() == mathlib and not driver.git(mathlib_dir, "status", "--porcelain", "--untracked-files=no").strip()
    stable = not changed_reused and not changed_sources and not changed_config and not changed_patches and original_unchanged and bool(mathlib_unchanged)
    rows = []
    for row in ctx.targets:
        mid = row["id"]
        retained = mid in ctx.retained
        producer = ctx.original["batch_build"] if retained else batch
        lines = built_lines(ctx.original_text, row["module"]) if retained else retry_built[mid]
        producing_path = "../original-attempt/batch-build.log" if retained else "../batch-build.log"
        obj = object_path(ctx.tree, row)
        ok = bool(build_ok and audit_ok and stable and lines and obj.is_file())
        log = args.out / "logs" / f"{mid}.log"
        log.write_text("\n".join([f"Module: {mid} {row['module']}",
            "Target excerpt from shared attempts, not an independent module invocation.",
            f"Producing compilation attempt: {'original (retained)' if retained else 'retry (rebuilt)'}",
            f"Full producing transcript: {producing_path} (SHA-256 {producer['log_sha256']})",
            *lines, f"Producing batch exit: {producer['exit_code']}",
            "The original batch failed elsewhere; retained object hashes are preserved." if retained else "This target was freshly rebuilt during the retry.",
            f"Successful checkpoint validation requests all 48 targets: ../batch-build.log (SHA-256 {batch['log_sha256']})",
            *completion, f"Shared retry exit: {batch['exit_code']}",
            f"Aggregate audit over all 48 targets exit: {audit['exit_code']}; ../islattice-audit.log",
            f"Retained sources/objects and configuration preserved: {stable}",
            f"Module evidence successful: {ok}", ""]))
        rows.append(dict(id=mid, module=row["module"], successful=ok, build_mode="shared_repaired_batch",
            compilation_attempt="original" if retained else "retry", compilation_log_sha256=producer["log_sha256"],
            compilation_batch_exit_code=producer["exit_code"], built_lines=lines,
            shared_build_exit_code=batch["exit_code"], shared_log_sha256=batch["log_sha256"],
            source_sha256=row["source_sha256"], built_source_sha256=after[mid],
            patch_sha256=digest(ctx.patches[mid]) if mid in ctx.patches else None,
            olean_sha256=h.tasks.digest(obj) if obj.is_file() else None, log_sha256=h.tasks.digest(log),
            source_changed_during_build=before[mid] != after[mid], patch_changed_during_build=mid in changed_patches,
            sorry_diagnostic=sorry, reused_names=[f"{n}:{name}" for n, name in h.build.reused_names((ctx.tree / h.build.source_relpath(row)).read_text())],
            islattice_audit=dict(scope="all 48 selected modules together", exit_code=audit["exit_code"],
                                successful=audit_ok, log_sha256=audit.get("log_sha256"))))
    for result in (batch, audit):
        sidecar = result.get("resources", {}).get("gnu_time_sidecar")
        if sidecar:
            result["resources"]["gnu_time_sidecar_sha256"] = h.tasks.digest(args.out / sidecar)
    successful = sum(r["successful"] for r in rows)
    summary = args.out / f"{PACKET}-build.log"
    summary.write_text("\n".join(["Continuation of an initially fresh, SHA-256-verified pinned mirror build.",
        f"Original failed attempt retained unchanged; {len(ctx.retained)} target outputs and 64 imports retained.",
        f"Retry requested all 48 targets; fresh Built evidence required for {len(ctx.rebuilt)} rebuilt targets.",
        f"Command: {shlex.join(command)}", f"Toolchain: {version}; Mathlib: {mathlib}",
        *[f"{r['id']} producer={r['compilation_attempt']} successful={r['successful']}" for r in rows],
        f"Shared retry exit={batch['exit_code']}; sorry={sorry}; aggregate audit exit={audit['exit_code']}",
        f"DONE {successful}/48 successful", ""]))
    selection = ctx.selection
    receipt = dict(schema=SCHEMA, target=PACKET, issue=ctx.original.get("issue"),
        source_method="Continuation in the same initially fresh verified project: retained successful outputs and proof-only patches applied strictly to SHA-256-verified pinned mirror sources.",
        source_base_url=selection["source_base_url"], accepted_commit=selection["accepted_commit"],
        fresh_project_objects=False, toolchain=version, mathlib=mathlib,
        lean_num_threads_environment=args.lean_workers,
        concurrency_note="LEAN_NUM_THREADS is the Lake worker budget, not the per-compiler thread count.",
        started_utc=batch["started_utc"], ended_utc=driver.utc(), host=h.tasks.host_resources(),
        batch_build=batch, aggregate_islattice_audit=audit, modules=rows,
        original_attempt=dict(receipt_path=str(ctx.receipt_path), archived_receipt_path="original-attempt/P0208-receipt.json",
            receipt_sha256=h.tasks.digest(ctx.receipt_path), batch_log_path="original-attempt/batch-build.log",
            batch_log_sha256=ctx.original["batch_build"]["log_sha256"],
            batch_exit_code=ctx.original["batch_build"]["exit_code"], fresh_project_objects=True),
        original_attempt_unchanged=original_unchanged,
        retained_target_ids=ctx.retained, rebuilt_target_ids=ctx.rebuilt, repaired_target_ids=sorted(ctx.patches),
        target_dependency_edges=ctx.edges, retained_project_module_count=len(ctx.reuse),
        unexpected_rebuilt_retained=unexpected, changed_retained_outputs=changed_reused,
        changed_sources=changed_sources, changed_config=changed_config, changed_patches=changed_patches,
        mathlib_unchanged=bool(mathlib_unchanged), proof_only_reviews=ctx.reviews,
        proof_only_reviews_sha256=h.tasks.digest(args.out / "proof-only-reviews.json"),
        patch_notes_sha256=h.tasks.digest(args.out / "patch-notes.json"),
        reuse_before_sha256=h.tasks.digest(args.out / "reuse-before.json"),
        reuse_after_sha256=h.tasks.digest(args.out / "reuse-after.json"),
        selection_sha256=h.tasks.digest(ctx.selection_file), project_config=selection["project_config"],
        helper_sha256=selection["helper_sha256"], driver_sha256=ctx.original["driver_sha256"],
        repair_driver_sha256=h.tasks.digest(Path(__file__)),
        ledger_sha256=selection["ledger_sha256"], target_sha256=selection["target_sha256"],
        imports=selection["accepted_import_records"], remaining=selection["remaining"],
        pruned=selection["pruned"], already_ported=selection["already_ported"],
        build_log_sha256=h.tasks.digest(summary), islattice_audit_sha256=h.tasks.digest(audit_source),
        all_successful=successful == len(rows))
    driver.write_json(args.out / "results.json", rows)
    driver.write_json(args.out / f"{PACKET}-receipt.json", receipt)
    print(f"Repair checkpoint: {successful}/48 successful; evidence: {args.out}", flush=True)
    return 0 if receipt["all_successful"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "phase1", "patches"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("work", "out", "notes"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--lean-workers", type=int, default=4)
    parser.add_argument("--plan", "--dry-run", action="store_true", help="Read-only; no Lake commands or writes")
    args = parser.parse_args()
    for name in ("repo", "phase1", "patches", "work", "out", "notes"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    args.out = args.out or args.phase1 / "evidence-repaired"
    if args.lean_workers < 1:
        fail("--lean-workers must be positive")
    h = driver.helpers(args.repo)
    ctx = preflight(args, h, require_original=not args.plan)
    if args.plan:
        print(json.dumps(dict(original_attempt_complete=ctx.original is not None,
            target_count=len(ctx.targets), import_count=len(ctx.imports),
            repaired_target_ids=sorted(ctx.patches), retained_target_ids=ctx.retained,
            rebuilt_target_ids=ctx.rebuilt, target_dependency_edges=ctx.edges,
            proof_only_reviews=ctx.reviews, output=str(args.out)), indent=2))
        return 0
    return execute(args, h, ctx)


if __name__ == "__main__":
    sys.exit(main())
