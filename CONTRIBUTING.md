# Contributing to otto-sh

Thanks for your interest in contributing! The full contributor guide —
development environment setup, test layout, documentation conventions, type
checking, and AI-assisted contribution policy — lives at
[docs/contributing.md](docs/contributing.md).

## TL;DR

The `Makefile` is the contract. Before opening a pull request, run:

```bash
make all
```

This runs the same pipeline CI runs: `clean-dist → typecheck → coverage →
docs → build`. If `make all` is green locally, CI should be green too.

For a one-time setup on a fresh checkout:

```bash
make dev      # `uv sync` + install git hooks
```

See [docs/contributing.md](docs/contributing.md) for everything else.
