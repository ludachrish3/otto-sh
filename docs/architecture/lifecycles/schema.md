# otto schema — the data contracts, exported

`otto schema` is the smallest of the first-party commands and the purest
expression of the {doc}`data-boundary <../subsystems/data-boundary>` design:
because every external file otto reads is validated by a pydantic model,
those same models can be *emitted* as JSON Schemas. `otto schema export` writes schemas for
`lab.json`, `.otto/settings.toml`, and reservation files; editors pick
them up for completion and inline validation
({doc}`../../guide/editor-schemas`).

## What is unique about `schema`

- **Fully `lab_free`.** It never loads a lab, creates no output directory,
  and runs no gate — it reflects models, full stop. It is the reference
  example of a command that opts out of everything in the preamble
  ({doc}`index`).
- **Single source of truth.** The schema is generated from the exact model
  that validates ingest, so documentation, editor tooling, and runtime
  validation cannot disagree. When a host-spec field changes, the exported
  schema changes in the same commit — there is no second definition to
  update.
- **Extensions surface automatically.** Because project-registered host
  classes bring their own spec models ({doc}`../subsystems/hosts`), a repo
  that extends otto sees its fields in the export; `--builtins-only`
  restricts to otto's own types.

## `otto schema --help`

```{raw} html
:file: ../../_static/generated/termynal/help-schema.html
```
