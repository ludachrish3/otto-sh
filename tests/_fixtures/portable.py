"""Write the paths a test ran under as placeholders, so a recorded golden is portable.

One home for the rule the golden modules share (``test_unchanged_loading``,
``test_reader_goldens``, ``test_link_composition_goldens``): a temporary
directory or the checkout root never reaches a committed artifact.
"""

from pathlib import Path


def portable(text: str, *, places: "dict[str, Path]") -> str:
    """*text* with each path in *places* written as its placeholder; longest path first.

    *places* maps a placeholder (``<A>``, ``<case>``, ``<root>``) to a path.
    Both the path as given and its resolved form are replaced, so a symlinked
    temporary directory cannot leak through.
    """
    spellings = sorted(
        {
            (str(form), placeholder)
            for placeholder, path in places.items()
            for form in (path, path.resolve())
        },
        key=lambda pair: -len(pair[0]),
    )
    for spelled, placeholder in spellings:
        text = text.replace(spelled, placeholder)
    return text
