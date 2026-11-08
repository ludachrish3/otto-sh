# API stability

otto is before 1.0, so its whole Python API is **provisional**. This page says
what that promises, which names are public, and how the reference marks them.
How to mark a change in a commit is in
[Branching and commits](../contributing.md#branching-and-commits).

## What is public

A name is public if and only if it is in the `__all__` of a namespace otto
declares: the modules under *Public API* in the {doc}`API reference <index>`.
A public class's members are public too, except those whose names start with
an underscore. The exception is a small set of dunders that are part of how a
class is used: `__call__`; `__enter__`, `__exit__`, `__aenter__` and
`__aexit__`; `__iter__`, `__next__`, `__aiter__` and `__anext__`; `__len__`,
`__contains__`, `__getitem__`, `__setitem__` and `__delitem__`; `__eq__` and
`__hash__`. Those are public members.

A public import path survives otto's internal reorganisation: moving the code
behind a path never moves the path.

Everything else is internal: the modules under *Internals*, and any module
path otto does not declare, even one that imports today. An internal module
may change, move or disappear in any release, unmarked.

## What each tier promises

| Tier | Promise |
|---|---|
| stable | Every breaking change is marked. The area has a documented behaviour contract and named conformance tests. |
| provisional | Every breaking change is marked. A removed, moved or renamed name, and every change to a recorded shape (a signature, a class's members and bases, an enum's members, a versioned format), is caught mechanically. A change of behaviour is caught by review. |
| experimental (from 1.0) | No compatibility promise beyond accurate labelling, and promotion out of it only by an explicit decision. |

A breaking change is *marked* by a `!` in its commit subject or a
`BREAKING CHANGE:` footer. The changelog shows it with a **BREAKING** badge,
and it decides the version bump. An addition is not breaking unless it is a
new required parameter, a new required input, or a new obligation on a class
you subclass.

No area is stable yet. An area becomes stable by promotion: a design of its
own that writes the area's contract page, names its conformance tests, and is
signed off by the owner.

## How a name gets its tier

- A new public name enters at the **entry tier**, with no declaration change:
  provisional before 1.0, experimental from 1.0.
- A tier above the entry tier is claimed explicitly, name by name. A
  forgotten claim gives the weaker promise, never a stronger one.
- A moved or newly aliased path enters at the entry tier unless it carries its
  own claim.

## How these pages mark it

- **Stable is never marked**: it is what you may assume.
- A module whose public names are all below stable opens with a banner naming
  the tier. Today that is every public module.
- Every page carries a notice that otto's API is provisional before 1.0.
- An Internals module opens with a note that it is internal.
- A class or callable with facts in the manifest's `[symbols]` table carries
  them as notes: *Rights* for a withdrawn right, *Positional parameters* or
  *Positional fields* for what it takes by position
  ([Calling conventions](#calling-conventions)).
- Once a module mixes tiers, each of its non-stable names carries a badge
  instead.

(api-calling-conventions)=
## Calling conventions

otto's public callables take **keyword arguments only**: call
`f(name=value)`, never `f(value)`, and build a record as
`Cls(name=..., value=...)`. A keyword call keeps working when a parameter is
added or the parameters are reordered; a positional one would not.

- **Positional subjects are the exceptions.** A few callables let you pass
  their leading parameters by position, because the call reads naturally
  that way. Each is listed in `api/public.toml`'s `[symbols]` table, with a
  reason whenever the entry lists two or more names or covers a
  positional-only or variadic (`*args`) parameter; a single plain subject
  needs none. Its entry in the {doc}`API reference <index>` carries a
  *Positional parameters* note naming them, and so does every override or
  conforming implementation the entry reaches. A parameter no note names is
  keyword-only.
- **A NamedTuple record is a tuple.** `MetricDataPoint` and `LogEvent` stay
  tuples, so their fields are positional, in their documented order, and you
  may unpack them. That makes their shape the contract: inserting, removing,
  reordering or even appending a field is a breaking change. Their entries
  carry a *Positional fields* note saying so.
- **Positional `match` patterns do not work on records.** A keyword-only
  dataclass has an empty `__match_args__`, so `case Cls(a, b)` raises
  `TypeError` when it is matched. Use keyword patterns:
  `case Cls(name=name, value=value)`.
- **otto's errors pickle.** An error rebuilds from its `args` when it is
  unpickled, so every error otto raises either keeps the parameters it does
  not carry in `args` defaulted, or defines `__reduce__`. An error can cross
  a process boundary and come back as the same class.
- **Some classes withdraw a right.** A class marked *not constructed by
  callers* is one otto builds and hands to you: its constructor makes no
  compatibility promise. A class marked *not for subclassing* makes no
  promise about its abstract members. The reference shows the
  mark as a *Rights* note under the class; a class without one may be
  constructed and subclassed.
- **`@final` restates a withdrawn subclass right.** A class marked *not for
  subclassing* is, as a rule, also decorated `@final`, so your type checker
  refuses a subclass before otto's docs have to. The manifest decides; the
  decorator only restates it, and
  [the quality gates](../architecture/quality-gates.md#what-binds-each-gate)
  keep the two in step. A few such classes are not `@final` because otto
  subclasses them itself; the mark still applies.

Switching a callable to keyword-only was a marked breaking change, made
once, before 1.0. How the rule is enforced is in
[the quality-gates table](../architecture/quality-gates.md#what-binds-each-gate).

## Removal and deprecation

Before 1.0, a public name may be removed or moved in any release, and the
change is marked. From 1.0, a stable or provisional name is never removed
outright: it is deprecated first, warns with a `DeprecationWarning` naming
its replacement, and may be removed no earlier than the next major release.
That is the recorded policy; the machinery that enforces it is still pending.
