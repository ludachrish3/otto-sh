"""End-to-end tests: the extension on dut clamps and divides, and prints each result."""

import pytest
from quickstart_embedded import call_fn


@pytest.mark.ensure("installed")
class TestMathops:
    """The loaded extension, one exported function per test."""

    @pytest.mark.parametrize(
        ("fn", "printed"),
        [
            ("op_clamp_lo", "op_clamp_lo=0"),
            ("op_clamp_in", "op_clamp_in=7"),
            ("op_div_ok", "op_div_ok=0,5"),
            ("op_div_zero", "op_div_zero=-1,0"),
        ],
    )
    async def test_result_is_printed(self, fn: str, printed: str) -> None:
        """The function prints the result the logic computes."""
        assert printed in await call_fn("mathops", fn)
