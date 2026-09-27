# Registry hygiene — monitor parsers' built-ins

## Done ✅

- ✅ Command frames, embedded filesystems, binary loaders and every other
  class registry: since #455 each built-in is registered by reference
  (`otto.registry.Ref`) in its registry's own module, and the checks the
  public `register_*` functions ran live in each registry's *validate* hook,
  so built-ins and third-party entries meet the same checks. This replaces
  the earlier "built-ins call the public `register_*` wrapper" direction this
  file used to argue for. See `docs/architecture/subsystems/registries.md`,
  "Built-ins register by reference". (Sibling todos that point here for the
  fix direction should read that page instead.)

## Remaining (deferred)

Monitor shell parsers still load their built-ins from a literal,
`DEFAULT_PARSERS = { ... }` in `src/otto/monitor/parsers.py`, while
third-party parsers go through `register_host_parsers()`. That function is
host-scoped and instance-valued (there is no `register_X(type_name, cls)`
path), so converting it needs a NEW public function, and it is deferred.

Close this file when the monitor revamp's project-level `register_parsers()`
(`docs/superpowers/plans/2026-07-02-monitor-phase1-backend-contract.md`)
lands: check whether the built-in `DEFAULT_PARSERS` travel that same path,
then delete this file.
