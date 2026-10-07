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
- Once a module mixes tiers, each of its non-stable names carries a badge
  instead.

## Removal and deprecation

Before 1.0, a public name may be removed or moved in any release, and the
change is marked. From 1.0, a stable or provisional name is never removed
outright: it is deprecated first, warns with a `DeprecationWarning` naming
its replacement, and may be removed no earlier than the next major release.
That is the recorded policy; the machinery that enforces it is still pending.
