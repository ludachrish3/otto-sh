# Host preferences — general product-repo defaulting (end state)

Recorded from the capability-resolution brainstorm (2026-06-20). Pick this up
*after* the menu+select spec lands.

## The direction

A product repo should be able to default **most** host options from its own
`.otto/settings.toml`, uniformly, under a single `[host_preferences]` umbrella —
applying to hosts *generally*, not just protocols. Two halves converge:

1. **Selections** — which option a product prefers from a lab-declared menu.
   Delivered first for `term`/`transfer` by the capability-resolution spec
   (`docs/superpowers/specs/2026-06-20-host-capability-resolution-design.md`):
   the lab declares `valid_terms`/`valid_transfers`; a product's
   `[host_preferences]` declares an ordered preference intersected with each
   host's menu; per-instance selection rides the `get_host(..., term=/transfer=)`
   override-copy seam. The `CapabilityResolver` is field-agnostic, so new
   menu-style fields opt in without new machinery.

2. **Option values** — defaulting `ssh_options`, ports, timeouts, etc. Already
   exists as `[host_defaults]` (see `todo/host-default-options.md`): repo-level
   per-key merge at the `create_host_from_dict` chokepoint, last-repo-in-
   `OTTO_SUT_DIRS` wins.

## End state

Converge (1) and (2) into one coherent `[host_preferences]` story, so a product
repo declares — in one place — both the menu selections it prefers and the
option values it defaults, for the hosts it touches.

Open questions to resolve when picked up:

- Do `[host_preferences]` (selections) and `[host_defaults]` (values) merge into
  one block, or stay sibling blocks under a shared precedence model? (Today they
  are separate; the capability spec deliberately keeps them beside each other.)
- Which additional fields become menu-style selectable beyond term/transfer?
  Candidate test: a host capability with multiple valid options where a
  product/test legitimately chooses. **Not** firmware/hardware-static facts
  (`filesystem`/`command_frame`/`loader`) — those stay single-valued lab
  attributes (software → product; hardware+firmware → lab).
- Confirm multi-repo precedence ("last repo in `OTTO_SUT_DIRS` wins") holds
  uniformly across selections and values.

## Follow-ups surfaced during Phase 2 (term-family + host_preferences)

- **Restore the term family check when the override-copy seam lands.** Phase 2's
  spec validator now rejects a term applied to a family it doesn't serve (e.g.
  `ssh` on an embedded host), but the in-place `UnixHost.set_term_type` checks
  only registry membership — not `_TERM_FAMILIES` — whereas `set_transfer_type`
  already enforces `"unix" in cls.host_families`. §9 of the capability spec plans
  to *remove* both setters in favor of the override-copy seam, which resolves the
  asymmetry automatically; but the seam's `__post_init__` rebuild MUST run the
  same family validation the spec validator does, so a runtime protocol switch
  can't escape the family constraint that lab-data load enforces. If the setters
  are kept for any reason, add the `_TERM_FAMILIES` check to `set_term_type` to
  mirror `set_transfer_type`.
- **Embedded preference-path test gap** (low priority): the embedded
  `EmbeddedHostSpec.to_host(preferences=)` + factory-flatten-for-embedded path is
  byte-identical to the unix path but untested at any level (unix is fully
  tested). A single embedded factory case (`os_type="embedded"`,
  `command_frame="zephyr"`, `preferences={".*": {"transfer": ["tftp"]}}`) closes it.

## Related

- `docs/superpowers/specs/2026-06-20-host-capability-resolution-design.md` (§8, §12)
- `docs/superpowers/specs/2026-06-20-host-product-providers-design.md` — the
  code-vs-data split; products are the *software* under test, registered in code.
- `todo/host-default-options.md` — the option-value half (`[host_defaults]`).
- `todo/host-declared-transfer-term-lists.md` — the original seed of the
  selection half (superseded by the capability-resolution spec).
- `todo/registry_builtin_registration_symmetry.md` and the N→1 registry
  consolidation track — adjacent: consolidating the lab-driven strategy
  registries.
