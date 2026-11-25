# A link to a host whose record was skipped says why (#1305)

## Problem

A lab source skips a host record it cannot read when it computes the
addressing its declared links resolve against, and logs the reason at debug
level only. The skip is deliberate: the record may belong to another lab,
whose typo or missing registration must not fail this lab's load. A declared
link that names such a host then fails with `unknown host '<id>' (no such host
in any lab file)`. The host is in the file; the reason it was not loaded is
invisible.

Reproduced on main `4d11834e4`: an element of another lab whose host record
carries an unknown field, and a link from a host of the loaded lab to it.

## Goals and non-goals

- The error for a link that names a skipped host says the record was found
  and skipped, where, and why.
- A skip no attached link touches stays quiet (debug), as today.
- Additive: a custom lab source that reports no skips behaves as today.
- Not a goal: making skips loud, or changing which records are skipped.

## Approaches

1. **Element-name match, skip list on `DeclaredLinkInputs` (chosen).** The
   source remembers each skipped record as its element name, its document and
   the reason. On an endpoint nothing addresses, a skipped record whose
   element slug is the endpoint id, or a prefix of it up to a `-`, is reported
   as the cause. Host ids are `<element>-<board>[-<slot>]`, and a record that
   fails validation usually has no computable id, so the element name is the
   one fact always available. It is the same prefix rule the "did you mean"
   hint uses.
2. Report only when the skipped record's exact id can be computed. Misses the
   case in the issue (validation fails before the id exists).
3. Attach the skip list to the mapping the JSON source returns. Avoids a
   public field but hides data in a duck-typed attribute, and the layered
   lookup would have to forward it.

Chris approved approach 1 (2026-11-25).

## Design

- `otto.labs.protocol.SkippedHostRecord` (frozen): `element` (the element's
  name), `origin` (the document), `reason` (the error's text). Public, beside
  `DeclaredLinkInputs`.
- `DeclaredLinkInputs.skipped`: a function returning the source's skipped
  records. Optional; the default returns none. Called only when an attached
  link names an endpoint nothing addresses.
- The JSON source computes addressing and skips in one pass, once per load;
  `addressing` and `skipped` read the same result.
- `resolve_declared_links` takes the skipped records of every contribution
  (a link of one source may name a host another source skipped).
  `_resolve_endpoint`, on a miss, reports matching skipped records:

  ```
  host 'bb1310-qemu' was not loaded: the record of element 'bb1310' in <file> was skipped: <reason>
  ```

  Several matches (an element with two skipped hosts) are all listed. With no
  match the message is today's, hint included.

## Tests

- Unit, link resolution: a miss with a matching skipped record names element,
  document and reason; a non-matching one (`bb1` against `bb1310-qemu`) keeps
  today's message; two matches list both.
- Through the JSON source: the issue's reproduction; the same lab without the
  link still loads quietly; a link in one source naming a host another source
  skipped.
- A contribution that supplies no `skipped` resolves as before.

## Left to the builder

Exact wording; where the one-pass result lives.
