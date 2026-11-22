# P0179: four ready modules on Lean 4.36.0

This evidence supports a **partial port of P0179** in
[`rohanarun/alpoge-rh-replay`](https://github.com/rohanarun/alpoge-rh-replay).
The reservation is [Port P0179, issue #32377](https://github.com/rohanarun/alpoge-rh-replay/issues/32377).
Port results are separate from the archive replay and earn no replay credit.

## Scope and result

Three submitted source files are byte-identical to their ledger-pinned originals.
They receive only JSON records with `"notes": "unchanged"`, without patch files.
M027599 has one proof-only compatibility fix: `List.TFAE.out` now uses one-based
indices, so `.out 4 0` becomes `.out 5 1`. This selects the same implication from
a principal maximal ideal to `IsDiscreteValuationRing`. No definition, theorem
statement, hypothesis, instance, or proof-trust mechanism was changed.

| Module ID | Module suffix under `Development.AbelianVarietyTheory.Proofs.DivisorProofs` | Full compiler transcript |
| --- | --- | --- |
| M022910 | `EtaleMaximalQuotientAlgebraicStatement` | [M022910.log](logs/M022910.log) |
| M023055 | `EvalQuotientRingVectorAtPointsInjective` | [M023055.log](logs/M023055.log) |
| M026102 | `ExistsHeightOneValuationsControlSubgroup` | [M026102.log](logs/M026102.log) |
| M027599 | `ExistsMulEqMulSqOfFaithfullyFlatHeightOne` | [M027599.log](logs/M027599.log) |

[P0179-receipt.json](P0179-receipt.json) records each module's original source,
compiled source, patch (null when absent), output object, and transcript SHA-256,
along with the command, timestamps, exit status, and actual producing `Built`
line. The [initial build summary](history/initial-check/P0179-build.log) records
the four original-source invocations. M027599's
[initial diagnostic](history/initial-check/logs/M027599.log) reports that TFAE
indices start at one; the [repair summary](history/additional-check-1/P0179-build.log)
records its subsequent build. The individual logs retain all compiler output,
including warnings. The final M027599 log above is the successful repaired build.

There were 57 previously accepted records, four ready unported members, one
blocked member, and two pruned members at selection time. Acceptance of these
four records would bring coverage to **61 of 62 live packet members**. The packet
remains incomplete:

- **M022730**, `EtaleAwayQuotientEqLocalization`, needs accepted import records
  **M042135** (`LocalizationInverseModF`) and **M042148**
  (`LocalizationResidueApproximation`), both in P0110.
- Pruned **M022885** and **M022990** are excluded.
- All 57 existing packet records are preserved.

## Exact inputs and fresh project

- Lean: **`leanprover/lean4:v4.36.0`**.
- Mathlib v4.36.0: **`fc4f59e577feb1a4956671bc04ac18aaaf8cd842`**.
- Frozen accepted-import and requirement baseline:
  **`352bdee9ee3cab6cbdcd4a0653225e303c796d72`**.
- Refreshed PR base: **`a61531665610ee8cf73359a32011effc28cf33d2`**.
  The intervening updates affect contributor/activity files; the port
  requirements, target, ledger, audit scripts, helpers, and selected accepted
  import records/patches are unchanged.
- Pinned source mirror:
  [`leanprover-community/riemann-hypothesis` at `8ec38f92ac54b5c91bc07f4f6f88ea61691b7975`](https://github.com/leanprover-community/riemann-hypothesis/tree/8ec38f92ac54b5c91bc07f4f6f88ea61691b7975/src).

An empty project was created for this run and populated with the four targets
and their complete eight-module project import closure. **All 12 original source
files were SHA-256 checked against `tracker/modules.jsonl.gz` before applying
the four already-accepted import patches.** This used the pinned source mirror,
not a full archive extraction. The repository's CI preparation code supports
this same per-file hash verification method.

Only the exact target dependency packages and toolchain cache were shared.
No project `.olean` files were copied into the new project. All nine dependency
Git checkouts match their pinned manifest revisions and have no tracked changes.
The exported configuration preserves the archive's Lean options. The actual
compilation used `LEAN_NUM_THREADS=1` and a separate
`lake --no-cache build +<Module>:olean` invocation for each target, followed by
one additional invocation for the repaired M027599. The other three successful
target outputs were retained.

The original driver's `check` mode writes `fresh_project_objects: false` as a
mode flag; it did not inspect whether project objects existed at setup. The
original receipt and logs are retained byte-for-byte under
[history/initial-check](history/initial-check). The final receipt records the
session's empty-project setup observation separately and corroborates it with
actual producing `Built` lines for **all 12 project modules**. No saved pre-build
inventory or second execution of `tracker/port_build.py reproduce` is claimed.

## Semantic audits and integrity checks

The repository's unchanged elaborated-environment audits run over all 12 project
modules, including the accepted imports:

- [IsLattice audit transcript](islattice-audit.log).
- [Reused-name audit transcript](reused-audit.log).

Their sources, exact module scope, exit codes, zero-hit summaries, and hashes are
recorded in the final receipt. The checks cover the repository's known reused
constant names; they are not a whole-project or RH verdict.

The [evidence verifier](provenance/finalize_evidence.py) checks source and patch
roundtrips, accepted import records, clean dependency revisions, complete
successful producing transcripts, the absence of `sorry` diagnostics, and both
audits before exporting the receipt. It does not run or invent builds. The
[initial driver](provenance/initial_driver.py), frozen helpers, manifest, project
configuration, and accepted import records/patches are included for inspection.

To verify the downloaded evidence files:

```sh
sha256sum -c SHA256SUMS
```

The object hashes identify this run. They are not promised to match across
different absolute build paths or environments.

## Reproduce the compilation

Use a repository checkout containing the four submitted records and a new build
directory, with Python 3, Git, GNU patch, elan, and network access available:

```sh
bash reproduce.sh /absolute/replay-checkout /absolute/new-build-directory
```

The script verifies and prepares the pinned project sources with the repository's
CI helper, installs the exact target, fetches matching dependency artifacts,
builds the four targets, and runs both audits over the 12-module closure. Its
default worker budget is two; set `LEAN_NUM_THREADS=1` to match this run. This is
a reproduction convenience, not a substitute for the repository's record and
policy validators.

The replay toolchain, replay Mathlib pin, replay verifier, and replay completion
accounting are untouched by the submitted records.
