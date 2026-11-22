#!/usr/bin/env python3
"""Build and verify Lean port PRs against the pinned archive sources and port target.

For every port record changed in a PR (or explicitly requested via --patch-ids):
1. Resolves all transitive project imports from tracker/modules.jsonl.gz.
2. Fetches each required source file (from --src or the pinned archive source mirror)
   and verifies its SHA-256 against tracker/modules.jsonl.gz.
3. Applies every required port/patches/<ID>.patch cleanly with git apply.
4. Writes lean-toolchain and lakefile.toml matching port/target.json and the archive's
   original Lake options (including GroundTheoryFields's synthInstance.maxHeartbeats).
5. Optionally runs `lake update` and `lake exe cache get` (--setup-lake).
6. Compiles every target module with `lake build +<Module>:olean` and rejects any
   build error or `declaration uses 'sorry'` diagnostic.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
PATCHES = ROOT / "port" / "patches"
TARGET_JSON = ROOT / "port" / "target.json"
MODULES_LEDGER = ROOT / "tracker" / "modules.jsonl.gz"

# Pinned commit of leanprover-community/riemann-hypothesis (every fetched file is
# still cryptographically verified against source_sha256 in tracker/modules.jsonl.gz).
DEFAULT_SOURCE_BASE_URL = (
    "https://raw.githubusercontent.com/"
    "leanprover-community/riemann-hypothesis/"
    "8ec38f92ac54b5c91bc07f4f6f88ea61691b7975/src"
)

GROUND_THEORY_FIELDS_SUBDIRS = frozenset({"LocalFields", "GlobalFields"})
SORRY_DIAG_RE = re.compile(r"declaration uses 'sorry(?:Ax)?'")


def load_ledger(ledger_path: Path = MODULES_LEDGER) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    with gzip.open(ledger_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            by_id[row["id"]] = row
    return by_id


def load_target(target_path: Path = TARGET_JSON) -> dict:
    return json.loads(target_path.read_text(encoding="utf-8"))


def load_record(module_id: str, patches_dir: Path = PATCHES) -> dict | None:
    path = patches_dir / f"{module_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def module_relpath(module_name: str) -> str:
    return module_name.replace(".", "/") + ".lean"


def get_changed_module_ids(base_ref: str = "origin/main", repo_root: Path = ROOT) -> list[str]:
    """Return sorted module IDs whose .json or .patch files changed vs base_ref."""
    diff_range = f"{base_ref}...HEAD"
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            diff_range,
            "--",
            "port/patches/M*.json",
            "port/patches/M*.patch",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Fallback to two-dot diff if merge-base is unavailable (e.g. shallow clone)
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACMR",
                base_ref,
                "--",
                "port/patches/M*.json",
                "port/patches/M*.patch",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        p = Path(line.strip())
        if p.suffix in {".json", ".patch"} and re.fullmatch(r"M\d{6}", p.stem):
            ids.add(p.stem)
    return sorted(ids)


def expand_targets(
    raw_ids: list[str],
    by_id: dict[str, dict],
    patches_dir: Path = PATCHES,
) -> list[str]:
    """Expand module IDs (M000001) and packet IDs (P0001) into sorted module IDs."""
    by_packet: dict[str, list[str]] = {}
    for mid, row in by_id.items():
        by_packet.setdefault(row["packet"], []).append(mid)

    expanded: set[str] = set()
    for token in raw_ids:
        token = token.strip()
        if not token:
            continue
        if token in by_id:
            expanded.add(token)
        elif token in by_packet:
            ported_in_packet = [
                mid for mid in by_packet[token] if (patches_dir / f"{mid}.json").is_file()
            ]
            if not ported_in_packet:
                raise ValueError(f"Packet {token} has no port records in {patches_dir}")
            expanded.update(ported_in_packet)
        else:
            raise ValueError(f"Unknown module or packet ID: {token}")
    return sorted(expanded)


def transitive_closure(module_ids: list[str], by_id: dict[str, dict]) -> list[str]:
    """Return sorted module IDs for module_ids and all their transitive project imports."""
    seen: set[str] = set()
    stack = list(module_ids)
    while stack:
        mid = stack.pop()
        if mid in seen:
            continue
        if mid not in by_id:
            raise ValueError(f"Unknown module ID in ledger: {mid}")
        seen.add(mid)
        for dep in by_id[mid].get("imports", ()):
            if dep not in seen:
                stack.append(dep)
    return sorted(seen)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_url_bytes(url: str, retries: int = 4, timeout: int = 30) -> bytes:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "alpoge-rh-replay-port-ci"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def materialize_sources(
    workspace: Path,
    closure_ids: list[str],
    by_id: dict[str, dict],
    local_src: Path | None,
    source_base_url: str,
    max_workers: int = 8,
) -> None:
    """Copy or download each source file into workspace/src and verify its SHA-256."""
    src_root = workspace / "src"
    src_root.mkdir(parents=True, exist_ok=True)
    base = source_base_url.rstrip("/")

    def fetch_one(mid: str) -> tuple[str, str]:
        row = by_id[mid]
        rel = module_relpath(row["module"])
        dest = src_root / rel
        expected_sha = row["source_sha256"]

        if local_src is not None:
            origin = local_src / rel
            if not origin.is_file():
                raise FileNotFoundError(f"{mid}: missing source file {origin}")
            data = origin.read_bytes()
        else:
            url = f"{base}/{urllib.parse.quote(rel, safe='/')}"
            data = fetch_url_bytes(url)

        actual_sha = sha256_bytes(data)
        if actual_sha != expected_sha:
            raise ValueError(
                f"{mid} ({row['module']}): source SHA-256 mismatch: "
                f"expected {expected_sha}, got {actual_sha}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return mid, rel

    print(
        f"[FETCH] Materializing {len(closure_ids)} SHA-256-verified source file(s)...",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_one, mid): mid for mid in closure_ids}
        for future in as_completed(futures):
            future.result()
    print(f"[FETCH] Verified {len(closure_ids)}/{len(closure_ids)} source file(s).", flush=True)


def apply_patches(
    workspace: Path,
    closure_ids: list[str],
    patches_dir: Path = PATCHES,
) -> list[str]:
    """Apply patches for every patched module in closure_ids. Returns failed IDs."""
    failed: list[str] = []
    for mid in closure_ids:
        record = load_record(mid, patches_dir)
        if record is None:
            print(f"[ERROR] {mid}: missing port record {patches_dir / f'{mid}.json'}", file=sys.stderr)
            failed.append(mid)
            continue
        notes = record.get("notes", "")
        patch_path = patches_dir / f"{mid}.patch"
        if notes == "unchanged":
            print(f"[KEEP]  {mid}: unchanged (verified against archive source)", flush=True)
            continue
        if not patch_path.is_file():
            print(f"[ERROR] {mid}: record is patched but {patch_path.name} is missing", file=sys.stderr)
            failed.append(mid)
            continue

        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_path)],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[ERROR] {mid}: patch failed to apply:\n{result.stderr}", file=sys.stderr)
            failed.append(mid)
        else:
            print(f"[PATCH] {mid}: applied {patch_path.name}", flush=True)
    return failed


def render_lakefile(module_names: list[str], mathlib_rev: str) -> str:
    """Generate a lakefile.toml matching the archive's options for the materialized modules."""
    regular_libs: set[str] = set()
    gt_subdirs: set[str] = set()
    gt_fields_subdirs: set[str] = set()
    main_roots: set[str] = set()

    for name in module_names:
        parts = name.split(".")
        top = parts[0]
        if top in {"Main", "Statements"}:
            main_roots.add(top)
        elif top == "GroundTheory" and len(parts) > 1:
            if parts[1] in GROUND_THEORY_FIELDS_SUBDIRS:
                gt_fields_subdirs.add(parts[1])
            else:
                gt_subdirs.add(parts[1])
        else:
            regular_libs.add(top)

    default_targets: list[str] = sorted(regular_libs)
    if gt_subdirs:
        default_targets.append("GroundTheory")
    if gt_fields_subdirs:
        default_targets.append("GroundTheoryFields")
    if main_roots:
        default_targets.append("Main")

    lines = [
        'name = "tree"',
        'srcDir = "src"',
        f"defaultTargets = {json.dumps(default_targets)}",
        'moreLeanArgs = ["--tstack=400000"]',
        "",
        "[leanOptions]",
        "autoImplicit = false",
        "relaxedAutoImplicit = false",
        "maxSynthPendingDepth = 3",
        "pp.unicode.fun = true",
        "maxHeartbeats = 4000000",
        "",
        "[[require]]",
        'name = "mathlib"',
        'git = "https://github.com/leanprover-community/mathlib4"',
        f'rev = "{mathlib_rev}"',
        "",
    ]

    for lib in sorted(regular_libs):
        lines.extend([
            "[[lean_lib]]",
            f'name = "{lib}"',
            f'globs = ["{lib}.+"]',
            "",
        ])

    if gt_subdirs:
        globs = [f"GroundTheory.{sub}.+" for sub in sorted(gt_subdirs)]
        lines.extend([
            "[[lean_lib]]",
            'name = "GroundTheory"',
            f"globs = {json.dumps(globs)}",
            "",
        ])

    if gt_fields_subdirs:
        globs = [f"GroundTheory.{sub}.+" for sub in sorted(gt_fields_subdirs)]
        lines.extend([
            "[[lean_lib]]",
            'name = "GroundTheoryFields"',
            f"globs = {json.dumps(globs)}",
            "leanOptions = {synthInstance.maxHeartbeats = 400000}",
            "",
        ])

    if main_roots:
        roots = [r for r in ("Statements", "Main") if r in main_roots]
        lines.extend([
            "[[lean_lib]]",
            'name = "Main"',
            f"roots = {json.dumps(roots)}",
            "",
        ])

    return "\n".join(lines)


def write_project_files(workspace: Path, module_names: list[str], target: dict) -> None:
    (workspace / "lean-toolchain").write_text(target["lean"].strip() + "\n", encoding="utf-8")
    lakefile = render_lakefile(module_names, target["mathlib"])
    (workspace / "lakefile.toml").write_text(lakefile, encoding="utf-8")


def run_streaming(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command, streaming combined output live while returning (returncode, output)."""
    chunks: list[str] = []
    with subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            chunks.append(line)
        rc = proc.wait()
    return rc, "".join(chunks)


def setup_lake_env(lake: str, workspace: Path, clean_cache_archive: bool = True) -> None:
    print("\n[LAKE] Running `lake update`...", flush=True)
    rc, _ = run_streaming([lake, "update"], cwd=workspace)
    if rc != 0:
        raise RuntimeError(f"`lake update` failed with exit code {rc}")

    print("\n[LAKE] Running `lake exe cache get`...", flush=True)
    rc, _ = run_streaming([lake, "exe", "cache", "get"], cwd=workspace)
    if rc != 0:
        raise RuntimeError(f"`lake exe cache get` failed with exit code {rc}")

    if clean_cache_archive:
        cache_dir = Path(os.environ.get("MATHLIB_CACHE_DIR", Path.home() / ".cache" / "mathlib"))
        if cache_dir.is_dir():
            print(f"[LAKE] Cleaning compressed cache archive {cache_dir} to save disk space...", flush=True)
            shutil.rmtree(cache_dir, ignore_errors=True)


def build_modules(
    lake: str,
    workspace: Path,
    target_ids: list[str],
    by_id: dict[str, dict],
    patches_dir: Path = PATCHES,
    skip_unchanged: bool = False,
) -> list[str]:
    """Build each target module with lake and reject any build errors or sorry warnings."""
    to_build: list[tuple[str, str]] = []
    for mid in target_ids:
        record = load_record(mid, patches_dir)
        if record is None:
            continue
        if skip_unchanged and record.get("notes") == "unchanged":
            print(f"[SKIP]  {mid}: unchanged (--skip-unchanged)", flush=True)
            continue
        to_build.append((mid, by_id[mid]["module"]))

    if not to_build:
        print("No modules to build.", flush=True)
        return []

    targets = [f"+{name}:olean" for _, name in to_build]
    print(f"\n[BUILD] Building {len(to_build)} module(s) with `{lake} build`...", flush=True)
    print(f"        IDs: {', '.join(mid for mid, _ in to_build)}", flush=True)

    rc, output = run_streaming([lake, "build"] + targets, cwd=workspace)
    has_sorry = bool(SORRY_DIAG_RE.search(output))

    if rc == 0 and not has_sorry:
        for mid, module_name in to_build:
            print(f"[PASS]  {mid} ({module_name})", flush=True)
        return []

    if has_sorry:
        print("\n[ERROR] Build emitted a `declaration uses 'sorry'` diagnostic!", file=sys.stderr)
        # Remove compiled artifacts for target modules so individual re-runs re-emit diagnostics
        lib_lean = workspace / ".lake" / "build" / "lib" / "lean"
        for _, module_name in to_build:
            stem = lib_lean / module_name.replace(".", "/")
            for ext in (".olean", ".olean.private", ".olean.server", ".ilean", ".trace", ".hash"):
                p = stem.with_suffix(ext)
                if p.exists():
                    p.unlink()
    else:
        print(f"\n[WARN]  Batch build failed (exit {rc}). Checking modules individually...", flush=True)

    failed: list[str] = []
    for mid, module_name in to_build:
        r_rc, r_out = run_streaming([lake, "build", f"+{module_name}:olean"], cwd=workspace)
        r_sorry = bool(SORRY_DIAG_RE.search(r_out))
        if r_rc != 0 or r_sorry:
            reason = "uses sorry" if r_sorry else f"exit {r_rc}"
            print(f"[FAIL]  {mid} ({module_name}): {reason}", file=sys.stderr)
            failed.append(mid)
        else:
            print(f"[PASS]  {mid} ({module_name})", flush=True)
    if not failed:
        # Guard against a sorry warning from a transitive dependency or non-reproduced failure
        failed = [mid for mid, _ in to_build]
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--lake",
        default=os.environ.get("PORT_CI_LAKE", "lake"),
        help="Path to lake binary (default: 'lake' in PATH)",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("PORT_CI_WORKSPACE", ""),
        help="Directory for the temporary Lean build workspace (default: auto temp dir)",
    )
    parser.add_argument(
        "--src",
        default=os.environ.get("PORT_CI_SRC", ""),
        help="Optional local unpacked archive src/ directory (skips HTTPS source fetch)",
    )
    parser.add_argument(
        "--source-base-url",
        default=os.environ.get("PORT_CI_SOURCE_BASE_URL", DEFAULT_SOURCE_BASE_URL),
        help="Base URL for raw archive src/ files when --src is omitted",
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref to diff against to detect changed port records (default: origin/main)",
    )
    parser.add_argument(
        "--patch-ids",
        nargs="+",
        metavar="ID",
        help="Explicit module (M000001) or packet (P0001) IDs to build",
    )
    parser.add_argument(
        "--setup-lake",
        action="store_true",
        help="Run `lake update` and `lake exe cache get` before building",
    )
    parser.add_argument(
        "--keep-cache-archive",
        action="store_true",
        help="Do not delete ~/.cache/mathlib after `lake exe cache get`",
    )
    parser.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="Only compile modules with .patch files (skip unchanged records)",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Fetch sources, verify SHA-256, apply patches, and write Lake files without building",
    )
    args = parser.parse_args()

    by_id = load_ledger()
    target = load_target()

    if args.patch_ids:
        module_ids = expand_targets(args.patch_ids, by_id)
        print(f"Building {len(module_ids)} explicitly requested module(s).", flush=True)
    else:
        module_ids = get_changed_module_ids(args.base_ref)
        if not module_ids:
            print("No port records changed vs base ref. Nothing to build.", flush=True)
            return 0
        print(f"Detected {len(module_ids)} changed port module(s): {', '.join(module_ids)}", flush=True)

    closure_ids = transitive_closure(module_ids, by_id)
    if len(closure_ids) > len(module_ids):
        print(
            f"Resolved {len(closure_ids) - len(module_ids)} transitive project import(s) "
            f"({len(closure_ids)} total module(s) to materialize).",
            flush=True,
        )

    local_src = Path(args.src) if args.src else None
    if local_src is not None and not local_src.is_dir():
        print(f"ERROR: --src directory not found: {local_src}", file=sys.stderr)
        return 1

    temp_ctx = None
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        temp_ctx = tempfile.TemporaryDirectory(prefix="port-ci-")
        workspace = Path(temp_ctx.name)

    try:
        materialize_sources(
            workspace=workspace,
            closure_ids=closure_ids,
            by_id=by_id,
            local_src=local_src,
            source_base_url=args.source_base_url,
        )

        patch_failures = apply_patches(workspace, closure_ids)
        if patch_failures:
            print(
                f"\nPORT_BUILD_FAILED {len(patch_failures)} patch failure(s): "
                f"{', '.join(patch_failures)}",
                file=sys.stderr,
            )
            return 1

        module_names = [by_id[mid]["module"] for mid in closure_ids]
        write_project_files(workspace, module_names, target)
        print(
            f"[LAKE] Wrote lean-toolchain ({target['lean']}) and lakefile.toml "
            f"(mathlib {target['mathlib'][:12]}) in {workspace}",
            flush=True,
        )

        if args.prepare_only:
            print("Workspace prepared. Skipping build (--prepare-only).", flush=True)
            return 0

        if args.setup_lake:
            setup_lake_env(args.lake, workspace, clean_cache_archive=not args.keep_cache_archive)

        build_failures = build_modules(
            lake=args.lake,
            workspace=workspace,
            target_ids=module_ids,
            by_id=by_id,
            skip_unchanged=args.skip_unchanged,
        )
        if build_failures:
            print(
                f"\nPORT_BUILD_FAILED {len(build_failures)} module(s): {', '.join(build_failures)}",
                file=sys.stderr,
            )
            return 1

        print(f"\nPORT_BUILD_PASSED all {len(module_ids)} module(s).", flush=True)
        return 0
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


if __name__ == "__main__":
    sys.exit(main())
