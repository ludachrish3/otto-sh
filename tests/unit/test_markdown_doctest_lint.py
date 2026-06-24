import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "lint_markdown_doctests.py"


def _load_linter():
    spec = importlib.util.spec_from_file_location("lint_markdown_doctests", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURE = '''\
# Title

A clean executed example:

```{doctest}
>>> 1 + 1
2
```

An un-executed example that SHOULD be flagged:

```python
>>> dangerous()
None
```

Mid-line `>>>` is a remote prompt pattern, not a doctest prompt:

```python
await host.expect(r">>> ", timeout=5.0)
```

A four-backtick block that *displays* a doctest fence — not linted:

````markdown
```{doctest}
>>> from otto.utils import Status
>>> Status.Success
```
````

Intentional non-runnable pedagogy, exempted:

<!-- doctest-lint: ignore -->
```python
>>> add(1, 2)
3
```

A bare prompt loose in prose is also flagged:

>>> stray()
'''


def test_flags_only_unexecuted_prompts(tmp_path):
    linter = _load_linter()
    md = tmp_path / "sample.md"
    md.write_text(FIXTURE)
    offenses = linter.lint_file(md)
    # Assert on the offending line *content* (robust to fixture line-number
    # drift): only the un-executed ```python prompt and the bare-prose prompt
    # are flagged. The {doctest} block, the mid-line r">>> " regex, the
    # 4-backtick display block, and the ignore-exempted block are all clean.
    lines = FIXTURE.splitlines()
    flagged = {lines[n - 1].strip() for n, _ in offenses}
    assert flagged == {">>> dangerous()", ">>> stray()"}, offenses


def test_clean_file_has_no_offenses(tmp_path):
    linter = _load_linter()
    md = tmp_path / "clean.md"
    md.write_text("# Ok\n\n```{doctest}\n>>> 2 + 2\n4\n```\n")
    assert linter.lint_file(md) == []
