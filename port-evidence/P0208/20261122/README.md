# P0208 port build evidence

This evidence covers **48 of 64 live packet modules** on Lean 4.36.0 and Mathlib `fc4f59e577feb1a4956671bc04ac18aaaf8cd842` (v4.36.0). All 48 submitted builds and their required audits passed. 45 target sources are unchanged from their pinned bytes. No replay pins or replay credit are affected.

Claim: [Port P0208, issue 28141](https://github.com/rohanarun/alpoge-rh-replay/issues/28141).

## Build phases

- **phase-1**: 48 targets; [receipt](phase-1/P0208-receipt.json), [full build transcript](phase-1/batch-build.log), [audit transcript](phase-1/islattice-audit.log).

Phase 1 began with no project objects. Only the exact target dependency packages and their Mathlib cache were shared. The original fresh batch found mechanical proof errors. Its complete failed transcript and receipt are retained within phase 1. The corrected targets were rebuilt in the same tree; successful outputs were retained with before/after digests. A successful invocation requesting all 48 targets and the 48-target audit then established the repaired result. A later continuation, when present, retains the verified first-phase project objects and records their before/after hashes. It does not claim a second fresh build of the whole packet. Earlier receipts and transcripts remain unchanged.

Sources were obtained through the repository's pinned source-mirror helper, with every file's SHA-256 checked against `tracker/modules.jsonl.gz`. Accepted import patches were applied without offsets or fuzz. The receipts identify their accepted upstream commits and all source, patch, object and log digests. No full archive extraction is claimed.

The full shared transcripts contain every compiler diagnostic. Each module log below is explicitly labelled as an excerpt from shared invocations, not a separate invocation. Compiler warnings are retained. Successful records require an actual target `Built` line in the producing attempt, a verified output object, a successful complete final validation batch, no sorry diagnostic in accepted output evidence, preserved inputs, and a zero-hit `IsLattice` audit.

## Review and reproduction information

[packet-index.json](packet-index.json) maps every target to its receipt, module excerpt, full transcript, audit and digests. [SHA256SUMS](SHA256SUMS) covers every evidence file except itself. The receipts preserve exact commands, times, pins, helper identities and resource measurements. A reported largest-child RSS is not a sum of simultaneous process memory.

The `provenance/` directory contains the executed driver sources, the independent evidence verifier, frozen Lean/Lake configuration and dependency manifest, target pins, and the verifier's successful transcript. Driver defaults refer to the original workspace; pass the recorded workspace arguments or recreate that directory structure when checking the retained build tree. The phase selections and input manifests identify exactly which pinned sources and accepted patches were used.

The elaborated audit covers the submitted packet targets. The repository-wide `Main` build, full axiom audit, statement comparison and independent checker run remain separate maintainer work.

## Submitted module transcripts

| ID | Module | Transcript excerpt |
|---|---|---|
| M130308 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ExistsPointLocalizationPrimeStatement` | [phase-1 log](phase-1/logs/M130308.log) |
| M130315 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ExistsProperIntegralModel` | [phase-1 log](phase-1/logs/M130315.log) |
| M130338 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ExistsStalkFractionOfGenericStatement` | [phase-1 log](phase-1/logs/M130338.log) |
| M130356 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ExistsUniqueCentreLocalHom` | [phase-1 log](phase-1/logs/M130356.log) |
| M130366 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ExistsUniqueTangentDualNumber` | [phase-1 log](phase-1/logs/M130366.log) |
| M130382 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ExtendToFractionFieldStatement` | [phase-1 log](phase-1/logs/M130382.log) |
| M130443 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.FinitePointExtendsSection` | [phase-1 log](phase-1/logs/M130443.log) |
| M130458 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.FinrankAdjoinVarsField` | [phase-1 log](phase-1/logs/M130458.log) |
| M130720 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.GenericVanishingStalkVanishingStatement` | [phase-1 log](phase-1/logs/M130720.log) |
| M130746 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.GermGenPtInjectiveRangeRegAtStatement` | [phase-1 log](phase-1/logs/M130746.log) |
| M131100 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ImplicitFunctionHolomorphicStatement` | [phase-1 log](phase-1/logs/M131100.log) |
| M131172 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.IsClopenSpecializationsGenPtStatement` | [phase-1 log](phase-1/logs/M131172.log) |
| M131186 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.IsCptGenResidueFieldGenericStatement` | [phase-1 log](phase-1/logs/M131186.log) |
| M131193 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.IsFinOpenSubBaseChange` | [phase-1 log](phase-1/logs/M131193.log) |
| M131210 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.IsSncPatchRat` | [phase-1 log](phase-1/logs/M131210.log) |
| M131213 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.IsUnitIffUnitAt` | [phase-1 log](phase-1/logs/M131213.log) |
| M131214 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.IsUnitNatCastStalk` | [phase-1 log](phase-1/logs/M131214.log) |
| M131255 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.KaehlerRankLocalizationBound` | [phase-1 log](phase-1/logs/M131255.log) |
| M131257 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.KaehlerRankTranscendenceDegree` | [phase-1 log](phase-1/logs/M131257.log) |
| M131394 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.LieModelExistsUniqueAct` | [phase-1 log](phase-1/logs/M131394.log) |
| M131396 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.LieModelExistsUniqueEquivStatement` | [phase-1 log](phase-1/logs/M131396.log) |
| M131415 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.LieModelFunctorialMapStatement` | [phase-1 log](phase-1/logs/M131415.log) |
| M131739 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.PolynomialFieldExtensionDegreeStatement` | [phase-1 log](phase-1/logs/M131739.log) |
| M131755 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.PrimeChartExistsUniquePoint` | [phase-1 log](phase-1/logs/M131755.log) |
| M131849 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.RegAtIffChartLocalization` | [phase-1 log](phase-1/logs/M131849.log) |
| M131856 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.RegAtNoetherianModelNoetherian` | [phase-1 log](phase-1/logs/M131856.log) |
| M131860 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.RegularAtTranslate` | [phase-1 log](phase-1/logs/M131860.log) |
| M131897 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.RiemannFormHermitianLatticeStatement` | [phase-1 log](phase-1/logs/M131897.log) |
| M132049 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.SiegelLimitImagBoundStatement` | [phase-1 log](phase-1/logs/M132049.log) |
| M132121 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.StalkClosedPointToToStalk` | [phase-1 log](phase-1/logs/M132121.log) |
| M132228 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.TorsionLiftIsClosedImmersionStatement` | [phase-1 log](phase-1/logs/M132228.log) |
| M132281 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.TorusPointOverFractionFieldStatement` | [phase-1 log](phase-1/logs/M132281.log) |
| M132301 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.TraceIntegralSubringExtensionStatement` | [phase-1 log](phase-1/logs/M132301.log) |
| M132392 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ValAtCompLocalHom` | [phase-1 log](phase-1/logs/M132392.log) |
| M132411 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ValuativeLiftExistsUnique` | [phase-1 log](phase-1/logs/M132411.log) |
| M132453 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.ZarhinBundleKernelFirstCoordEquations` | [phase-1 log](phase-1/logs/M132453.log) |
| M132510 | `Development.AutomorphicSpectrum.Proofs.GlobalProofs.BlockBasisChangeEntriesStatement` | [phase-1 log](phase-1/logs/M132510.log) |
| M132602 | `Development.AutomorphicSpectrum.Proofs.GlobalProofs.DigammaLogBoundRightHalfPlaneStatement` | [phase-1 log](phase-1/logs/M132602.log) |
| M132683 | `Development.AutomorphicSpectrum.Proofs.GlobalProofs.GeneralLinearIntegralCompactOpenStatement` | [phase-1 log](phase-1/logs/M132683.log) |
| M132758 | `Development.AutomorphicSpectrum.Proofs.GlobalProofs.IwasawaDecompositionUpperTriangularStatement` | [phase-1 log](phase-1/logs/M132758.log) |
| M132797 | `Development.AutomorphicSpectrum.Proofs.GlobalProofs.LocalFieldUniformizerNormPropertiesStatement` | [phase-1 log](phase-1/logs/M132797.log) |
| M132864 | `Development.AutomorphicSpectrum.Proofs.GlobalProofs.PrimePowerNotDvdDifferent` | [phase-1 log](phase-1/logs/M132864.log) |
| M132983 | `Development.AutomorphicSpectrum.Proofs.MembershipProofs.AdelicCharacterRationalDualImports` | [phase-1 log](phase-1/logs/M132983.log) |
| M132996 | `Development.AutomorphicSpectrum.Proofs.MembershipProofs.ExistsInvariantForm` | [phase-1 log](phase-1/logs/M132996.log) |
| M133005 | `Development.AutomorphicSpectrum.Proofs.MembershipProofs.IsArtinianRingKummerAdjoinRootQuotientRoot` | [phase-1 log](phase-1/logs/M133005.log) |
| M133020 | `Development.AutomorphicSpectrum.Proofs.MembershipProofs.SplitQuadraticAlgebraOrder` | [phase-1 log](phase-1/logs/M133020.log) |
| M133031 | `Development.AutomorphicSpectrum.Proofs.MembershipProofs.ThetaSumStarAndSchAStar` | [phase-1 log](phase-1/logs/M133031.log) |
| M133247 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.ContDiffAtFrameEntry` | [phase-1 log](phase-1/logs/M133247.log) |

## Remaining packet members at the final phase baseline

| ID | Module | Missing accepted imports |
|---|---|---|
| M131597 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.MulMemSqOfSmulEqZero` | M119671, M120428 |
| M131764 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.PrimitiveDerivationPairing` | M005021 |
| M131872 | `Development.AutomorphicSpectrum.Proofs.GeometryProofs.RegularLatticeLocalFree` | M140454 |
| M133286 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.DenseCoordinateTorusStatement` | M133285 |
| M133296 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.DoublingLimitLocalLogarithmStatement` | M133295 |
| M133354 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.ExistsConstAddEquivOfRangeEq` | M134362 |
| M133390 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.ExistsHolomorphicNormalFrameStatement` | M133389 |
| M133708 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.GapEqWeightSubNext` | M139479, M163509 |
| M133750 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.HarmonicAnnulusMaximumPrincipleStatement` | M133749 |
| M133896 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.IsOpenFlagDomainNondegenerateMinorsStatement` | M133895 |
| M133979 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.LocalLogarithmHomomorphismStatement` | M133978 |
| M134003 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.LogDetComparableGramBoundStatement` | M134002 |
| M134040 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.LogSineMobiusTransform` | M139471 |
| M134090 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.MinorRatioRegularOfInverseStatement` | M134089 |
| M134230 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.PolydiscPoissonSmoothStatement` | M134229 |
| M134329 | `Development.AutomorphicSpectrum.Proofs.MetricProofs.RiemannFormPeriodDeterminantBoundStatement` | M134328 |

Pruned packet IDs: none. This is a partial packet result; its remaining state is recorded at each phase's frozen upstream baseline.
