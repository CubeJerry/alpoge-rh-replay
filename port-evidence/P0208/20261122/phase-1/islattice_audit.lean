/-
Audit ported modules for silent uses of `Submodule.IsLattice`.

Mathlib #44365 renamed the pinned `Submodule.IsLattice` (finitely generated and spanning) to
`Submodule.IsFullLattice` and reused the name `Submodule.IsLattice` for a weaker notion without
`span_eq_top`. A pinned module can therefore compile unchanged on the port target while one of its
statements or definitions silently means something weaker. Text search cannot tell these uses
apart from the archive's own declarations named `IsLattice`, so this audit asks the elaborated
environment instead: it reports every declaration of the given modules whose type, or whose body
if it is a definition, refers to the constant `Submodule.IsLattice`. Proofs of theorems are not
inspected, since they may legitimately use the new notion (for example `IsLattice.of_fg`).

Run it from the ported tree after building the modules:

  lake env lean --run <path>/port/islattice_audit.lean Module.One Module.Two ...

It prints one line per hit and exits with status 1 if there are any.
-/
import Lean

open Lean

def auditedConstant : Name := `Submodule.IsLattice

unsafe def main (args : List String) : IO UInt32 := do
  if args.isEmpty then
    IO.eprintln "usage: lake env lean --run port/islattice_audit.lean Module..."
    return 2
  initSearchPath (← findSysroot)
  let imports := args.toArray.map fun m => ({ module := m.toName } : Import)
  let env ← importModules imports {} (trustLevel := 1024)
  let mut hits := 0
  for m in args do
    let some idx := env.getModuleIdx? m.toName
      | throw <| IO.userError s!"module {m} is not in the environment"
    for n in env.header.moduleData[idx.toNat]!.constNames do
      let some ci := env.find? n | continue
      let inType := ci.type.getUsedConstants.contains auditedConstant
      let inBody := match ci with
        | .defnInfo d => d.value.getUsedConstants.contains auditedConstant
        | _ => false
      if inType || inBody then
        let place := if inType then "statement" else "definition body"
        IO.println s!"{m}: {n} uses Submodule.IsLattice in its {place}"
        hits := hits + 1
  IO.println s!"islattice-audit: {args.length} module(s), {hits} hit(s)"
  return if hits == 0 then 0 else 1
