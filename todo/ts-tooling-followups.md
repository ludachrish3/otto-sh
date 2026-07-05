# TypeScript tooling — follow-ups

Teed up during the TS-tooling-parity work (branch `worktree-ts-tooling-parity`).
That branch brought `web/` to Python-parity for lint/format/type-check/coverage
(Biome + tsc + vitest v8), language-split `make` umbrellas, a CI `web-quality`
job, Dependabot npm, `.vscode` integration, and a non-interactive-safe
`prepare-commit-msg` hook. The items below were deliberately deferred.

## 1. Tighten Biome lint/format rules over time

The initial config (`web/biome.json`) is `recommended` + the React domain
(`useExhaustiveDependencies` / `useHookAtTopLevel` at error). Once the team is
comfortable, ratchet up: consider enabling additional `nursery`/`style`/
`suspicious` rules (e.g. `noExplicitAny`, stricter `complexity` rules), and
review whether `lineWidth` should match any future convention. Enforce +
fix properly rather than deny (per the repo's ruff philosophy).

## 2. Raise the vitest coverage floors

Baseline at adoption (`web/vite.config.ts` thresholds set ~2-3% below):

| Metric | Baseline | Floor set |
|--------|----------|-----------|
| Statements | 67.1% | 65 |
| Branches | 55.6% | 53 |
| Functions | 68.6% | 66 |
| Lines | 67.7% | 65 |

Biggest gaps to close first (drive the floor toward the Python side's ~94%):
`ChartPanel.tsx` (~1.6% — the Plotly-effect component, needs jsdom/RTL tests or
is left to the Playwright e2e), `EventToolbar.tsx` (~34%), `api/sse.ts` (~28%),
`EventPopover.tsx` (~34%). Raise the thresholds each time coverage climbs so it
stays a ratchet.

## 3. Firefox (Gecko) browser-engine testing

The Playwright dashboard e2e covers Chromium (Blink) + WebKit today. Add Firefox
(Gecko) for full main-engine coverage:
- CI: `playwright install --with-deps firefox` in the dashboard job; add a
  Firefox lane (parametrize `--browser firefox`, or a dedicated nox/Makefile
  target à la `dashboard-webkit`).
- Vagrantfile: Firefox's Playwright build needs its own system libs — add them
  to the dev-root provisioner's apt list (mirror the WebKit deps note), and to
  `make browsers` (`playwright install ... firefox`).
- Mind engine-specific pins (the WebKit Safari-modebar test) — most assertions
  should be engine-agnostic.

## 4. TypeScript performance tooling

For when perf concerns surface with more dashboard usage:
- `vitest bench` (zero new dep) to micro-benchmark the hot SSE data-path
  functions (`store.ts` metric reducers, `grouping.ts`, `retirement.ts`) — the
  TS analogue of the Python `hyperfine` / import-budget guard.
- A bundle-size budget (`size-limit` or `rollup-plugin-visualizer`) mirroring
  the Python import-budget guard, to keep the air-gapped Plotly bundle from
  silently bloating.
- React DevTools Profiler (+ optional `why-did-you-render` in dev) for
  unnecessary-re-render hunts.

## 5. Revisit the deliberate `biome-ignore` sites

Enabling the strict rules surfaced intentional patterns that were documented
with inline `biome-ignore` + rationale rather than "fixed":
- `ChartPanel.tsx` / `ChartGrid.tsx` / `EventPopover.tsx` — one-shot init and
  epoch-triggered Plotly effects deliberately omit deps. Evaluate whether a
  `useEffectEvent`/ref refactor could make the deps honest without changing the
  init/refresh behavior.
- `dashboard.css` — mode-toggle `!important` and base-then-state specificity.
  Check whether specificity alone can replace the `!important` overrides.

## 6. (Discovered, NOT this workstream) main docs gate is red

Building `make docs` on the branch base (`ac77da2`, origin/main) fails the
`-W` nitpicky gate with 25 `otto.host.login_proxy.Cred` / `login_proxy` xref
warnings: `src/otto/host/login_proxy.py` was added without a
`docs/api/host/loginproxy.rst` automodule page, so other host modules'
docstrings can't resolve `Cred`. Unrelated to TS tooling — belongs to the
login-proxy workstream. Fix is a one-file add (an `.. automodule::
otto.host.login_proxy` page + toctree entry), but confirm the login-proxy
work's intended doc placement first.
