# #590 follow-up: stability tiers, the deprecation lifecycle, and showing both to users

The tier of every public name lives only in the manifest: `api/public.toml`
from P1, with `stability` set per namespace (spec
`docs/superpowers/specs/2026-10-04-public-api-manifest-design.md` §5). Nothing a
user reads shows it. Everything below is an owner decision from 2026-10-05,
made after three adversarial design reviews (Codex, twice, and the final
reviewer). The owner accepted every finding. Rows marked *proposal* are the
controller's, and the owner may overrule them.

## The goal: what each tier promises

- **stable.** Breaking changes are marked. The area has a documented behaviour
  contract and named conformance tests (spec 1 §5).
- **provisional.** Breaking changes are marked. Mechanical coverage is the
  dump's recorded shape; behaviour is covered by review.
- **experimental** (from 1.0). There is no compatibility promise. A user can
  rely only on accurate labelling, and on deliberate promotion.

Mark rule: **every breaking change to a non-experimental symbol needs a mark.**
Compatible additions need none.

## How a symbol gets its tier: the entry-tier rule

- **The tier follows the PATH,** not the object. The dump records no identity
  across commits, and none is added: an identity key would not survive an
  implementation move. The dump format does not change.
- **The entry tier.** A new public symbol enters at its era's entry tier with
  no manifest edit: **provisional before 1.0, experimental from 1.0.** Before
  1.0, adding provisional symbols costs nothing.
- **Any tier above the entry tier is claimed explicitly, per symbol, in the
  manifest.** A namespace default never gives a newly added symbol a tier above
  entry. Promoting a whole namespace means the manifest diff names each of its
  symbols.
  - Stable may be claimed before 1.0. The claim is still explicit, and spec 1
    §5's contract page, conformance tests and owner sign-off still apply.
  - From 1.0, claiming provisional is the deliberate promotion out of
    experimental.
- **Forgetting the claim fails safe.** The symbol gets the weaker promise,
  never an unintended stronger one.
- **Moves and aliases.** A moved or newly aliased path enters at the entry tier
  unless it carries its own claim. This closes the hole where an experimental
  (or provisional) object moved into a stable namespace would silently gain the
  stronger promise.
- **Aliases validate; they never lift.** Within one commit, all public paths to
  one object must resolve to the same tier independently. The producer child
  checks this at runtime, because the dump does not record alias groups.
  Disagreement is refused. An unclaimed new alias of an explicitly stable object
  fails.
- **Inherited exposure.** Members and nested classes inherit their owner's tier.
  So a NEW public member or nested class of an owner above the entry tier needs
  its own explicit admission. That covers a method added to a stable class, and
  an object moved into one.
- **Claims are authorization.** A per-symbol claim equal to its namespace's
  default is valid, and NOT refused as noise, because the claim is what
  authorizes the tier.
- **Grandfathering at 1.0.** The 1.0 transition commit writes an explicit
  per-path claim into the manifest for every symbol that is provisional or
  stable at that point, generated from the pre-1.0 inventory. After 1.0, a
  pre-1.0 protected symbol without a claim is a transition error, never a
  silent drop to experimental.

## Transition rules (per symbol)

| From → to | Rule |
|---|---|
| new, at the entry tier | free; no manifest edit |
| new, above the entry tier | an explicit per-symbol claim |
| experimental → removed, or any change while experimental at both ends | free; the checker skips compatibility findings |
| experimental → provisional or stable | explicit claim, which is the promotion. If the same commit also reshapes the symbol, it needs a breaking mark, which closes demote → reshape → promote laundering |
| provisional → stable | explicit claim; no breaking mark; spec 1 §5's contract page, conformance tests and sign-off |
| stable → provisional | needs a mark, and the manifest edit names each affected symbol |
| provisional or stable → experimental | before 1.0: n/a, because experimental does not exist yet. From 1.0: **refused for any symbol that shipped protected in a release**; deprecation is the only way out |
| provisional or stable → removed | before 1.0: a breaking mark (a hard cutover is fine). From 1.0: only through the deprecation lifecycle below |
| a namespace removed | each symbol is judged by its own tier and lifecycle |

## Deprecation and removal (a binding lifecycle, separate from tiers)

**Stability is a property of the object's claim. The lifecycle is a property of
the PATH:** active → deprecated → removed. Deprecation never replaces the tier.
A deprecated stable path keeps "stable" as its tier.

- **When it is required.** From 1.0, a stable or provisional symbol is never
  removed outright. It goes through deprecation. Provisional symbols may still
  CHANGE with a mark, but they never vanish without a window. Before 1.0, hard
  cutovers are fine, and deprecation is optional.
- **What can be deprecated:**
  - a path: an old import location stays as an alias, and the object stays at
    its new path;
  - a whole object;
  - a whole namespace, which covers its protected descendants.
- **Manifest metadata** per deprecated path: `since`, `remove_in` and an optional
  `use` replacement.
  - Cancelling a deprecation is allowed.
  - Re-deprecating starts a new window.
  - Deprecating an experimental symbol is allowed, and is not a promotion.
  - A removal requires every affected protected path to be eligible.
- **The window is one major release.** Something deprecated during 1.x may be
  removed no earlier than 2.0: the window is crossing the next major-version
  boundary.
  - Deprecations made before 1.0 count from 1.0, so 2.0 is the earliest removal.
  - Pre-releases do not count.
- **`remove_in` is the earliest eligibility, not a deadline.** Keeping a symbol
  longer is fine. A published `remove_in` may be extended and never shortened.
  `since` is immutable once published.
- **Deprecated means usable, not frozen.**
  - Compatible maintenance is allowed.
  - An incompatible fix needs a mark and keeps the original removal
    eligibility.
  - Deprecating a class covers its existing members; retiring one member alone
    needs its own lifecycle entry.
  - A deprecated API must stay usable: replacing its body with an
    unconditional error is a removal, not maintenance.
- **No new protected dependencies on deprecated APIs.** A new stable or
  provisional signature must not take or return an object-wide-deprecated type,
  and dependents migrate before removal. This is a REVIEW rule, with no
  analyzer.
- **The release gate.**
  - Per-commit structural checks stay as they are.
  - A release-time gate takes the explicit CANDIDATE version before tagging. It
    proves a removal is eligible by finding a **published release, on the same
    lineage, that shipped the deprecated binding and its warning**. `since`
    cannot create history.
  - Lifecycle failures stay fatal even when the bump version is overridden.
- **The runtime warning.** It is the standard `DeprecationWarning`, naming `use`
  and `remove_in`.
  - Objects use `typing_extensions.deprecated` (`warnings.deprecated` on 3.13+).
  - A deprecated path alias warns when the public path resolves, from the lazy
    `__getattr__`.
  - Warning triggers are defined separately for constants, modules and typing
    aliases.
  - **Prerequisite:** the decorator and the API-dump producer must work
    together. A probe found two conflicts: a decorated coroutine on 3.14 hit a
    producer refusal, and a decorated class on 3.10 recorded `(*args,
    **kwargs)` as its constructor. Test functions, coroutines, generators,
    classes and subclassing across 3.10–3.14 before anything is deprecated.
  - **Tests** run real consumer operations in fresh processes: attribute access,
    from-import, star import, `importlib`, repeated access, and a prior
    submodule load. They assert the category, message, caller location and
    function. Negative controls show the replacement path does not warn.
    Fixtures are explicit, so "every entry warns" is not circular.
  - `DeprecationWarning` is hidden by default, so the **badges and release notes
    are the dependable channel** and the runtime warning is a bonus. Document how
    to enable it. Autodoc and the agreement checks capture the expected
    deprecations narrowly. The docs build is tested under both its normal
    configuration and warnings-as-errors, and nothing is suppressed globally.
- **Tombstones.** A `removed` path lives in a SEPARATE registry: it is not in
  `__all__` or the lazy tables. It reserves the path and its descendants
  forever, and refuses reintroduction by any route: eager, lazy or submodule.
  - Tombstones are kept indefinitely. If they ever clutter the API docs, prune
    the DISPLAY only, never the reservation.
  - The import hint ("removed in 2.0, use X") is best-effort.
    `from pkg import Gone` raises a generic `ImportError`, and a deleted module
    has no `__getattr__`. Where the hint matters, keep an explicit module shim.
- **Breaches are breaches.** `Corrects:` can repair a missing breaking mark, but it
  cannot supply a notice period users never received. An accidental removal
  of a protected path is reported as a breach. It cannot be restored over the
  tombstone rule, and it has its own recovery note.
- **Release events.** Deprecation, cancellation, extension, removal and
  correction are each separate release events. Release notes are derived from
  the events, not from diffs between endpoints. Until "What's new" (#603)
  exists, an interim announcement channel carries them (*proposal*: a
  "Deprecations" section in the changelog).

## Docs marking

- **Stable is never marked.** It is what a reader assumes.
- **In P1:**
  - a stability page states each tier's promise, the entry-tier rule and the
    lifecycle;
  - a banner marks each module whose public symbols are all non-stable;
  - a **site-wide notice** says otto's API is provisional before 1.0, so a
    reader landing mid-page sees it.

  Per-symbol badges are not shipped while every symbol carries the same one,
  because about 540 identical badges would carry no information.
- **Once the first mixed module exists:** every non-stable symbol gets a badge
  at its anchor, on every page that documents it.
- **A deprecated path** gets a badge: "Deprecated since 1.4, removable from
  2.0; use X". A tombstoned path keeps a historical docs entry.
- **The docs test** enumerates anchors from the BUILT site (`objects.inv` and
  the HTML), not from the hook, so it can fail. Every non-stable or deprecated
  anchor carries its marking, and no stable anchor carries one.

## Mechanically checkable leakage (cheap half)

`mro` entries and `E:` enum defaults already name sets of public bindings. An
experimental base class, or an experimental enum default, in a non-experimental
binding is detectable from the dump at no producer cost, so the checker refuses
it. Annotations stay a review rule: the dump does not record them (dump spec
§1).

## Work

- [ ] **Spec.** Amend spec 1 §5 and §6 with everything above: the per-tier
  promises, the entry-tier rule, the transition table, the lifecycle, the
  release gate, tombstones and the docs marking.
- [ ] **Manifest reader** (`scripts/api_manifest.py`): per-symbol claims,
  lifecycle metadata, the tombstone registry, and validation.
- [ ] **Checker** (`scripts/check_breaking_marks.py`):
  - entry-tier resolution;
  - claim enforcement;
  - the child's alias-consistency report;
  - inherited-exposure admission;
  - the transition table;
  - the cheap leakage half;
  - the 1.0 grandfathering check;
  - tests for every row, with merges and both parent orders.
- [ ] **The release gate** (`scripts/release_bump.py` and the release flow): the
  explicit candidate version, and lifecycle eligibility against published
  releases.
- [ ] **Runtime:** decorator and dump interoperability first; then the
  deprecated-path warnings in the lazy `__getattr__`; then the consumer-operation
  tests.
- [ ] **Docs:** the Sphinx hook, the stability page, the site-wide notice, the
  badges, and the built-site docs test. ✅ in P1 (14caec5b): the Sphinx hooks (scripts/docs_api_reference.py, wired in docs/conf.py), the stability page, the site-wide notice, the module banners, and the built-site check (scripts/check_docs_api_marks.py), which gates both docs lanes: make docs locally and nox -s docs in CI. Left: the mixed-tier and per-symbol work, which waits for the first module whose symbols differ in tier: a badge at each non-stable or deprecated symbol's anchor, and the built-site check's per-anchor rule for it.
- [ ] **Review checklist** in `docs/contributing.md`:
  - no experimental type in a stable or provisional signature (annotations,
    bases, defaults, field types, return types);
  - no new protected dependency on a deprecated API.

## Later

- [ ] A per-release "What's new" page (issue #603), held until otto is more
  stable.
