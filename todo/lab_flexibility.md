# Lab and Environment Metadata

## What already shipped (not this file's business)

The multi-source half of this thread is **done**: a repo declares an ordered
list of host-data sources as `[[lab.sources]]` entries, otto reads all of them
across all repos, and a later source overrides an earlier one per host record
with a warning naming both. That covers combining a global database with
repo-owned VM/QEMU definitions, and the collision question ("fail loudly?" —
answered: override, loudly).

- Design: `docs/superpowers/specs/2026-08-19-multi-source-lab-data-design.md`
- User guide: `docs/guide/setup/host-database.md`

## What is still open: metadata

Lab data today is *hosts and links* — records about individual devices. There
is no home for data that belongs to a **lab** or to an **environment** as a
whole, opaque to otto. The motivating example is a lab-scoped set of usernames
valid to query there; the general shape is a configuration blob a repo's own
code reads, keyed by lab and/or environment, that otto stores and hands over
without interpreting.

Open questions for whoever picks this up:

* What is the scoping model — lab-scoped, environment-scoped, or both, and how
  do the two compose when a lab appears in several environments?
* Where does it live: a new section in the lab data (so a host source
  provides it, like hosts and links), a settings table, or both?
* If a host source provides it, how does it survive multi-source merging —
  the same later-wins override as hosts, per-key merge, or something else?
* What is the read API (`get_lab_metadata(...)`? an attribute on `Lab`?), and
  what does otto guarantee about the shape it stores (deliberately: nothing
  beyond "JSON-ish and opaque")?

Deferred deliberately in the multi-source spec (§3, §12) — it is a data-model
question, not a source-plumbing one, and wants its own spec.
