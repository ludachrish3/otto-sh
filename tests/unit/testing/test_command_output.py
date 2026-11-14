from otto.testing.command_output import EXACT_OUTPUT_CASES


def test_value_is_the_text_with_one_trailing_newline_removed():
    by_id = {c.id: c for c in EXACT_OUTPUT_CASES}
    assert by_id["printf 'a\\n\\n'"].value == "a\n"
    assert by_id["printf 'abc'"].value == "abc"
    assert by_id["printf 'a\\r\\nb\\rc\\n\\n'"].value == "a\r\nb\rc\n"
    assert by_id["invalid-utf8"].value == "�"
    assert len(by_id["long-line-no-newline"].value) == 100_000
    assert len(EXACT_OUTPUT_CASES) == 19
    assert len(by_id) == 19


def test_the_table_is_exported():
    import otto.testing

    assert otto.testing.EXACT_OUTPUT_CASES is EXACT_OUTPUT_CASES
