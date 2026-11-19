// The one rule otto enforces at commit time (.githooks/commit-msg): a `!` in the
// header and a `BREAKING CHANGE:` footer go together, or neither is present.
// No `extends`: a shared preset would bring header-length and case rules otto's
// subjects do not follow. `make check-breaking` (the pre-push hook and CI's
// lint-python) remains the authority: it also refuses an empty footer, a mark on
// a maintainer-only scope, and remediation git-cliff would cut
// (docs/contributing.md, the breaking-change section).
export default {
  rules: {
    "breaking-change-exclamation-mark": [2, "always"],
  },
};
