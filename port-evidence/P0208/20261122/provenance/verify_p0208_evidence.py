#!/usr/bin/env python3
"""Read-only, independent gate for P0208's 48-target build and 15-target continuation.

Default: verify both completed phases. --phase1-only verifies the first checkpoint.
--phase1-evidence selects either the original successful or repaired evidence directory.
--extension-evidence selects either the original successful or repaired continuation.
No Lean/Lake commands, writes, imports of the drivers, or network requests are made.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
LEAN = "leanprover/lean4:v4.36.0"
MATHLIB = "fc4f59e577feb1a4956671bc04ac18aaaf8cd842"
BASE = "5a067f8d23e12b7c98be83b12add05a724e8177a"
EXT_BASE = "82f522b09a3e0ef920716dada3753e9b30f385bf"
ISSUE = "https://github.com/rohanarun/alpoge-rh-replay/issues/28141"
NEW = set("M131597 M131764 M131872 M133286 M133296 M133354 M133390 M133750 "
          "M133896 M133979 M134003 M134040 M134090 M134230 M134329".split())
M130315_SOURCE = "d2246966c28be0da4adf4c3cccbc8c61330fe0d84aebe26782412de810de950b"
M130315_REPLACEMENTS = (
    (b"Polynomial.coeff_monomial", b"Polynomial.coeff_monomial_apply", 7),
    (b"AddSubmonoidClass.coe_finset_sum", b"AddSubmonoidClass.coe_finsetSum", 1),
)
EXT_REPAIRS = {"M131597", "M131764"}
M131764_SOURCE = "6e54cbb0fbd6e056155e66328795bcc7402538b658013da311e307cb5098d79f"
M131764_DELETIONS = ((32, b"      | zero => simp\n"), (263, b"  | zero => simp\n"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    require(path.is_file(), f"Missing/incomplete evidence: {path}")
    return json.loads(path.read_bytes())


def check(path, expected):
    require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected), f"Invalid digest: {path}")
    require(path.is_file() and sha(path.read_bytes()) == expected, f"Missing file or digest mismatch: {path}")


def index(rows):
    result = {row["id"]: row for row in rows}
    require(len(result) == len(rows), "Duplicate module IDs")
    return result


def git(repo, *args):
    return subprocess.check_output(["git", "--no-optional-locks", "-C", str(repo), *args], stderr=subprocess.PIPE)


def apply_diff(original, patch, relative):
    """Apply a single-file unified diff in memory, without offsets, fuzz or newline normalization."""
    lines = []
    for line in patch.decode().splitlines(keepends=True):
        if line.startswith("\\ No newline at end of file"):
            require(bool(lines) and lines[-1].endswith("\n"), "Invalid no-newline marker")
            lines[-1] = lines[-1][:-1]
        else:
            lines.append(line)
    first = next((i for i, line in enumerate(lines) if line.startswith("@@ ")), -1)
    require(first >= 0, "Patch has no unified hunk")
    headers = lines[:first]
    for marker, expected in (("--- ", "a/" + relative), ("+++ ", "b/" + relative)):
        names = [line[4:].rstrip("\r\n").split("\t", 1)[0] for line in headers if line.startswith(marker)]
        require(names == [expected], f"Unexpected patch path: {names}")
    source = original.decode().splitlines(keepends=True)
    output, cursor, i = [], 0, first
    while i < len(lines):
        match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[i])
        require(match is not None, f"Invalid hunk header: {lines[i]!r}")
        old_count, new_count = int(match[2] or 1), int(match[4] or 1)
        position = int(match[1]) - bool(old_count)
        require(cursor <= position <= len(source), "Invalid/overlapping hunk position")
        output.extend(source[cursor:position]); cursor = position
        require(len(output) == int(match[3]) - bool(new_count), "New hunk position mismatch")
        old_seen = new_seen = 0; i += 1
        while i < len(lines) and not lines[i].startswith("@@ "):
            line = lines[i]; require(line[:1] in (" ", "+", "-"), "Unexpected unified-diff line")
            if line[0] in " -":
                require(cursor < len(source) and source[cursor] == line[1:], "Patch context differs from pinned source")
                cursor += 1; old_seen += 1
            if line[0] in " +":
                output.append(line[1:]); new_seen += 1
            i += 1
        require((old_seen, new_seen) == (old_count, new_count), "Hunk line counts mismatch")
    return "".join(output + source[cursor:]).encode()


def nonproof_text(data):
    """Preserve declaration headers and definitions, omitting only indented theorem proofs."""
    kept, header, in_proof = [], False, False
    for line in data.decode().splitlines(keepends=True):
        if line.strip() and not line[0].isspace():
            in_proof = False
            header = bool(re.match(r"^(?:(?:private|protected)\s+)*(?:theorem|lemma)\b", line))
        if in_proof and (not line.strip() or line[0].isspace()):
            continue
        kept.append(line)
        if header and re.search(r":=\s*by\s*(?:--[^\n]*)?\n?$", line):
            in_proof, header = True, False
    return "".join(kept).encode()


def verify_m130315_transform(original, built):
    """Allow only the eight manually reviewed proof-lemma substitutions in this pinned file."""
    require(sha(original) == M130315_SOURCE, "M130315 exception requires the reviewed pinned source")
    expected = original
    for old, new, count in M130315_REPLACEMENTS:
        require(original.count(old) == count, f"M130315 reviewed replacement count differs: {old.decode()}")
        expected = expected.replace(old, new)
    require(built == expected, "M130315 contains changes beyond the eight reviewed proof-lemma substitutions")


def verify_m131764_transform(original, built):
    """Delete only the two reviewed obsolete induction branches; preserve every other byte."""
    require(sha(original) == M131764_SOURCE, "M131764 exception requires the reviewed pinned source")
    lines = original.splitlines(keepends=True)
    for number, expected in M131764_DELETIONS:
        require(len(lines) >= number and lines[number - 1] == expected,
                f"M131764 reviewed deletion differs at original line {number}")
    removed = {number for number, _ in M131764_DELETIONS}
    expected = b"".join(line for number, line in enumerate(lines, 1) if number not in removed)
    require(built == expected, "M131764 contains changes beyond the two reviewed induction-branch deletions")


class Gate:
    def __init__(self, args):
        self.root = args.workspace.resolve()
        self.repo = self.root / "port-update-base"
        self.run = self.root / "port-P0208-verified"
        self.tree = self.run / "lean"
        self.ev = (args.phase1_evidence or self.run / "evidence").resolve()
        self.ext = (args.extension_evidence or self.root / "evidence-extension").resolve()
        self.drivers = (args.drivers or self.root / "port-tools").resolve()
        self.patches = (args.patches or self.root / "alpoge-rh-replay/port/patches").resolve()
        self.blobs, self.tracked, self.sources, self.patch_hashes = {}, {}, {}, {}

    def blob(self, commit, relative):
        key = (commit, relative)
        if key not in self.blobs:
            self.blobs[key] = git(self.repo, "show", f"{commit}:{relative}")
        return self.blobs[key]

    def paths(self, commit):
        if commit not in self.tracked:
            self.tracked[commit] = set(git(self.repo, "ls-tree", "-r", "--name-only", commit).decode().splitlines())
        return self.tracked[commit]

    def closure(self, ids):
        seen, todo = set(), list(ids)
        while todo:
            mid = todo.pop()
            if mid not in seen:
                seen.add(mid); todo.extend(self.ledger[mid]["imports"])
        return seen

    def rel(self, mid):
        return self.ledger[mid]["module"].replace(".", "/")

    def object(self, mid):
        return self.tree / ".lake/build/lib/lean" / (self.rel(mid) + ".olean")

    def source(self, mid, pinned_root, patch, expected=None):
        relative = "src/" + self.rel(mid) + ".lean"
        original = (pinned_root / relative).read_bytes()
        require(sha(original) == self.ledger[mid]["source_sha256"], f"Pinned source mismatch: {mid}")
        built = apply_diff(original, patch, relative) if patch is not None else original
        value = sha(built)
        check(self.tree / relative, value)
        require(expected is None or value == expected, f"Built-source/patch mismatch: {mid}")
        self.sources[mid] = value; self.patch_hashes[mid] = sha(patch) if patch is not None else None

    def imports(self, records, commit, pinned_root):
        for mid, record in index(records).items():
            row = self.ledger[mid]; relative = f"port/patches/{mid}.json"
            data = self.blob(commit, relative)
            require(sha(data) == record["record_sha256"], f"Accepted record mismatch: {mid}")
            saved = json.loads(data)
            for key, value in (("module", row["module"]), ("source_sha256", row["source_sha256"]), ("lean", LEAN), ("mathlib", MATHLIB)):
                require(saved[key] == value, f"Accepted record {key} mismatch: {mid}")
            relative = f"port/patches/{mid}.patch"
            patch = self.blob(commit, relative) if relative in self.paths(commit) else None
            require((sha(patch) if patch is not None else None) == record["patch_sha256"], f"Accepted patch mismatch: {mid}")
            require((saved["notes"] == "unchanged") == (patch is None), f"Accepted notes/patch mismatch: {mid}")
            self.source(mid, pinned_root, patch, record.get("built_source_sha256"))

    def built_lines(self, text, mid):
        pattern = r"^\S+\s+\[\d+/\d+\]\s+Built\s+" + re.escape(self.ledger[mid]["module"]) + r"(?::olean)?(?:\s|$)"
        return [line for line in text.splitlines() if re.match(pattern, line)]

    def log(self, ev, name, result, command, expected_exit=0, allow_errors=False):
        require(result["exit_code"] == expected_exit and result["command"] == command, f"Exit/command mismatch: {ev / name}")
        check(ev / name, result["log_sha256"])
        text = (ev / name).read_text()
        for line in (f"== command: {shlex.join(command)}", f"== execution_command: {shlex.join(result['execution_command'])}",
                     f"== started_utc: {result['started_utc']}", f"== ended_utc: {result['ended_utc']}", f"== exit_code: {expected_exit}"):
            require(text.splitlines().count(line) == 1, f"Log metadata mismatch: {ev / name}: {line}")
        require(not re.search(r"declaration uses 'sorry(?:Ax)?'", text), f"Sorry diagnostic: {ev / name}")
        require(allow_errors or not re.search(r"^error:", text, re.M), f"Error diagnostic: {ev / name}")
        resource = result.get("resources", {})
        if "gnu_time_sidecar" in resource:
            require(Path(resource["gnu_time_sidecar"]).name == resource["gnu_time_sidecar"], "Invalid resource sidecar path")
            check(ev / resource["gnu_time_sidecar"], resource["gnu_time_sidecar_sha256"])
            expected_execution = ["/usr/bin/time", "-v", "-o", str(ev / resource["gnu_time_sidecar"]), *command]
        else:
            expected_execution = command
        require(result["execution_command"] == expected_execution, f"Unexpected executable wrapper: {ev / name}")
        require(f"== cwd: {self.tree}" in text.splitlines(), f"Wrong build/audit working directory: {ev / name}")
        return text

    def phase(self, r, ev, ids, audit_ids, pinned_root, mode, original=None):
        require(r["target"] == "P0208" and r["issue"] == ISSUE and r["all_successful"], "Wrong or unsuccessful packet receipt")
        require(r["toolchain"].startswith("Lean (version 4.36.0,") and r["mathlib"] == MATHLIB, "Compiler pins differ")
        require(r["mathlib_unchanged"] is True, "Mathlib changed during build")
        require(r["project_config"] == self.selection["project_config"], "Project configuration receipt mismatch")
        for name, value in r["project_config"].items(): check(self.tree / name, value)
        check(self.drivers / "packet_driver.py", r["driver_sha256"])
        launcher = ["elan", "run", LEAN, "lake", "--no-ansi"]
        command = launcher + ["--no-cache", "build"] + [f"+{self.ledger[mid]['module']}:olean" for mid in ids]
        text = self.log(ev, "batch-build.log", r["batch_build"], command)
        require(any(line.startswith("Build completed successfully") for line in text.splitlines()), "Missing successful build completion")
        command = launcher + ["env", "lean", "--run", str(ev / "islattice_audit.lean")] + [self.ledger[mid]["module"] for mid in audit_ids]
        audit = self.log(ev, "islattice-audit.log", r["aggregate_islattice_audit"], command)
        for transcript in (text, audit):
            require(f"== LEAN_NUM_THREADS: {r['lean_num_threads_environment']}" in transcript.splitlines(), "Worker-budget metadata mismatch")
        require(r["aggregate_islattice_audit"]["complete_zero_hit_summary"] is True, "Audit summary not successful")
        require(f"islattice-audit: {len(audit_ids)} module(s), 0 hit(s)" in audit.splitlines(), "Wrong audit scope/result")
        check(ev / "islattice_audit.lean", r["islattice_audit_sha256"])
        require(r["islattice_audit_sha256"] == self.selection["helper_sha256"]["port/islattice_audit.lean"], "Audit source changed")
        modules = index(r["modules"])
        require(set(modules) == set(ids) and load(ev / "results.json") == r["modules"], "Target/results scope mismatch")
        for mid, m in modules.items():
            require(m["module"] == self.ledger[mid]["module"] and m["source_sha256"] == self.ledger[mid]["source_sha256"], f"Module identity mismatch: {mid}")
            require(m["successful"] and m["shared_build_exit_code"] == 0 and m["build_mode"] == mode, f"Unsuccessful target: {mid}")
            producing_text = text
            if original is not None:
                attempt = "original" if mid in r["retained_target_ids"] else "retry"
                producing = original["receipt"]["batch_build"] if attempt == "original" else r["batch_build"]
                producing_text = original["text"] if attempt == "original" else text
                require(m["compilation_attempt"] == attempt and m["compilation_log_sha256"] == producing["log_sha256"]
                        and m["compilation_batch_exit_code"] == producing["exit_code"], f"Producing attempt mismatch: {mid}")
            require(m["built_lines"] and m["built_lines"] == self.built_lines(producing_text, mid), f"Producing Built evidence missing: {mid}")
            require(not m.get("sorry_diagnostic") and not m.get("source_changed_during_build") and not m.get("patch_changed_during_build") and not m["reused_names"], f"Flagged target: {mid}")
            require(m["shared_log_sha256"] == r["batch_build"]["log_sha256"], f"Shared log mismatch: {mid}")
            a = m["islattice_audit"]
            require(a["exit_code"] == 0 and a["successful"] and a["log_sha256"] == r["aggregate_islattice_audit"]["log_sha256"], f"Target audit mismatch: {mid}")
            patch_path = self.patches / f"{mid}.patch"
            patch = patch_path.read_bytes() if patch_path.exists() else None
            require((sha(patch) if patch is not None else None) == m["patch_sha256"], f"Target patch mismatch: {mid}")
            self.source(mid, pinned_root, patch, m["built_source_sha256"])
            check(self.object(mid), m["olean_sha256"]); check(ev / "logs" / f"{mid}.log", m["log_sha256"])
            require("Module evidence successful: True" in (ev / "logs" / f"{mid}.log").read_text(), f"Incomplete module excerpt: {mid}")
        return text

    def original_attempt(self, r, selected):
        info = r["original_attempt"]
        path = Path(info["receipt_path"]).resolve()
        require(path == self.run / "evidence/P0208-receipt.json" and path.parent != self.ev,
                "Original-attempt path must identify the untouched initial receipt")
        check(path, info["receipt_sha256"])
        original = load(path); ev = path.parent
        require(info["archived_receipt_path"] == "original-attempt/P0208-receipt.json"
                and info["batch_log_path"] == "original-attempt/batch-build.log",
                "Unexpected original-attempt archive paths")
        archive = self.ev / "original-attempt"
        require(r["original_attempt_unchanged"] is True, "Original attempt changed during repair")
        initial_files = {p.relative_to(ev).as_posix(): sha(p.read_bytes()) for p in ev.rglob("*") if p.is_file()}
        archived_files = {p.relative_to(archive).as_posix(): sha(p.read_bytes()) for p in archive.rglob("*") if p.is_file()}
        require(initial_files == archived_files, "Original-attempt archive is incomplete or changed")
        require(original["schema"] == "rh-port-mirror-batch-receipt-v1"
                and original["fresh_project_objects"] is True and info["fresh_project_objects"] is True
                and original["all_successful"] is False, "Original attempt was not an unsuccessful fresh batch")
        require(original["source_method"] == self.selection["source_method"], "Original source-method provenance differs")
        for key in ("target", "issue", "accepted_commit", "source_base_url", "toolchain", "mathlib",
                    "selection_sha256", "project_config", "helper_sha256", "driver_sha256", "ledger_sha256",
                    "target_sha256", "imports", "remaining", "pruned", "already_ported", "islattice_audit_sha256"):
            require(original[key] == r[key], f"Repair changed original provenance: {key}")
        require(not original["changed_sources"] and not original["changed_config"] and original["mathlib_unchanged"] is True,
                "Original attempt changed its inputs")
        check(ev / "selection.json", original["selection_sha256"])
        require(load(ev / "selection.json") == self.selection, "Original selection differs")
        check(ev / "P0208-build.log", original["build_log_sha256"])
        check(ev / "islattice_audit.lean", original["islattice_audit_sha256"])
        audit = original["aggregate_islattice_audit"]
        require(audit["exit_code"] is None and not audit.get("command") and not audit.get("log_sha256")
                and not (ev / "islattice-audit.log").exists(), "Original failed batch must not claim an audit")
        batch = original["batch_build"]; code = batch["exit_code"]
        require(type(code) is int and code != 0 and info["batch_exit_code"] == code
                and info["batch_log_sha256"] == batch["log_sha256"], "Original failure/exit provenance mismatch")
        command = ["elan", "run", LEAN, "lake", "--no-ansi", "--no-cache", "build"] + [
            f"+{self.ledger[mid]['module']}:olean" for mid in selected]
        text = self.log(ev, "batch-build.log", batch, command, expected_exit=code, allow_errors=True)
        require(not any(line.startswith("Build completed successfully") for line in text.splitlines())
                and re.search(r"^error:", text, re.M), "Original failure transcript is inconsistent")
        require(f"== LEAN_NUM_THREADS: {original['lean_num_threads_environment']}" in text.splitlines(),
                "Original worker-budget metadata mismatch")
        modules = index(original["modules"])
        require(set(modules) == set(selected) and load(ev / "results.json") == original["modules"],
                "Original target/results scope mismatch")
        for mid, m in modules.items():
            require(m["module"] == self.ledger[mid]["module"] and m["source_sha256"] == self.ledger[mid]["source_sha256"]
                    and m["built_source_sha256"] == self.ledger[mid]["source_sha256"] and m["patch_sha256"] is None,
                    f"Original target identity/source differs: {mid}")
            require(m["successful"] is False and m["build_mode"] == "shared_batch" and m["shared_build_exit_code"] == code
                    and m["shared_log_sha256"] == batch["log_sha256"] and m["built_lines"] == self.built_lines(text, mid),
                    f"Original target transcript mismatch: {mid}")
            require(not m["source_changed_during_build"] and not m["patch_changed_during_build"]
                    and not m["sorry_diagnostic"] and not m["reused_names"], f"Original target was flagged: {mid}")
            a = m["islattice_audit"]
            require(a["exit_code"] is None and a["successful"] is False and a["log_sha256"] is None,
                    f"Original target claimed a successful audit: {mid}")
            check(ev / "logs" / f"{mid}.log", m["log_sha256"])
            require("Module evidence successful: False" in (ev / "logs" / f"{mid}.log").read_text(),
                    f"Original excerpt concealed unsuccessful batch: {mid}")
        retained, rebuilt = r["retained_target_ids"], r["rebuilt_target_ids"]
        require(len(retained) == len(set(retained)) and len(rebuilt) == len(set(rebuilt)) and rebuilt
                and not set(retained) & set(rebuilt) and set(retained) | set(rebuilt) == set(selected),
                "Repair target partition mismatch")
        for mid in retained:
            require(modules[mid]["built_lines"] and modules[mid]["olean_sha256"], f"Retained target lacks original output: {mid}")
        return {"receipt": original, "text": text, "retained": set(retained), "rebuilt": set(rebuilt)}

    def repaired_outputs(self, r, original, text):
        check(self.drivers / "packet_repair.py", r["repair_driver_sha256"])
        check(self.ev / "patch-notes.json", r["patch_notes_sha256"])
        check(self.ev / "proof-only-reviews.json", r["proof_only_reviews_sha256"])
        for key in ("unexpected_rebuilt_retained", "changed_retained_outputs", "changed_sources", "changed_config", "changed_patches"):
            require(not r[key], f"Repair changed retained evidence: {key}")
        check(self.ev / "reuse-before.json", r["reuse_before_sha256"])
        check(self.ev / "reuse-after.json", r["reuse_after_sha256"])
        before, after = load(self.ev / "reuse-before.json"), load(self.ev / "reuse-after.json")
        retained = self.old_ids - original["rebuilt"]
        require(before == after and set(before) == retained and r["retained_project_module_count"] == len(retained),
                "Repair retained-output chain changed")
        modules, old_modules = index(r["modules"]), index(original["receipt"]["modules"])
        changed = {mid for mid in modules if modules[mid]["built_source_sha256"] != old_modules[mid]["built_source_sha256"]}
        require(set(r["repaired_target_ids"]) == changed and changed <= original["rebuilt"]
                and all(modules[mid]["patch_sha256"] for mid in changed), "Repair patch/source scope mismatch")
        notes = load(self.ev / "patch-notes.json")
        require(set(notes) == changed and all(isinstance(note, str) and note.strip() and note != "unchanged"
                                            for note in notes.values()), "Repair notes scope/content mismatch")
        patched = {mid for mid, m in modules.items() if m["patch_sha256"]}
        require({p.stem for p in (self.ev / "patches").glob("*.patch")} == patched, "Frozen target-patch scope mismatch")
        for mid in patched: check(self.ev / "patches" / f"{mid}.patch", modules[mid]["patch_sha256"])
        reviews = load(self.ev / "proof-only-reviews.json")
        require(set(reviews) == changed and reviews == r["proof_only_reviews"], "Proof-only review scope/content mismatch")
        for mid, review in reviews.items():
            relative = "src/" + self.rel(mid) + ".lean"
            pinned = (self.root / "port-work/pinned-sources" / relative).read_bytes()
            built = (self.tree / relative).read_bytes()
            skeleton = nonproof_text(pinned)
            expected = dict(notes=notes[mid], nonproof_skeleton_sha256=sha(skeleton),
                            statement_and_definition_text_unchanged=True, source_sha256=modules[mid]["source_sha256"],
                            built_source_sha256=modules[mid]["built_source_sha256"], patch_sha256=modules[mid]["patch_sha256"])
            if mid == "M130315":
                require(modules[mid]["source_sha256"] == M130315_SOURCE, "M130315 ledger source differs from reviewed pin")
                verify_m130315_transform(pinned, built)
                require(all(term in notes[mid] for term in ("comp", "coord", "gradedRing", "proof irrelevance", "Subtype.ext")),
                        "M130315 notes must explain the definition/instance proof-field equivalence")
                expected.update(
                    review_method="exact_reviewed_identifier_replacements",
                    reviewed_replacements=[dict(old=old.decode(), new=new.decode(), count=count)
                                           for old, new, count in M130315_REPLACEMENTS],
                    statement_and_definition_text_unchanged=False,
                    theorem_statement_text_unchanged=True,
                    definition_and_instance_data_unchanged=True,
                    patched_nonproof_skeleton_sha256=sha(nonproof_text(built)),
                    equivalence_note="The polynomial data in comp and coord are byte-identical; only their subtype membership proofs change, so the resulting subtype values are equal by Subtype.ext rfl and proof irrelevance. In gradedRing, only the mul_mem proof field changes; all data and other instance fields remain byte-identical, so proof irrelevance preserves the instance.")
            else:
                require(skeleton == nonproof_text(built), f"Declaration text changed: {mid}")
            require(review == expected, f"Proof-only review differs from source/patch evidence: {mid}")
        edges = [[mid, dep] for mid in modules for dep in sorted(self.closure(self.ledger[mid]["imports"]) & set(modules))]
        require(r["target_dependency_edges"] == edges == [], "Target dependencies invalidate retained outputs")
        expected_retained = {mid for mid, m in old_modules.items() if m["built_lines"] and m["olean_sha256"] and mid not in patched}
        require(original["retained"] == expected_retained, "Retained-target classification differs from original evidence")
        for mid, snap in before.items():
            require(self.built_lines(original["text"], mid) and not self.built_lines(text, mid),
                    f"Retained module lacks initial fresh build or was rebuilt: {mid}")
            require(snap["module"] == self.ledger[mid]["module"] and snap["source_sha256"] == self.sources[mid],
                    f"Repair retained source mismatch: {mid}")
            paths = [self.object(mid)] + [Path(str(self.object(mid)) + suffix) for suffix in (".private", ".server")
                                         if Path(str(self.object(mid)) + suffix).is_file()]
            require(set(snap["artifacts"]) == {p.relative_to(self.tree).as_posix() for p in paths},
                    f"Repair retained artifact scope mismatch: {mid}")
            for relative, value in snap["artifacts"].items(): check(self.tree / relative, value)
            if mid in original["retained"]:
                obj_hash = snap["artifacts"][self.object(mid).relative_to(self.tree).as_posix()]
                require(obj_hash == old_modules[mid]["olean_sha256"] == modules[mid]["olean_sha256"]
                        and old_modules[mid]["built_source_sha256"] == modules[mid]["built_source_sha256"]
                        and old_modules[mid]["patch_sha256"] == modules[mid]["patch_sha256"],
                        f"Retained target differs from its original output: {mid}")
        for mid in original["rebuilt"]:
            require(self.built_lines(text, mid), f"Rebuilt target lacks fresh retry evidence: {mid}")

    def phase1(self):
        r = load(self.ev / "P0208-receipt.json")
        repaired = r["schema"] == "rh-port-repaired-batch-receipt-v1"
        require(r["schema"] in ("rh-port-mirror-batch-receipt-v1", "rh-port-repaired-batch-receipt-v1")
                and r["accepted_commit"] == BASE and r["fresh_project_objects"] is (not repaired),
                "Wrong phase1 schema/base/freshness")
        check(self.ev / "selection.json", r["selection_sha256"])
        self.selection = s = load(self.ev / "selection.json")
        require(s["accepted_commit"] == BASE and s["packet"] == "P0208", "Wrong frozen selection")
        data = self.blob(BASE, "tracker/modules.jsonl.gz")
        require(sha(data) == r["ledger_sha256"] == s["ledger_sha256"], "Ledger digest mismatch")
        self.ledger = index([json.loads(line) for line in gzip.decompress(data).splitlines()])
        target = self.blob(BASE, "port/target.json")
        require(sha(target) == r["target_sha256"] == s["target_sha256"] and json.loads(target) == s["pins"], "Target pins mismatch")
        require(s["pins"]["lean"] == LEAN and s["pins"]["mathlib"] == MATHLIB, "Wrong target toolchain")
        require(r["helper_sha256"] == s["helper_sha256"], "Helper identities differ")
        for name, value in s["helper_sha256"].items(): require(sha(self.blob(BASE, name)) == value, f"Helper mismatch: {name}")
        selected, imports = index(s["selected"]), index(s["imports"])
        require(len(selected) == 48 and len(imports) == 64 and not set(selected) & set(imports), "Wrong phase1 scope")
        require(all(self.ledger[mid]["packet"] == "P0208" for mid in selected), "Target is outside P0208")
        self.old_ids = set(selected) | set(imports)
        require(self.closure(selected) == self.old_ids, "Phase1 import closure mismatch")
        for mid, row in {**selected, **imports}.items(): require(row == self.ledger[mid], f"Ledger row mismatch: {mid}")
        require(index(r["imports"]) == index(s["accepted_import_records"]) and set(index(r["imports"])) == set(imports), "Phase1 import records mismatch")
        require(not r["changed_sources"] and not r["changed_config"], "Phase1 inputs changed")
        for key in ("remaining", "pruned", "already_ported"): require(r[key] == s[key], f"Phase1 {key} mismatch")
        self.imports(r["imports"], BASE, self.root / "port-work/pinned-sources")
        original = self.original_attempt(r, selected) if repaired else None
        text = self.phase(r, self.ev, list(selected), list(selected), self.root / "port-work/pinned-sources",
                          "shared_repaired_batch" if repaired else "shared_batch", original)
        check(self.ev / "P0208-build.log", r["build_log_sha256"])
        if repaired:
            self.repaired_outputs(r, original, text)
            print(f"PASS original failure provenance and repair: {len(original['retained'])} retained targets, {len(original['rebuilt'])} freshly rebuilt targets")
        else:
            for mid in self.old_ids: require(self.built_lines(text, mid) and self.object(mid).is_file(), f"Missing fresh phase1 import output: {mid}")
        print("PASS phase1: 48 target receipts/objects/logs, 112 verified project sources, exact 48-module audit")
        return r, selected

    def reuse_chain(self, ev, receipt, expected_ids):
        check(ev / "reuse-before.json", receipt["reuse_before_sha256"])
        check(ev / "reuse-after.json", receipt["reuse_after_sha256"])
        before, after = load(ev / "reuse-before.json"), load(ev / "reuse-after.json")
        require(before == after and set(before) == set(expected_ids), f"Retained-output chain changed: {ev}")
        for mid, snap in before.items():
            require(snap["module"] == self.ledger[mid]["module"] and snap["source_sha256"] == self.sources[mid],
                    f"Reused source mismatch: {mid}")
            paths = [self.object(mid)] + [Path(str(self.object(mid)) + suffix) for suffix in (".private", ".server")
                                         if Path(str(self.object(mid)) + suffix).is_file()]
            require(set(snap["artifacts"]) == {p.relative_to(self.tree).as_posix() for p in paths},
                    f"Reused artifact scope mismatch: {mid}")
            for relative, value in snap["artifacts"].items(): check(self.tree / relative, value)
        return before

    def failed_extension(self, r, ev, new_ids):
        info = r["original_attempt"]
        path = Path(info["receipt_path"]).resolve()
        require(path == self.root / "evidence-extension/P0208-extension-receipt.json" and path.parent != ev,
                "Original continuation path must identify the untouched failed receipt")
        check(path, info["receipt_sha256"])
        raw, raw_ev = load(path), path.parent
        require(info["archived_receipt_path"] == "original-attempt/P0208-extension-receipt.json"
                and info["batch_log_path"] == "original-attempt/batch-build.log"
                and info["fresh_project_objects"] is False and r["original_attempt_unchanged"] is True,
                "Original continuation archive/freshness metadata differs")
        archive = ev / "original-attempt"
        raw_files = {p.relative_to(raw_ev).as_posix(): sha(p.read_bytes()) for p in raw_ev.rglob("*") if p.is_file()}
        archived_files = {p.relative_to(archive).as_posix(): sha(p.read_bytes()) for p in archive.rglob("*") if p.is_file()}
        require(raw_files == archived_files, "Original continuation archive is incomplete or changed")
        require(raw["schema"] == "rh-port-continuation-receipt-v1" and raw["fresh_project_objects"] is False
                and raw["all_successful"] is False and raw["retained_project_module_count"] == 112
                and raw["combined_target_count"] == 48,
                "Original continuation is not the completed failed 112-output continuation")
        for key in ("target", "issue", "accepted_commit", "source_base_url", "phase1", "toolchain", "mathlib",
                    "project_config", "accepted_imports", "applied_patches", "new_project_module_count",
                    "packet_complete", "remaining", "input_manifest_sha256", "ledger_sha256",
                    "compatibility", "compatibility_sha256", "islattice_audit_sha256", "driver_sha256", "extension_driver_sha256"):
            require(raw[key] == r[key], f"Continuation repair changed original provenance: {key}")
        require(raw["mathlib_unchanged"] is True, "Original continuation changed Mathlib")
        for key in ("unexpected_rebuilt_existing", "changed_reused_outputs", "changed_sources", "changed_configuration", "changed_patches"):
            require(not raw[key], f"Original continuation changed its inputs: {key}")
        for name in ("phase1-receipt.json", "phase1-selection.json", "inputs-manifest.json", "compatibility.json", "islattice_audit.lean"):
            require((raw_ev / name).read_bytes() == (ev / name).read_bytes(), f"Continuation repair changed frozen file: {name}")
        audit = raw["aggregate_islattice_audit"]
        require(audit["exit_code"] is None and not audit.get("command") and not audit.get("log_sha256")
                and not (raw_ev / "islattice-audit.log").exists(), "Failed continuation must not claim an audit")
        batch = raw["batch_build"]
        require(batch["exit_code"] == info["batch_exit_code"] == 1
                and batch["log_sha256"] == info["batch_log_sha256"], "Original continuation failure provenance differs")
        command = ["elan", "run", LEAN, "lake", "--no-ansi", "--no-cache", "build"] + [
            f"+{self.ledger[mid]['module']}:olean" for mid in sorted(NEW)]
        text = self.log(raw_ev, "batch-build.log", batch, command, expected_exit=1, allow_errors=True)
        require(re.search(r"^error:", text, re.M) and not any(line.startswith("Build completed successfully") for line in text.splitlines()),
                "Original continuation failure transcript is inconsistent")
        require(f"== LEAN_NUM_THREADS: {raw['lean_num_threads_environment']}" in text.splitlines(),
                "Original continuation worker-budget metadata differs")
        require(not any(self.built_lines(text, mid) for mid in self.old_ids), "Original continuation rebuilt a phase1 output")
        modules = index(raw["modules"])
        require(set(modules) == NEW and load(raw_ev / "results.json") == raw["modules"], "Original continuation target scope differs")
        retained = NEW - EXT_REPAIRS
        for mid, m in modules.items():
            require(m["module"] == self.ledger[mid]["module"] and m["source_sha256"] == self.ledger[mid]["source_sha256"]
                    and m["built_source_sha256"] == m["source_sha256"] and m["patch_sha256"] is None,
                    f"Original continuation target source differs: {mid}")
            require(m["successful"] is False and m["build_mode"] == "shared_continuation_batch"
                    and m["shared_build_exit_code"] == 1 and m["shared_log_sha256"] == batch["log_sha256"]
                    and m["built_lines"] == self.built_lines(text, mid) and not m["reused_names"],
                    f"Original continuation target transcript differs: {mid}")
            require(bool(m["built_lines"]) == (mid in retained) and bool(m["olean_sha256"]) == (mid in retained),
                    f"Original continuation producing-target set differs: {mid}")
            a = m["islattice_audit"]
            require(a["exit_code"] is None and a["successful"] is False and a["log_sha256"] is None,
                    f"Original continuation target claimed an audit: {mid}")
            check(raw_ev / "logs" / f"{mid}.log", m["log_sha256"])
            require("Module evidence successful: False" in (raw_ev / "logs" / f"{mid}.log").read_text(),
                    f"Original continuation excerpt conceals failure: {mid}")
            if mid in retained: check(self.object(mid), m["olean_sha256"])
        imports = index(raw["built_imports"])
        require(set(imports) == new_ids - NEW and len(imports) == 35, "Original continuation import-output scope differs")
        for mid, row in imports.items():
            require(row["module"] == self.ledger[mid]["module"] and row["built_lines"]
                    and row["built_lines"] == self.built_lines(text, mid) and row["built_source_sha256"] == self.sources[mid],
                    f"Original continuation import evidence differs: {mid}")
            check(self.object(mid), row["olean_sha256"])
        reuse = self.reuse_chain(raw_ev, raw, self.old_ids)
        return {"receipt": raw, "text": text, "retained": retained, "rebuilt": EXT_REPAIRS, "reuse": reuse}

    def extension_repair_reviews(self, r, ev, modules):
        check(ev / "patch-notes.json", r["patch_notes_sha256"])
        check(ev / "proof-only-reviews.json", r["proof_only_reviews_sha256"])
        notes, reviews = load(ev / "patch-notes.json"), load(ev / "proof-only-reviews.json")
        require(set(notes) == set(reviews) == EXT_REPAIRS and reviews == r["proof_only_reviews"],
                "Continuation repair notes/review scope differs")
        require({p.stem for p in (ev / "patches").glob("*.patch")} == EXT_REPAIRS,
                "Continuation frozen target-patch scope differs")
        for mid in EXT_REPAIRS:
            require(isinstance(notes[mid], str) and "proof" in notes[mid].lower() and "statement" in notes[mid].lower(),
                    f"Continuation repair notes are incomplete: {mid}")
            check(ev / "patches" / f"{mid}.patch", modules[mid]["patch_sha256"])
            relative = "src/" + self.rel(mid) + ".lean"
            pinned = (self.root / "port-extension-inputs" / relative).read_bytes()
            built = (self.tree / relative).read_bytes()
            before, after = nonproof_text(pinned), nonproof_text(built)
            expected = dict(notes=notes[mid], nonproof_skeleton_sha256=sha(before),
                            statement_and_definition_text_unchanged=True, source_sha256=modules[mid]["source_sha256"],
                            built_source_sha256=modules[mid]["built_source_sha256"], patch_sha256=modules[mid]["patch_sha256"])
            if mid == "M131764":
                require(modules[mid]["source_sha256"] == M131764_SOURCE, "M131764 ledger source differs from reviewed pin")
                verify_m131764_transform(pinned, built)
                require(all(term in notes[mid] for term in ("dualDer", "Derivation.mk'", "proof irrelevance")),
                        "M131764 notes must explain the data-map/proof-field equivalence")
                expected.update(review_method="exact_reviewed_line_deletions",
                    reviewed_deletions=[dict(original_line=number, text=line.decode()) for number, line in M131764_DELETIONS],
                    statement_and_definition_text_unchanged=False, theorem_statement_text_unchanged=True,
                    definition_and_instance_data_unchanged=True, patched_nonproof_skeleton_sha256=sha(after),
                    equivalence_note="The dualDer data map and declared type remain byte-identical. Only the Prop-valued Leibniz-law proof passed to Derivation.mk' changes, so proof irrelevance preserves the derivation. TensorProduct.inductionOn now provides tmul and add cases and obtains zero from 0 tensor 0; deleting the obsolete zero alternatives preserves the theorem statements and definition data.")
            else:
                require(before == after, f"Continuation repair changed declaration text: {mid}")
            require(reviews[mid] == expected, f"Continuation proof review differs from source/patch evidence: {mid}")

    def repaired_extension_outputs(self, r, ev, original, text, new_ids):
        check(self.drivers / "packet_extension_repair.py", r["extension_repair_driver_sha256"])
        check(self.drivers / "packet_repair.py", r["repair_driver_sha256"])
        require(r["phase1_unchanged"] is True and r["helper_sha256"] == self.selection["helper_sha256"],
                "Continuation repair changed phase1 evidence or helper identities")
        for key in ("unexpected_rebuilt_retained", "changed_retained_outputs", "changed_config"):
            require(not r[key], f"Continuation repair changed retained inputs: {key}")
        require(set(r["retained_target_ids"]) == NEW - EXT_REPAIRS and len(r["retained_target_ids"]) == 13
                and set(r["rebuilt_target_ids"]) == set(r["repaired_target_ids"]) == EXT_REPAIRS
                and len(r["rebuilt_target_ids"]) == len(r["repaired_target_ids"]) == 2,
                "Continuation repair target partition differs")
        all_ids = self.old_ids | new_ids
        edges = [[mid, dep] for mid in sorted(all_ids) for dep in sorted(self.closure(self.ledger[mid]["imports"]) & EXT_REPAIRS)]
        require(r["target_dependency_edges"] == edges == [], "A retained project output depends on a repaired continuation target")
        retained = all_ids - EXT_REPAIRS
        require(len(all_ids) == r["project_source_count"] == 162 and len(retained) == r["retained_project_module_count"] == 160
                and r["phase1_project_module_count"] == len(self.old_ids) == 112 and r["accepted_import_count"] == 99,
                "Continuation repair project counts differ")
        for name in ("original", "before", "after"):
            check(ev / f"sources-{name}.json", r[f"sources_{name}_sha256"])
        original_sources = load(ev / "sources-original.json")
        before_sources, after_sources = load(ev / "sources-before.json"), load(ev / "sources-after.json")
        expected_sources = {mid: self.sources[mid] for mid in all_ids}
        expected_original = {mid: self.ledger[mid]["source_sha256"] if mid in EXT_REPAIRS else value
                             for mid, value in expected_sources.items()}
        require(original_sources == expected_original and before_sources == after_sources == expected_sources
                and r["source_changes_for_repair"] == sorted(EXT_REPAIRS), "Continuation repair source-hash chain differs")
        require(not any(self.built_lines(text, mid) for mid in retained), "Continuation retry rebuilt a retained output")
        snapshots = self.reuse_chain(ev, r, retained)
        require({mid: snapshots[mid] for mid in self.old_ids} == original["reuse"],
                "Continuation repair broke the original 112-output reuse chain")
        modules, old_modules = index(r["modules"]), index(original["receipt"]["modules"])
        changed = {mid for mid in NEW if modules[mid]["built_source_sha256"] != old_modules[mid]["built_source_sha256"]}
        require(changed == EXT_REPAIRS and {mid for mid in NEW if modules[mid]["patch_sha256"]} == EXT_REPAIRS,
                "Continuation target patch/source scope differs")
        for mid in NEW - EXT_REPAIRS:
            relative = self.object(mid).relative_to(self.tree).as_posix()
            require(modules[mid]["olean_sha256"] == old_modules[mid]["olean_sha256"] == snapshots[mid]["artifacts"][relative]
                    and modules[mid]["built_source_sha256"] == old_modules[mid]["built_source_sha256"]
                    and modules[mid]["patch_sha256"] is None, f"Retained continuation target differs: {mid}")
        for mid in EXT_REPAIRS: require(self.built_lines(text, mid), f"Missing fresh continuation retry output: {mid}")
        imports, old_imports = index(r["built_imports"]), index(original["receipt"]["built_imports"])
        require(set(imports) == set(old_imports) == new_ids - NEW, "Continuation repaired import scope differs")
        producer = original["receipt"]["batch_build"]
        for mid, row in imports.items():
            expected = {**old_imports[mid], "compilation_attempt": "original",
                        "compilation_log_sha256": producer["log_sha256"], "compilation_batch_exit_code": producer["exit_code"]}
            relative = self.object(mid).relative_to(self.tree).as_posix()
            require(row == expected and row["olean_sha256"] == snapshots[mid]["artifacts"][relative],
                    f"Retained continuation import producer differs: {mid}")
        self.extension_repair_reviews(r, ev, modules)

    def extension(self, p1, selected):
        ev = self.ext; r = load(ev / "P0208-extension-receipt.json"); s = self.selection
        repaired = r["schema"] == "rh-port-repaired-continuation-receipt-v1"
        require(r["schema"] in ("rh-port-continuation-receipt-v1", "rh-port-repaired-continuation-receipt-v1")
                and r["accepted_commit"] == EXT_BASE and r["fresh_project_objects"] is False, "Wrong continuation schema/base/freshness")
        require(r["new_project_module_count"] == 50 and r["retained_project_module_count"] == (160 if repaired else 112)
                and r["combined_target_count"] == 63 and r["packet_complete"] is False, "Wrong continuation scope")
        for key in ("unexpected_rebuilt_existing", "changed_reused_outputs", "changed_sources", "changed_configuration", "changed_patches"):
            require(not r[key], f"Continuation changed retained inputs: {key}")
        check(self.drivers / "packet_extension.py", r["extension_driver_sha256"])
        require(Path(r["phase1"]["receipt_path"]).resolve() == self.ev / "P0208-receipt.json",
                "Continuation references a different phase1 checkpoint")
        for name, key in (("phase1-receipt.json", "receipt_sha256"),):
            check(ev / name, r["phase1"][key]); check(self.ev / "P0208-receipt.json", r["phase1"][key])
        require(load(ev / "phase1-receipt.json") == p1 and load(ev / "phase1-selection.json") == s, "Phase1 copies changed")
        require(r["phase1"]["target_ids"] == list(selected) and r["phase1"]["batch_log_sha256"] == p1["batch_build"]["log_sha256"] and r["phase1"]["audit_log_sha256"] == p1["aggregate_islattice_audit"]["log_sha256"], "Phase1 provenance links mismatch")
        inputs = self.root / "port-extension-inputs"; check(ev / "inputs-manifest.json", r["input_manifest_sha256"]); check(inputs / "manifest.json", r["input_manifest_sha256"])
        manifest = load(ev / "inputs-manifest.json"); closure = self.closure(NEW); new_ids = closure - self.old_ids
        require(manifest["upstream_commit"] == EXT_BASE and set(manifest["target_ids"]) == NEW and manifest["all_source_hashes_match"] is True, "Wrong extension input manifest")
        require(manifest["ledger_sha256"] == r["ledger_sha256"] == sha(self.blob(EXT_BASE, "tracker/modules.jsonl.gz")), "Extension ledger mismatch")
        require(self.blob(BASE, "tracker/modules.jsonl.gz") == self.blob(EXT_BASE, "tracker/modules.jsonl.gz"), "Ledger changed between phases")
        entries = index(manifest["files"])
        require(set(entries) == closure and len(closure) == manifest["source_count"] == 52 and len(new_ids) == 50, "Extension closure mismatch")
        for mid, entry in entries.items():
            relative = "src/" + self.rel(mid) + ".lean"
            require(entry["module"] == self.ledger[mid]["module"] and entry["relative_path"] == relative and entry["source_sha256"] == self.ledger[mid]["source_sha256"] and entry["role"] == ("target" if mid in NEW else "import"), f"Input manifest entry mismatch: {mid}")
            check(inputs / relative, entry["source_sha256"])
        records = index(r["accepted_imports"])
        require(set(records) == closure - NEW, "Extension accepted-import scope mismatch")
        self.imports(r["accepted_imports"], EXT_BASE, inputs)
        compat = load(ev / "compatibility.json"); check(ev / "compatibility.json", r["compatibility_sha256"])
        require(compat == r["compatibility"] and compat["all_compatible"] is True and compat["baseline_commit"] == BASE and compat["accepted_commit"] == EXT_BASE, "Compatibility evidence mismatch")
        names = set(s["helper_sha256"]) | {"port/target.json", "port/pruned.txt", "tracker/modules.jsonl.gz"}
        require(set(compat["files"]) == names, "Compatibility file scope mismatch")
        for name in names:
            data = self.blob(BASE, name)
            require(data == self.blob(EXT_BASE, name) and compat["files"][name] == {"identical": True, "sha256": sha(data)}, f"Baseline compatibility failure: {name}")
        expected_imports = [{"id": x["id"], "identical": True, "record_sha256": x["record_sha256"], "patch_sha256": x["patch_sha256"]} for x in s["accepted_import_records"]]
        require(compat["existing_import_records"] == expected_imports, "Existing import compatibility list mismatch")
        for entry in expected_imports:
            for suffix, key in (("json", "record_sha256"), ("patch", "patch_sha256")):
                relative = f"port/patches/{entry['id']}.{suffix}"
                data = self.blob(EXT_BASE, relative) if relative in self.paths(EXT_BASE) else None
                require((sha(data) if data is not None else None) == entry[key], f"Accepted existing import changed: {relative}")
        require((compat["existing_import_count"], compat["extension_import_count"], compat["new_source_count"]) == (64, 37, 50), "Compatibility counts mismatch")
        require(set(compat["reused_extension_import_ids"]) == closure & self.old_ids and set(compat["new_import_patch_ids"]) == {mid for mid in new_ids - NEW if self.patch_hashes[mid]}, "Compatibility import/patch scopes mismatch")
        original = self.failed_extension(r, ev, new_ids) if repaired else None
        text = self.phase(r, ev, sorted(NEW), sorted(set(selected) | NEW), inputs,
                          "shared_repaired_continuation_batch" if repaired else "shared_continuation_batch", original)
        if repaired:
            require(index(r["applied_patches"]) == {mid: {"id": mid, "patch_sha256": self.patch_hashes[mid]}
                    for mid in new_ids - NEW if self.patch_hashes[mid]}, "Accepted-import patch manifest changed during repair")
            self.repaired_extension_outputs(r, ev, original, text, new_ids)
        else:
            require(not any(self.built_lines(text, mid) for mid in self.old_ids), "Retained module was rebuilt during continuation")
            built_imports = index(r["built_imports"])
            require(set(built_imports) == new_ids - NEW, "New built-import scope mismatch")
            for mid, row in built_imports.items():
                require(row["module"] == self.ledger[mid]["module"] and row["built_lines"] and row["built_lines"] == self.built_lines(text, mid) and row["built_source_sha256"] == self.sources[mid], f"New import evidence mismatch: {mid}")
                check(self.object(mid), row["olean_sha256"])
            require(index(r["applied_patches"]) == {mid: {"id": mid, "patch_sha256": self.patch_hashes[mid]} for mid in new_ids if self.patch_hashes[mid]}, "Applied-patch manifest mismatch")
            self.reuse_chain(ev, r, self.old_ids)
        require({x["id"] for x in r["remaining"]} == {"M133708"} and set(r["remaining"][0]["missing_imports"]) == {"M139479", "M163509"}, "Remaining scope changed")
        require(r["remaining"][0]["module"] == self.ledger["M133708"]["module"], "Remaining module identity mismatch")
        print("PASS repaired continuation: 13 original target and 35 import producers, 2 fresh retry targets, 160 unchanged reuse snapshots, exact 63-module audit"
              if repaired else "PASS continuation: 15 target and 35 import outputs, 112 unchanged reuse snapshots, exact 63-module audit")

    def dependencies(self):
        manifest = load(self.tree / "lake-manifest.json")
        packages = manifest["packages"]
        require(len({x["name"] for x in packages}) == len(packages), "Duplicate dependency packages")
        for package in packages:
            folder = self.tree / ".lake/packages" / package["name"]
            require(git(folder, "rev-parse", "HEAD").decode().strip() == package["rev"], f"Dependency revision mismatch: {package['name']}")
            require(not git(folder, "status", "--porcelain", "--untracked-files=no").strip(), f"Modified dependency tracked files: {package['name']}")
            if package["name"] == "mathlib": require(package["rev"] == MATHLIB, "Mathlib manifest pin mismatch")
        require(any(x["name"] == "mathlib" for x in packages) and (self.tree / "lean-toolchain").read_text().strip() == LEAN, "Missing Mathlib or wrong Lean toolchain")
        print(f"PASS dependencies: {len(packages)} manifest-pinned pristine checkouts")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--drivers", type=Path, help="Directory containing the unchanged driver snapshots")
    parser.add_argument("--phase1-evidence", type=Path, help="Phase1 evidence directory; defaults to port-P0208-verified/evidence")
    parser.add_argument("--extension-evidence", type=Path, help="Continuation evidence directory; defaults to evidence-extension")
    parser.add_argument("--patches", type=Path, help="Final target-patch directory; imports are checked against accepted Git blobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--phase1-only", action="store_true")
    mode.add_argument("--combine", action="store_true", help="Verify both phases (default)")
    args = parser.parse_args(); gate = Gate(args)
    load(gate.ev / "P0208-receipt.json")
    if not args.phase1_only: load(gate.ext / "P0208-extension-receipt.json")
    phase1, selected = gate.phase1()
    if not args.phase1_only: gate.extension(phase1, selected)
    gate.dependencies()
    print(f"EVIDENCE_GATE_PASSED {48 if args.phase1_only else 63} targets")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"EVIDENCE_GATE_FAILED: {error}", file=sys.stderr)
        sys.exit(1)
