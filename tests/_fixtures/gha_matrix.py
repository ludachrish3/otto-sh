"""A GitHub Actions job's matrix, rendered the way GitHub renders it.

One renderer for every test that reads a workflow: the check-run names a job
produces (``tests/unit/test_ci_gate_admission.py``) and the ``run:`` text each
matrix row executes (``tests/unit/host/test_busybox_artifacts.py``) both
substitute ``${{ matrix.<key> }}`` from the same rows.
"""

import itertools


def _axes(job: dict) -> dict:
    matrix = job.get("strategy", {}).get("matrix", {})
    return {k: v for k, v in matrix.items() if isinstance(v, list) and k != "include"}


def _include(job: dict) -> list[dict]:
    return list(job.get("strategy", {}).get("matrix", {}).get("include", []))


def _product(axes: dict) -> list:
    """Every combination of the axes' values, none when there are no axes."""
    return list(itertools.product(*axes.values())) if axes else []


def matrix_rows(job: dict) -> list[dict]:
    """Every matrix row of ``job``, with ``include`` applied as GitHub applies it.

    The rows start as the axes' product. Each ``include`` entry is then merged into
    every base row whose values match the keys the entry shares with the axes (an
    entry sharing no key with the axes matches every base row); its other keys are
    added to those rows. An entry that matches no base row is appended as a row of
    its own.
    """
    axes = _axes(job)
    base = [dict(zip(axes, combo, strict=True)) for combo in _product(axes)]
    rows = [dict(row) for row in base]
    for entry in _include(job):
        shared = {k: v for k, v in entry.items() if k in axes}
        matched = [
            row
            for row, original in zip(rows[: len(base)], base, strict=True)
            if all(str(original[k]) == str(v) for k, v in shared.items())
        ]
        if not matched:
            rows.append(dict(entry))
        for row in matched:
            row.update(entry)
    return rows


def render(text: str, row: dict) -> str:
    """``text`` with each ``${{ matrix.<key> }}`` replaced by ``row[key]``."""
    for key, value in row.items():
        text = text.replace("${{ matrix." + key + " }}", str(value))
    return text


def expand_names(job_id: str, job: dict) -> list[str]:
    """The check-run names ``job`` produces, as GitHub renders them.

    A ``${{ matrix.<key> }}`` in the name is replaced by each value of that key (``include``
    entries contribute their keys too). A matrix job whose name uses no such expression, or
    that has no ``name``, is rendered ``name (v1, v2)`` with the values in the matrix's key order.
    """
    base = job.get("name", job_id)
    axes, include = _axes(job), _include(job)
    if not axes and not include:
        return [base]
    tokens = {"${{ matrix." + k + " }}" for k in [*axes, *(k for i in include for k in i)]}
    if not any(t in base for t in tokens):
        values = [[str(x) for x in row.values()] for row in matrix_rows(job)]
        return [f"{base} ({', '.join(v)})" for v in values]
    return [render(base, row) for row in matrix_rows(job)]
