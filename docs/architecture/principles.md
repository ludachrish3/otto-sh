# Design principles

Recurring rules the codebase holds itself to. They are not aspirations —
each one is enforced somewhere concrete, and a change that violates one
should expect to be challenged in review.

## Pydantic at the boundary, plain objects inside

External data (lab JSON, settings TOML, environment) is validated exactly
once, by a spec model, at the moment it enters; what circulates inside is
ordinary objects that are never re-validated. Corollary: `otto.models` stays
a leaf — it must not import the app graph it describes. ({doc}`subsystems/data-boundary`)

## Customize behavior in code, shape in data

Lab data declares what machines *are* (addresses, menus, profiles,
preferences); code declares what to *do* with them (products, providers,
backends). Products deliberately cannot be declared in lab data — the two
evolve on different clocks and are owned by different people.
({doc}`subsystems/hosts`)

## One registry idiom, symmetric registration

Every extension seam is a {class}`~otto.registry.Registry`: loud duplicates
with origin attribution, did-you-mean lookups, uniform `--list-*` support.
Built-ins register through the same public functions third parties use, so
the seams cannot rot unnoticed. ({doc}`subsystems/registries`)

## Dependencies flow through the context

{class}`~otto.context.OttoContext` carries the per-invocation runtime, and
components that want their dependencies visible take a `ctx`. The
context-variable behind {func}`~otto.context.get_context` is plumbing for
the zero-argument convenience accessors — not license for hidden globals.
({doc}`lifecycles/index`)

## Deterministic lifecycles, no `__del__`

Resources are closed by scopes (`HostScope`, `async with`, explicit
`close()`), never by garbage collection. `__del__` was removed deliberately;
teardown must be orderly and observable — containers close before their
parent's connection, sessions drain before sockets. ({doc}`lifecycles/index`,
{doc}`subsystems/docker-hosts`)

## The CLI edge is lazy and unbrickable

`import otto` is lazy (PEP 562); `otto --help` imports zero subcommand
modules; shell completion's fast path runs zero user code; a malformed
`settings.toml` or broken init module degrades to a framed warning, never a
traceback before argv parsing. A deterministic import-budget guard in the
test suite keeps startup cost from regressing. ({doc}`subsystems/registries`,
{doc}`lifecycles/index`)

## Fail loud, fail framed

No silent fallbacks: unknown registry names error with suggestions rather
than guessing; coverage stops on counter/build mismatch with the fix in the
message rather than emitting a plausible-but-wrong report; an unreachable
host is an error naming the host, not a skipped test. Graceful degradation
is reserved for *listing* paths (help, completion); *dispatch* fails loud.

## Logging: most-restrictive wins, and only for command I/O

Sensitivity composes by `max`: if either the host or the call says `QUIET`
or `NEVER`, the stricter mode holds. And {class}`~otto.logger.mode.LogMode`
gates command echo/output only — never warnings or errors, so silencing
chatter can't hide failures. ({doc}`utilities/logging`)

## One event loop, stateless strategies

Concurrency is asyncio only — no thread pools mixed into the loop; fan-out
uses `oneshot`/`gather` with per-host error isolation. Strategy objects
(command frames, binary loaders, filesystems) are small stateless values a
session or host *holds*, keeping them unit-testable without live hardware.
({doc}`subsystems/hosts`)

## Speak existing conventions

Where an established tool has trained users, otto follows: exit codes are
ssh-like (`255` = never ran, otherwise the shell's retcode); test semantics
are pytest's, not a reinvention; suites and instructions share option
classes; JSON/TOML field names are `snake_case`, matching the Python they
become. ({doc}`utilities/results`, {doc}`lifecycles/test`)

## Documentation is part of the change

Public behavior ships with docs and doctests: pure functions carry `>>>`
examples that run in CI, markdown examples run through the Sphinx doctest
builder, and the nitpicky, warnings-as-errors docs build means a renamed
class breaks the build rather than the reader. (See
{doc}`../contributing`.)
