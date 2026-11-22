/-
Audit ported modules for uses of Mathlib names whose meaning differs between the pinned Mathlib
(0df444a3) and the port target (v4.36.0), although the name and (mostly) the type are the same.
This generalises `port/islattice_audit.lean` from one constant to a table. Like that script it
asks the elaborated environment, not the text: it reports every declaration of the given modules
whose type, or whose body if it is a definition or an `opaque`, refers to a listed constant.
Proofs of theorems are not inspected, and theorems with auxiliary names (`_proof_1`, ...) are
skipped. A hit is not necessarily a problem: for the "junk value" entries the meaning is the same
whenever the side condition named in the table holds in the statement.

  lake env lean --run reused_audit.lean Module.One Module.Two ...

Exit status: 0 for no hits, 1 if there are hits, 2 on a usage or environment error.
The table was produced by fingerprinting the type and body of all 142,682 non-theorem constants
present under the same name in both environments, diffing the 16,857 that changed, and reviewing
those the archive's statements and definitions use. See PORTING.md, "Reused-name audit".
-/
import Lean

open Lean

def audited : List (Name × String) := [
  (`Submodule.IsLattice, "pinned meaning is Submodule.IsFullLattice (FG and spanning)"),
  (`IsAddTorsionFree, "pinned meaning is HasUniqueDiv (differs outside commutative monoids)"),
  (`IsMulTorsionFree, "pinned meaning is HasUniqueRoots (differs outside commutative monoids)"),
  (`MeasureTheory.eLpNorm, "target value is ∞ when the function is not a.e. strongly measurable"),
  (`MeasureTheory.NoAtoms, "pinned NoAtoms was an alias of NullSingletonClass (singletons are null); the target class forbids atoms in the measure-theoretic sense"),
  (`multiplicity, "pinned value is 1 when the multiplicity is infinite, target value is 0"),
  (`MeasureTheory.Measure.map, "pinned value is 0 when the map is not a.e. measurable, target value is a Dirac measure (for a nonzero measure)"),
  (`MeasureTheory.Measure.prod, "target value is 0 when the second measure is not s-finite, pinned value is the bind formula"),
  (`ModularForm.L, "target redefines the value at s = 0")]

def auxComponent (s : String) : Bool :=
  s.startsWith "_" || s.startsWith "match_" || s.startsWith "proof_"

def isAuxName : Name → Bool
  | .anonymous => false
  | .num _ _ => true
  | .str p s => auxComponent s || isAuxName p

unsafe def audit (mods : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let imports := mods.toArray.map fun m => ({ module := m.toName } : Import)
  let env ← importModules imports {} (trustLevel := 1024)
  let mut hits := 0
  for m in mods do
    let some idx := env.getModuleIdx? m.toName
      | throw <| IO.userError s!"module {m} is not in the environment"
    for n in env.header.moduleData[idx.toNat]!.constNames do
      let some ci := env.find? n | continue
      if (ci matches .thmInfo _) && isAuxName n then continue
      let tyUsed := ci.type.getUsedConstants
      let bodyUsed := match ci with
        | .defnInfo d => d.value.getUsedConstants
        | .opaqueInfo d => d.value.getUsedConstants
        | _ => #[]
      for (c, why) in audited do
        let inType := tyUsed.contains c
        if inType || bodyUsed.contains c then
          let place := if inType then "statement" else "definition body"
          IO.println s!"{m}: {n} uses {c} in its {place} ({why})"
          hits := hits + 1
  IO.println s!"reused-audit: {mods.length} module(s), {hits} hit(s)"
  return if hits == 0 then 0 else 1

unsafe def main (args : List String) : IO UInt32 := do
  if args.isEmpty then
    IO.eprintln "usage: lake env lean --run reused_audit.lean Module..."
    return 2
  try
    audit args.eraseDups
  catch e =>
    IO.eprintln s!"reused-audit: error: {e}"
    return 2
