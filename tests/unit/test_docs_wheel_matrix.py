"""Guard the air-gap wheel claims in docs/installation.md.

The gate under test answers one question: would the documented download
recipe actually place, inside the air gap, a wheel for every non-pure runtime
dependency on every Python otto claims to support? Every hostile condition
here is INJECTED into a synthetic lock/table — inheriting the real repo's
(currently healthy) state would score green no matter what the gate did.
"""

import importlib.util

from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "check_docs_wheel_matrix.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_docs_wheel_matrix", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheels(*names):
    return [{"url": f"https://example.invalid/{n}"} for n in names]


# --------------------------------------------------------------------------
# classify(): what kind of wheel matrix does this package ship?
# --------------------------------------------------------------------------


def test_classify_pure_package():
    mod = _load_gate()
    assert mod.classify(_wheels("pydantic-2.13.4-py3-none-any.whl")) == mod.PURE


def test_classify_per_version_package():
    mod = _load_gate()
    wheels = _wheels(
        "pydantic_core-2.46.4-cp310-cp310-manylinux_2_17_x86_64.whl",
        "pydantic_core-2.46.4-cp311-cp311-manylinux_2_17_x86_64.whl",
    )
    assert mod.classify(wheels) == mod.PER_VERSION


def test_classify_abi3_package():
    mod = _load_gate()
    wheels = _wheels(
        "cryptography-49.0.0-cp39-abi3-manylinux_2_34_x86_64.whl",
        "cryptography-49.0.0-cp314-cp314t-manylinux_2_34_x86_64.whl",
    )
    assert mod.classify(wheels) == mod.ABI3


def test_classify_per_version_with_pure_fallback():
    mod = _load_gate()
    wheels = _wheels(
        "tomli-2.4.1-cp311-cp311-manylinux_2_17_x86_64.whl",
        "tomli-2.4.1-py3-none-any.whl",
    )
    assert mod.classify(wheels) == mod.PER_VERSION_PURE_FALLBACK


# --------------------------------------------------------------------------
# runtime_closure(): dev dependencies must never reach the air-gap claims
# --------------------------------------------------------------------------


def _lock(packages, root_deps, root_dev_deps=()):
    entries = [
        {
            "name": "otto-sh",
            "dependencies": [{"name": n} for n in root_deps],
            "dev-dependencies": {"dev": [{"name": n} for n in root_dev_deps]},
        }
    ]
    entries.extend(packages)
    return {"package": entries}


def test_runtime_closure_is_transitive():
    mod = _load_gate()
    lock = _lock(
        packages=[
            {"name": "asyncssh", "dependencies": [{"name": "cryptography"}], "wheels": []},
            {"name": "cryptography", "dependencies": [{"name": "cffi"}], "wheels": []},
            {"name": "cffi", "wheels": []},
        ],
        root_deps=["asyncssh"],
    )
    assert mod.runtime_closure(lock).keys() == {"asyncssh", "cryptography", "cffi"}


def test_runtime_closure_excludes_dev_dependencies():
    mod = _load_gate()
    lock = _lock(
        packages=[
            {"name": "asyncssh", "wheels": []},
            {"name": "ruff", "wheels": []},
        ],
        root_deps=["asyncssh"],
        root_dev_deps=["ruff"],
    )
    assert mod.runtime_closure(lock).keys() == {"asyncssh"}


def test_runtime_closure_keeps_every_version_of_a_forked_package():
    # markdown-it-py resolves to 3.0.0 below 3.11 and 4.0.0 at/above it; both
    # entries must be considered or the gate inspects only one fork's wheels.
    mod = _load_gate()
    lock = _lock(
        packages=[
            {"name": "markdown-it-py", "version": "3.0.0", "wheels": []},
            {"name": "markdown-it-py", "version": "4.0.0", "wheels": []},
        ],
        root_deps=["markdown-it-py"],
    )
    assert len(mod.runtime_closure(lock)["markdown-it-py"]) == 2


# --------------------------------------------------------------------------
# wheel_gaps(): the air-gap question itself
# --------------------------------------------------------------------------

MINORS = [(3, 10), (3, 11), (3, 12)]


def test_wheel_gaps_flags_a_missing_minor():
    # INJECTED hole: cp311 ships, cp310 and cp312 do not.
    mod = _load_gate()
    wheels = _wheels("pydantic_core-2.46.4-cp311-cp311-manylinux_2_17_x86_64.whl")
    gaps = mod.wheel_gaps("pydantic-core", wheels, MINORS, "x86_64")
    assert len(gaps) == 2
    assert "3.10" in gaps[0]


def test_wheel_gaps_accepts_forward_compatible_abi3():
    # One cp39-abi3 wheel covers every supported minor at or above 3.9.
    mod = _load_gate()
    wheels = _wheels("cryptography-49.0.0-cp39-abi3-manylinux_2_34_x86_64.whl")
    assert mod.wheel_gaps("cryptography", wheels, MINORS, "x86_64") == []


def test_wheel_gaps_rejects_abi3_wheel_newer_than_the_target():
    # A cp312-abi3 wheel does NOT run on 3.10/3.11 — abi3 is forward-only.
    mod = _load_gate()
    wheels = _wheels("example-1.0-cp312-abi3-manylinux_2_34_x86_64.whl")
    gaps = mod.wheel_gaps("example", wheels, MINORS, "x86_64")
    assert len(gaps) == 2


def test_wheel_gaps_accepts_a_pure_wheel_for_every_minor():
    mod = _load_gate()
    wheels = _wheels("tomli-2.4.1-py3-none-any.whl")
    assert mod.wheel_gaps("tomli", wheels, MINORS, "x86_64") == []


def test_wheel_gaps_ignores_a_wheel_for_another_platform():
    # macOS-only wheels must not satisfy a Linux air gap.
    mod = _load_gate()
    wheels = _wheels(
        "pydantic_core-2.46.4-cp310-cp310-macosx_11_0_arm64.whl",
        "pydantic_core-2.46.4-cp311-cp311-manylinux_2_17_x86_64.whl",
        "pydantic_core-2.46.4-cp312-cp312-manylinux_2_17_x86_64.whl",
    )
    gaps = mod.wheel_gaps("pydantic-core", wheels, MINORS, "x86_64")
    assert len(gaps) == 1
    assert "3.10" in gaps[0]


def test_wheel_gaps_ignores_free_threaded_only_builds():
    # cp314t is the free-threaded ABI; it does not satisfy a stock 3.14.
    mod = _load_gate()
    wheels = _wheels("cryptography-49.0.0-cp314-cp314t-manylinux_2_34_x86_64.whl")
    assert mod.wheel_gaps("cryptography", wheels, [(3, 14)], "x86_64") != []


# --------------------------------------------------------------------------
# Docs parsing
# --------------------------------------------------------------------------

TABLE = """\
### Native-extension dependencies

| Package | Pulled in by | Wheel matrix | Notes |
| ------- | ------------ | ------------ | ----- |
| `cffi` | cryptography | per-version | C FFI bindings |
| `cryptography` | asyncssh | abi3 | SSH encryption |

## Next section
"""


def test_parse_native_table_reads_package_and_matrix():
    mod = _load_gate()
    assert mod.parse_native_table(TABLE) == {"cffi": "per-version", "cryptography": "abi3"}


def test_parse_native_table_stops_at_the_next_section():
    mod = _load_gate()
    text = TABLE + "\n| `ruff` | dev | per-version | not a runtime dep |\n"
    assert "ruff" not in mod.parse_native_table(text)


def test_supported_minors_come_from_pyproject_classifiers():
    mod = _load_gate()
    pyproject = (
        "[project]\n"
        'requires-python = ">=3.10"\n'
        "classifiers = [\n"
        '  "Programming Language :: Python :: 3",\n'
        '  "Programming Language :: Python :: 3.10",\n'
        '  "Programming Language :: Python :: 3.11",\n'
        "]\n"
    )
    assert mod.supported_minors(pyproject) == [(3, 10), (3, 11)]


def test_parse_download_minors_reads_the_recipe_loop():
    mod = _load_gate()
    docs = "Some prose.\n\n```bash\nfor PYVER in 3.10 3.11 3.12; do\n    echo hi\ndone\n```\n"
    assert mod.parse_download_minors(docs) == [(3, 10), (3, 11), (3, 12)]


# --------------------------------------------------------------------------
# audit(): the assembled gate, and the mutations that must turn it red
# --------------------------------------------------------------------------


def _healthy_inputs(mod):
    lock = _lock(
        packages=[
            {
                "name": "cryptography",
                "wheels": _wheels("cryptography-49.0.0-cp39-abi3-manylinux_2_34_x86_64.whl"),
            },
            {"name": "certifi", "wheels": _wheels("certifi-2026.1.1-py3-none-any.whl")},
        ],
        root_deps=["cryptography", "certifi"],
    )
    pyproject = (
        "[project]\n"
        'requires-python = ">=3.10"\n'
        "classifiers = [\n"
        '  "Programming Language :: Python :: 3.10",\n'
        '  "Programming Language :: Python :: 3.11",\n'
        "]\n"
    )
    docs = (
        "```bash\nfor PYVER in 3.10 3.11; do\n    echo hi\ndone\n```\n\n"
        "### Native-extension dependencies\n\n"
        "| Package | Pulled in by | Wheel matrix | Notes |\n"
        "| ------- | ------------ | ------------ | ----- |\n"
        "| `cryptography` | asyncssh | abi3 | SSH encryption |\n"
    )
    return lock, pyproject, docs


def test_audit_is_clean_on_consistent_inputs():
    mod = _load_gate()
    lock, pyproject, docs = _healthy_inputs(mod)
    assert mod.audit(lock, pyproject, docs) == []


def test_audit_flags_a_non_pure_package_missing_from_the_table():
    # INJECTED: a compiled package enters the closure, docs never mention it.
    mod = _load_gate()
    lock, pyproject, docs = _healthy_inputs(mod)
    lock["package"].append(
        {"name": "tomli", "wheels": _wheels("tomli-2.4.1-cp310-cp310-manylinux_2_17_x86_64.whl")}
    )
    lock["package"][0]["dependencies"].append({"name": "tomli"})
    problems = mod.audit(lock, pyproject, docs)
    assert any("tomli" in p and "missing from" in p for p in problems)


def test_audit_flags_a_table_row_that_is_not_a_runtime_dependency():
    mod = _load_gate()
    lock, pyproject, docs = _healthy_inputs(mod)
    docs += "| `numpy` | nothing | per-version | not actually a dep |\n"
    problems = mod.audit(lock, pyproject, docs)
    assert any("numpy" in p for p in problems)


def test_audit_flags_a_wrong_wheel_matrix_label():
    # INJECTED: docs claim abi3; the lock says one wheel per minor.
    mod = _load_gate()
    lock, pyproject, docs = _healthy_inputs(mod)
    for entry in lock["package"]:
        if entry["name"] == "cryptography":
            entry["wheels"] = _wheels(
                "cryptography-49.0.0-cp310-cp310-manylinux_2_34_x86_64.whl",
                "cryptography-49.0.0-cp311-cp311-manylinux_2_34_x86_64.whl",
            )
    problems = mod.audit(lock, pyproject, docs)
    assert any("abi3" in p and "per-version" in p for p in problems)


def test_audit_flags_a_recipe_that_skips_a_supported_python():
    # INJECTED: pyproject supports 3.10 and 3.11; the recipe downloads only 3.11.
    mod = _load_gate()
    lock, pyproject, docs = _healthy_inputs(mod)
    docs = docs.replace("for PYVER in 3.10 3.11;", "for PYVER in 3.11;")
    problems = mod.audit(lock, pyproject, docs)
    assert any("3.10" in p and "recipe" in p for p in problems)


def test_audit_flags_a_dependency_with_no_wheel_for_a_supported_python():
    # INJECTED: the true air-gap failure — nothing to install on 3.11.
    mod = _load_gate()
    lock, pyproject, docs = _healthy_inputs(mod)
    for entry in lock["package"]:
        if entry["name"] == "cryptography":
            entry["wheels"] = _wheels("cryptography-49.0.0-cp310-cp310-manylinux_2_34_x86_64.whl")
    docs = docs.replace("| abi3 |", "| per-version |")
    problems = mod.audit(lock, pyproject, docs)
    assert any("no wheel" in p and "3.11" in p for p in problems)


# --------------------------------------------------------------------------
# The live repository must satisfy its own gate.
# --------------------------------------------------------------------------


def test_live_repo_passes_the_gate():
    mod = _load_gate()
    assert mod.main([]) == 0
