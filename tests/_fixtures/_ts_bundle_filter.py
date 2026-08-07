"""The served-bundle URL filter shared by CDP collection and its drift guard.

``collect_ts_coverage`` (tests/_fixtures/_ts_coverage.py) keeps only V8 script
entries whose URL names one of our built bundles, and its zero-match guard
fires when the filter and the built bundles drift apart. That guard executes
only when ``OTTO_TS_COVERAGE`` is armed — which is make-only BY DESIGN (the
``make dashboard`` recipe: ad-hoc or ``nox`` runs must not append raw dumps
outside make's rm+stamp protocol). CI's three-engine dashboard matrix runs
via ``nox``, so it never armed the guard, and a vite output-layout change
would ship past exactly the lane most likely to see it first
(review 2026-08-06 §5.4, gate G13).

``bundle_filter_drift_reason`` is the configure-time twin: the browser
conftests run it against the on-disk dist in EVERY lane that collects those
suites — armed or not — without touching the coverage protocol. Both halves
share ``bundle_url_matches``, so the guard cannot diverge from the filter it
guards. This module stays free of playwright imports so unit tests (and the
conftests' configure hooks) can use it without pulling in the browser stack.
"""

from pathlib import Path


def bundle_url_matches(url: str) -> bool:
    """True if *url* names one of our served dist bundles.

    Two bundle shapes exist today: the monitor dashboard's hashed
    ``.../dist/assets/index-*.js`` and the covapp SPA's unhashed
    ``.../dist/covapp.js``. See ``collect_ts_coverage``'s docstring for why
    the predicate is deliberately this narrow (naming the exact shapes keeps
    the list self-documenting as bundles are added, instead of silently
    widening to "anything under dist/").
    """
    return "/assets/" in url or url.endswith("covapp.js")


def bundle_filter_drift_reason(dist_dir: Path, app: str) -> str | None:
    """Reason string if *app*'s built JS bundles have drifted off the filter.

    *dist_dir* is the root under which the app's built bundles live (the dist
    itself, or the app root containing it). ``None`` when at least one built
    bundle matches ``bundle_url_matches`` (collection would capture it) or
    when there are no JS bundles at all — a missing/empty dist is the
    stale-dist gate's finding, not ours. Mirrors the ``_stale_dist_reason``
    idiom so the conftests can ``pytest.exit`` with one clear message from
    ``pytest_configure`` (the only hook where that is xdist-safe — see the
    dashboard conftest's docstring).
    """
    js = sorted(
        p for pattern in ("*.js", "*.mjs", "*.cjs") for p in dist_dir.rglob(pattern) if p.is_file()
    )
    if not js:
        return None
    # The leading "/" is load-bearing: bundle_url_matches is a URL predicate,
    # and it must see the dist-RELATIVE path, never the absolute one — against
    # an absolute path, any ancestor directory that happens to be named
    # "assets" would forge a match and silently disable the guard for the
    # whole checkout (caught in review).
    if any(bundle_url_matches("/" + p.relative_to(dist_dir).as_posix()) for p in js):
        return None
    listing = ", ".join(str(p.relative_to(dist_dir)) for p in js[:10])
    return (
        f"ts-coverage filter drift: none of the {app} dist's built JS bundles "
        f"match bundle_url_matches() (tests/_fixtures/_ts_bundle_filter.py), so "
        f"an armed coverage run would raise its zero-match guard on every test. "
        f"Either the build layout moved (update the filter) or the build is "
        f"wrong. Bundles under {dist_dir}: {listing}"
    )
