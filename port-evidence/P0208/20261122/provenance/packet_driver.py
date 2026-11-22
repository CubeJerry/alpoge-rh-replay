#!/usr/bin/env python3
"""Local packet port runner using upstream helpers and SHA-256-verified mirror sources.

Does not create port records, claims, or commits. Final reproduction never reuses
project objects: only the prepared target Mathlib dependency packages are linked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def helpers(repo):
    sys.path.insert(0, str(repo / "tracker"))
    import port
    import port_build
    import tasks
    spec = importlib.util.spec_from_file_location("port_ci_build", repo / "scripts/port-ci-build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(port=port, build=port_build, tasks=tasks, ci=module)


def selection_path(args):
    return args.work / f"{args.packet}-selection.json"


def environment(args, h):
    env = h.build.environment(args.work)
    env["PATH"] = str(args.work / "elan/bin") + os.pathsep + env.get("PATH", "")
    env["LEAN_NUM_THREADS"] = str(getattr(args, "lean_threads", 1))
    return env


def require_hash(path, expected, h):
    if not path.is_file() or h.tasks.digest(path) != expected:
        raise SystemExit(f"SHA-256 mismatch or missing file: {path}")


def validate_record(row, record, patch, pins, h):
    for key in ("module", "source_sha256", "lean", "mathlib"):
        expected = row[key] if key in ("module", "source_sha256") else pins[key]
        if record.get(key) != expected:
            raise SystemExit(f"{row['id']}: accepted record has wrong {key}")
    notes = record.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        raise SystemExit(f"{row['id']}: accepted record has no notes")
    if (notes == "unchanged") == patch.exists():
        raise SystemExit(f"{row['id']}: accepted notes/patch mismatch")
    if patch.exists():
        errors = list(h.build.validate_patch_content(patch, h.build.source_relpath(row)))
        if errors:
            raise SystemExit("\n".join(errors))


def prepare(args, h):
    path = selection_path(args)
    if path.exists():
        raise SystemExit(f"Selection already frozen: {path}; use check/diff/reproduce")
    pins = h.build.target()
    accepted = git(args.repo, "rev-parse", args.accepted_ref).decode().strip()
    for rel in ("port/target.json", "port/pruned.txt", "tracker/modules.jsonl.gz"):
        if git(args.repo, "show", f"{accepted}:{rel}") != (args.repo / rel).read_bytes():
            raise SystemExit(f"Local {rel} differs from accepted ref {accepted}")
    tracked = set(git(args.repo, "ls-tree", "-r", "--name-only", accepted, "--", "port/patches").decode().splitlines())
    done = {Path(p).stem for p in tracked if re.fullmatch(r"port/patches/M\d{6}\.json", p)}
    by_id = h.ci.load_ledger()
    all_members = [r for r in by_id.values() if r["packet"] == args.packet]
    if not all_members:
        raise SystemExit(f"Unknown packet: {args.packet}")
    pruned = h.port.pruned()
    live = [r for r in all_members if r["id"] not in pruned]
    selected = sorted(h.port.ready_members(live, done), key=lambda r: (r["depth"], r["id"]))
    if not selected:
        raise SystemExit("No unported ready members on the accepted ref")
    selected_ids = {r["id"] for r in selected}
    known = done | selected_ids
    remaining = [dict(id=r["id"], module=r["module"],
                      missing_imports=[d for d in r["imports"] if d not in known])
                 for r in live if r["id"] not in known]
    imports = h.build.imports_closure(by_id, selected)
    records = []
    for row in imports:
        mid = row["id"]
        if mid not in done:
            raise SystemExit(f"Unaccepted project import: {mid}")
        record_path = args.repo / "port/patches" / f"{mid}.json"
        patch = record_path.with_suffix(".patch")
        for file in (record_path, patch):
            rel = file.relative_to(args.repo).as_posix()
            if rel in tracked:
                if not file.exists() or git(args.repo, "show", f"{accepted}:{rel}") != file.read_bytes():
                    raise SystemExit(f"{rel} differs from accepted commit {accepted}")
            elif file.exists():
                raise SystemExit(f"Unaccepted import file: {rel}")
        validate_record(row, json.loads(record_path.read_text()), patch, pins, h)
        records.append(dict(id=mid, record_sha256=h.tasks.digest(record_path),
                            patch_sha256=h.tasks.digest(patch) if patch.exists() else None))
    closure = sorted(selected + imports, key=lambda r: r["id"])
    pinned = args.work / "pinned-sources"
    missing = []
    for row in closure:
        source = pinned / h.build.source_relpath(row)
        if source.exists():
            require_hash(source, row["source_sha256"], h)
        else:
            missing.append(row["id"])
    if missing:
        h.ci.materialize_sources(pinned, missing, by_id, None, h.ci.DEFAULT_SOURCE_BASE_URL,
                                 max_workers=args.fetch_workers)
    tree = args.work / "lean"
    for row in closure:
        source = pinned / h.build.source_relpath(row)
        require_hash(source, row["source_sha256"], h)
        dest = tree / h.build.source_relpath(row)
        if dest.exists() and dest.read_bytes() != source.read_bytes():
            raise SystemExit(f"Refusing to overwrite an edited source: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    for row, record in zip(imports, records):
        patch = args.repo / "port/patches" / f"{row['id']}.patch"
        if patch.exists():
            print(h.build.apply_patch(tree, patch), flush=True)
        record["built_source_sha256"] = h.tasks.digest(tree / h.build.source_relpath(row))
    h.ci.write_project_files(tree, [r["module"] for r in closure], pins)
    manifest = tree / "lake-manifest.json"
    if not manifest.is_file():
        raise SystemExit("Prepared work/lean/lake-manifest.json is required")
    value = dict(schema="rh-port-mirror-selection-v1", packet=args.packet, created_utc=utc(),
                 accepted_commit=accepted, pins=pins, source_base_url=h.ci.DEFAULT_SOURCE_BASE_URL,
                 source_method="Pinned GitHub source mirror; each source verified against tracker/modules.jsonl.gz. No full archive extraction is claimed.",
                 ledger_sha256=h.tasks.digest(args.repo / "tracker/modules.jsonl.gz"),
                 target_sha256=h.tasks.digest(args.repo / "port/target.json"),
                 selected=selected, imports=imports, accepted_import_records=records,
                 remaining=remaining, pruned=[r["id"] for r in all_members if r["id"] in pruned],
                 already_ported=[r["id"] for r in live if r["id"] in done],
                 helper_sha256={name: h.tasks.digest(args.repo / name) for name in
                                ("tracker/port.py", "tracker/port_build.py", "tracker/validate_ports.py",
                                 "scripts/port-ci-build.py", "port/islattice_audit.lean")},
                 project_config={name: h.tasks.digest(tree / name) for name in
                                 ("lean-toolchain", "lakefile.toml", "lake-manifest.json")})
    write_json(path, value)
    print(f"Frozen {len(selected)} ready targets, {len(imports)} accepted imports, "
          f"{len(remaining)} remaining: {path}")


def load_selection(args, h):
    value = json.loads(selection_path(args).read_text())
    if value["packet"] != args.packet:
        raise SystemExit("Selection packet mismatch")
    require_hash(args.repo / "port/target.json", value["target_sha256"], h)
    require_hash(args.repo / "tracker/modules.jsonl.gz", value["ledger_sha256"], h)
    for name, digest in value["helper_sha256"].items():
        require_hash(args.repo / name, digest, h)
    for name, digest in value["project_config"].items():
        require_hash(args.work / "lean" / name, digest, h)
    for row in value["selected"] + value["imports"]:
        require_hash(args.work / "pinned-sources" / h.build.source_relpath(row), row["source_sha256"], h)
    for record in value["accepted_import_records"]:
        path = args.repo / "port/patches" / f"{record['id']}.json"
        require_hash(path, record["record_sha256"], h)
        patch = path.with_suffix(".patch")
        if record["patch_sha256"]:
            require_hash(patch, record["patch_sha256"], h)
        elif patch.exists():
            raise SystemExit(f"Unexpected import patch: {patch}")
    return value


def make_diffs(args, h, selection):
    folder = args.patches or args.repo / "port/patches"
    folder.mkdir(parents=True, exist_ok=True)
    for row in selection["selected"]:
        rel = h.build.source_relpath(row)
        original = args.work / "pinned-sources" / rel
        current = args.work / "lean" / rel
        patch = folder / f"{row['id']}.patch"
        if current.read_bytes() == original.read_bytes():
            if patch.exists():
                raise SystemExit(f"Stale patch for unchanged module: {patch}")
            continue
        result = subprocess.run(["diff", "-u", "--label", "a/" + rel, "--label", "b/" + rel,
                                 str(original), str(current)], capture_output=True)
        if result.returncode != 1:
            raise SystemExit(f"diff failed for {row['id']}: {result.stderr.decode()}")
        patch.write_bytes(result.stdout)
        errors = list(h.build.validate_patch_content(patch, rel))
        if errors:
            raise SystemExit("\n".join(errors))
        print(patch)


def run_log(command, tree, env, log, measure=False):
    start, timer = utc(), time.monotonic()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    time_log = log.with_suffix(".resources.txt")
    timed = measure and Path("/usr/bin/time").is_file()
    execution = ["/usr/bin/time", "-v", "-o", str(time_log), *command] if timed else command
    with log.open("w") as stream:
        stream.write(f"== started_utc: {start}\n== cwd: {tree}\n== command: {shlex.join(command)}\n"
                     f"== execution_command: {shlex.join(execution)}\n"
                     f"== LEAN_NUM_THREADS: {env['LEAN_NUM_THREADS']}\n")
        stream.flush()
        code = subprocess.run(execution, cwd=tree, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
        end, seconds = utc(), round(time.monotonic() - timer, 2)
        stream.write(f"\n== exit_code: {code}\n== ended_utc: {end}\n== seconds: {seconds}\n")
    result = dict(exit_code=code, started_utc=start, ended_utc=end, seconds=seconds,
                  command=command, execution_command=execution)
    if measure:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        result["resources"] = dict(user_seconds=round(usage.ru_utime - usage_before.ru_utime, 3),
                                   system_seconds=round(usage.ru_stime - usage_before.ru_stime, 3),
                                   cumulative_max_child_rss_kib=usage.ru_maxrss,
                                   note="Largest child RSS, not aggregate concurrent memory; getrusage maximum is cumulative for this driver process.")
        if timed:
            result["resources"]["gnu_time_sidecar"] = time_log.name
            match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", time_log.read_text())
            if match:
                result["resources"]["gnu_time_max_rss_kib"] = int(match[1])
    return result


def build_all(args, h, selection, tree, out, reproduce):
    env = environment(args, h)
    version, mathlib = h.build.toolchain(tree, env, selection["pins"])
    if git(tree / ".lake/packages/mathlib", "status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("Mathlib has tracked modifications")
    out.mkdir(parents=True, exist_ok=False)
    (out / "logs").mkdir()
    launcher = ["elan", "run", selection["pins"]["lean"]]
    started = utc()
    rows = []
    audit_source = args.repo / "port/islattice_audit.lean"
    if reproduce:
        shutil.copyfile(audit_source, out / "islattice_audit.lean")
        audit_source = out / "islattice_audit.lean"
    with (out / f"{args.packet}-build.log").open("w") as summary:
        def say(line):
            print(line, flush=True)
            summary.write(line + "\n")
            summary.flush()
        say(selection["source_method"])
        say(f"Toolchain: {version}; Mathlib {mathlib}")
        say(f"Targets: {len(selection['selected'])}; accepted imports: {len(selection['imports'])}")
        for row in selection["selected"]:
            mid, module = row["id"], row["module"]
            source = tree / h.build.source_relpath(row)
            source_before = h.tasks.digest(source)
            log = out / "logs" / f"{mid}.log"
            command = launcher + ["lake", "--no-cache", "build", f"+{module}:olean"]
            say(f"START {mid} {module}")
            result = run_log(command, tree, env, log)
            text = log.read_text(errors="replace")
            sorry = bool(h.ci.SORRY_DIAG_RE.search(text))
            olean = tree / ".lake/build/lib/lean" / (module.replace(".", "/") + ".olean")
            source_after = h.tasks.digest(source)
            ok = result["exit_code"] == 0 and not sorry and olean.is_file() and source_before == source_after
            patch = (args.patches or args.repo / "port/patches") / f"{mid}.patch"
            result.update(id=mid, module=module, command=command, successful=ok,
                          source_sha256=row["source_sha256"], built_source_sha256=source_after,
                          source_changed_during_build=source_before != source_after,
                          patch_sha256=h.tasks.digest(patch) if patch.exists() else None,
                          olean_sha256=h.tasks.digest(olean) if ok else None,
                          log_sha256=h.tasks.digest(log), sorry_diagnostic=sorry,
                          reused_names=[f"{n}:{name}" for n, name in h.build.reused_names(source.read_text())])
            if reproduce and ok:
                audit_log = out / "logs" / f"{mid}-islattice.log"
                audit_command = launcher + ["lake", "env", "lean", "--run", str(audit_source), module]
                audit = run_log(audit_command, tree, env, audit_log)
                audit.update(command=audit_command, log_sha256=h.tasks.digest(audit_log))
                result["islattice_audit"] = audit
                result["successful"] = audit["exit_code"] == 0
            rows.append(result)
            audit_status = result.get("islattice_audit", {}).get("exit_code", "not run")
            say(f"{mid} build_exit={result['exit_code']} audit_exit={audit_status} "
                f"successful={result['successful']} seconds={result['seconds']}")
            if not result["successful"]:
                for line in h.build.first_errors(text, limit=6):
                    say("    " + line)
            write_json(out / "results.json", rows)
        successful = sum(r["successful"] for r in rows)
        say(f"DONE {successful}/{len(rows)} successful")
    receipt = dict(schema="rh-port-mirror-receipt-v1", target=args.packet,
                   issue=args.issue, source_method=selection["source_method"],
                   source_base_url=selection["source_base_url"], accepted_commit=selection["accepted_commit"],
                   fresh_project_objects=reproduce, toolchain=version, mathlib=mathlib,
                   lean_num_threads=1, started_utc=started, ended_utc=utc(), host=h.tasks.host_resources(),
                   selection_sha256=h.tasks.digest(selection_path(args)), project_config=selection["project_config"],
                   helper_sha256=selection["helper_sha256"], driver_sha256=h.tasks.digest(Path(__file__).resolve()),
                   ledger_sha256=selection["ledger_sha256"], target_sha256=selection["target_sha256"],
                   imports=selection["accepted_import_records"], modules=rows,
                   remaining=selection["remaining"], pruned=selection["pruned"],
                   already_ported=selection["already_ported"],
                   build_log_sha256=h.tasks.digest(out / f"{args.packet}-build.log"),
                   all_successful=successful == len(rows),
                   islattice_audit_sha256=h.tasks.digest(audit_source) if reproduce else None)
    shutil.copyfile(selection_path(args), out / "selection.json")
    write_json(out / f"{args.packet}-receipt.json", receipt)
    return 0 if receipt["all_successful"] else 1


def check(args, h, selection):
    imports = {r["id"]: r for r in selection["imports"]}
    for record in selection["accepted_import_records"]:
        require_hash(args.work / "lean" / h.build.source_relpath(imports[record["id"]]),
                     record["built_source_sha256"], h)
    out = args.out or args.work / f"check-{args.packet}-{time.time_ns()}"
    return build_all(args, h, selection, args.work / "lean", out, False)


def materialize_reproduction(args, h, selection):
    if args.out.exists():
        raise SystemExit(f"Reproduction output already exists: {args.out}")
    # The logs/receipt live in OUT/evidence; OUT/lean is a newly materialized tree.
    tree = args.out / "lean"
    tree.mkdir(parents=True)
    for row in selection["selected"] + selection["imports"]:
        rel = h.build.source_relpath(row)
        dest = tree / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.work / "pinned-sources" / rel, dest)
        folder = (args.patches or args.repo / "port/patches") if row in selection["selected"] else args.repo / "port/patches"
        patch = folder / f"{row['id']}.patch"
        if patch.exists():
            errors = list(h.build.validate_patch_content(patch, rel))
            if errors:
                raise SystemExit("\n".join(errors))
            print(h.build.apply_patch(tree, patch), flush=True)
        if row in selection["selected"] and dest.read_bytes() != (args.work / "lean" / rel).read_bytes():
            raise SystemExit(f"{row['id']}: candidate patch does not reproduce the edited work source")
    for name in selection["project_config"]:
        shutil.copyfile(args.work / "lean" / name, tree / name)
    (tree / ".lake").mkdir()
    (tree / ".lake/packages").symlink_to((args.work / "lean/.lake/packages").resolve(), target_is_directory=True)
    return tree


def reproduce(args, h, selection):
    tree = materialize_reproduction(args, h, selection)
    return build_all(args, h, selection, tree, args.out / "evidence", True)


def batch_reproduce(args, h, selection):
    tree = materialize_reproduction(args, h, selection)
    out = args.out / "evidence"
    (out / "logs").mkdir(parents=True)
    env = environment(args, h)
    version, mathlib = h.build.toolchain(tree, env, selection["pins"])
    mathlib_dir = tree / ".lake/packages/mathlib"
    if git(mathlib_dir, "status", "--porcelain", "--untracked-files=no").strip():
        raise SystemExit("Mathlib has tracked modifications")
    closure = selection["selected"] + selection["imports"]
    before = {r["id"]: h.tasks.digest(tree / h.build.source_relpath(r)) for r in closure}
    patches = args.patches or args.repo / "port/patches"
    patch_hashes = {r["id"]: h.tasks.digest(patches / f"{r['id']}.patch")
                    if (patches / f"{r['id']}.patch").exists() else None for r in selection["selected"]}
    launcher = ["elan", "run", selection["pins"]["lean"]]
    command = launcher + ["lake", "--no-ansi", "--no-cache", "build"] + [f"+{r['module']}:olean" for r in selection["selected"]]
    audit_source = out / "islattice_audit.lean"
    shutil.copyfile(args.repo / "port/islattice_audit.lean", audit_source)
    print(f"Fresh batch: {len(selection['selected'])} targets; LEAN_NUM_THREADS={env['LEAN_NUM_THREADS']}; log: {out / 'batch-build.log'}", flush=True)
    batch = run_log(command, tree, env, out / "batch-build.log", measure=True)
    batch["log_sha256"] = h.tasks.digest(out / "batch-build.log")
    text = re.sub(r"\x1b\[[0-9;]*m", "", (out / "batch-build.log").read_text(errors="replace"))
    lines = text.splitlines()
    sorry = bool(h.ci.SORRY_DIAG_RE.search(text))
    completion = [line for line in lines if line.startswith("Build completed successfully")]
    built_lines = {r["id"]: [line for line in lines if re.match(
        r"^\S+\s+\[\d+/\d+\]\s+Built\s+" + re.escape(r["module"]) + r"(?::olean)?(?:\s|$)", line)]
        for r in selection["selected"]}
    objects = {r["id"]: tree / ".lake/build/lib/lean" / (r["module"].replace(".", "/") + ".olean")
               for r in selection["selected"]}
    batch_ok = batch["exit_code"] == 0 and not sorry and bool(completion) and all(
        built_lines[mid] and obj.is_file() for mid, obj in objects.items())
    audit = dict(exit_code=None, status="not run because batch build did not meet all evidence checks")
    if batch_ok:
        print("Batch compilation passed; running one aggregate IsLattice audit.", flush=True)
        audit_command = launcher + ["lake", "--no-ansi", "env", "lean", "--run", str(audit_source)] + [r["module"] for r in selection["selected"]]
        audit = run_log(audit_command, tree, env, out / "islattice-audit.log", measure=True)
        audit["log_sha256"] = h.tasks.digest(out / "islattice-audit.log")
        expected = f"islattice-audit: {len(selection['selected'])} module(s), 0 hit(s)"
        audit["complete_zero_hit_summary"] = expected in (out / "islattice-audit.log").read_text().splitlines()
    audit_ok = audit["exit_code"] == 0 and audit.get("complete_zero_hit_summary", False)
    after = {r["id"]: h.tasks.digest(tree / h.build.source_relpath(r)) for r in closure}
    changed_sources = [mid for mid in before if before[mid] != after[mid]]
    changed_config = [name for name, digest in selection["project_config"].items() if h.tasks.digest(tree / name) != digest]
    mathlib_unchanged = (git(mathlib_dir, "rev-parse", "HEAD").decode().strip() == mathlib and
                         not git(mathlib_dir, "status", "--porcelain", "--untracked-files=no").strip())
    stable = not changed_sources and not changed_config and mathlib_unchanged
    rows = []
    for row in selection["selected"]:
        mid, module = row["id"], row["module"]
        patch = patches / f"{mid}.patch"
        patch_changed = patch_hashes[mid] != (h.tasks.digest(patch) if patch.exists() else None)
        ok = bool(batch_ok and audit_ok and stable and not patch_changed and built_lines[mid] and objects[mid].is_file())
        excerpt = out / "logs" / f"{mid}.log"
        excerpt.write_text("\n".join([
            f"Module: {mid} {module}",
            "This is a target-specific excerpt of ONE shared batch invocation, not an independent module invocation.",
            "Full command, warnings/errors and transcript: ../batch-build.log",
            f"Shared transcript SHA-256: {batch['log_sha256']}",
            *built_lines[mid], *completion,
            f"Shared batch exit code: {batch['exit_code']}",
            f"Aggregate IsLattice audit exit code: {audit['exit_code']}; full transcript: ../islattice-audit.log",
            f"Target Built line observed: {bool(built_lines[mid])}; .olean exists: {objects[mid].is_file()}",
            f"Source/configuration integrity preserved: {stable}; patch unchanged: {not patch_changed}",
            f"Module evidence successful: {ok}", ""]))
        rows.append(dict(id=mid, module=module, successful=ok, build_mode="shared_batch",
                         shared_build_exit_code=batch["exit_code"], built_lines=built_lines[mid],
                         source_sha256=row["source_sha256"], built_source_sha256=after[mid],
                         source_changed_during_build=before[mid] != after[mid], patch_changed_during_build=patch_changed,
                         patch_sha256=patch_hashes[mid], olean_sha256=h.tasks.digest(objects[mid]) if objects[mid].is_file() else None,
                         log_sha256=h.tasks.digest(excerpt), shared_log_sha256=batch["log_sha256"],
                         sorry_diagnostic=sorry,
                         islattice_audit=dict(scope="all selected modules together", exit_code=audit["exit_code"],
                                              successful=audit_ok, log_sha256=audit.get("log_sha256")),
                         reused_names=[f"{n}:{name}" for n, name in h.build.reused_names((tree / h.build.source_relpath(row)).read_text())]))
    for result in (batch, audit):
        sidecar = result.get("resources", {}).get("gnu_time_sidecar")
        if sidecar:
            result["resources"]["gnu_time_sidecar_sha256"] = h.tasks.digest(out / sidecar)
    successful = sum(r["successful"] for r in rows)
    summary = out / f"{args.packet}-build.log"
    summary.write_text("\n".join([selection["source_method"],
        f"One shared fresh batch invocation; toolchain: {version}; Mathlib: {mathlib}",
        f"LEAN_NUM_THREADS={env['LEAN_NUM_THREADS']}; command: {shlex.join(command)}",
        *[f"{r['id']} built_line={bool(r['built_lines'])} successful={r['successful']}" for r in rows],
        f"Shared batch exit={batch['exit_code']}; sorry={sorry}; aggregate audit exit={audit['exit_code']}",
        f"DONE {successful}/{len(rows)} successful", ""]))
    receipt = dict(schema="rh-port-mirror-batch-receipt-v1", target=args.packet, issue=args.issue,
                   source_method=selection["source_method"], source_base_url=selection["source_base_url"],
                   accepted_commit=selection["accepted_commit"], fresh_project_objects=True,
                   toolchain=version, mathlib=mathlib, lean_num_threads_environment=int(env["LEAN_NUM_THREADS"]),
                   concurrency_note="LEAN_NUM_THREADS configures Lake's worker budget; it is not the per-compiler Lean thread count or a universal child-process semaphore.",
                   started_utc=batch["started_utc"], ended_utc=utc(), host=h.tasks.host_resources(),
                   batch_build=batch, aggregate_islattice_audit=audit, modules=rows,
                   changed_sources=changed_sources, changed_config=changed_config, mathlib_unchanged=mathlib_unchanged,
                   selection_sha256=h.tasks.digest(selection_path(args)), project_config=selection["project_config"],
                   helper_sha256=selection["helper_sha256"], driver_sha256=h.tasks.digest(Path(__file__).resolve()),
                   ledger_sha256=selection["ledger_sha256"], target_sha256=selection["target_sha256"],
                   imports=selection["accepted_import_records"], remaining=selection["remaining"],
                   pruned=selection["pruned"], already_ported=selection["already_ported"],
                   build_log_sha256=h.tasks.digest(summary), islattice_audit_sha256=h.tasks.digest(audit_source),
                   all_successful=successful == len(rows))
    shutil.copyfile(selection_path(args), out / "selection.json")
    write_json(out / "results.json", rows)
    write_json(out / f"{args.packet}-receipt.json", receipt)
    print(f"DONE {successful}/{len(rows)} successful; evidence: {out}", flush=True)
    return 0 if receipt["all_successful"] else 1


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", type=Path, required=True)
    common.add_argument("--work", type=Path, required=True)
    common.add_argument("--packet", required=True)
    common.add_argument("--patches", type=Path)
    common.add_argument("--issue", help="Reservation issue URL, recorded verbatim")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", parents=[common])
    p.add_argument("--accepted-ref", default="origin/main")
    p.add_argument("--fetch-workers", type=int, default=4)
    p = sub.add_parser("check", parents=[common])
    p.add_argument("--out", type=Path)
    sub.add_parser("diff", parents=[common])
    p = sub.add_parser("reproduce", parents=[common])
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("batch-reproduce", parents=[common])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--lean-threads", type=int, default=2,
                   help="LEAN_NUM_THREADS environment / Lake worker budget (not per-compiler threads; default: 2)")
    args = parser.parse_args()
    for name in ("repo", "work", "patches", "out"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(args, name, value.resolve())
    h = helpers(args.repo)
    if args.command == "prepare":
        prepare(args, h)
        return 0
    selected = load_selection(args, h)
    if args.command == "diff":
        make_diffs(args, h, selected)
        return 0
    if args.command == "batch-reproduce":
        if args.lean_threads < 1:
            raise SystemExit("--lean-threads must be positive")
        return batch_reproduce(args, h, selected)
    return check(args, h, selected) if args.command == "check" else reproduce(args, h, selected)


if __name__ == "__main__":
    sys.exit(main())
