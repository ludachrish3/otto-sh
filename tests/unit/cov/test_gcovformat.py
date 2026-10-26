"""The gcov header helper: which record layout a file uses, and which stamps llvm-cov reads.

The real files under ``data/`` come from clang 18.1.3 (``408*``, and ``B11*``
forced), clang 21.1.8 (``B11*`` by default) and gcc 15.2.0 (``B52*``).
"""

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from otto.host.gcovformat import (
    GCC_8,
    GCC_9,
    GCC_12,
    LLVM_402,
    LLVM_408,
    function_records,
    is_llvm_stamp,
    layout_for,
    parse_header,
)
from tests.unit.cov._gcno import TAG_ARCS_COUNTER, TAG_FUNCTION, gcc9_data, gcc9_notes

DATA = Path(__file__).parent / "data"
CLANG21_STAMP = 0xD03AECD7  # the stamp, and every function's cfg_checksum, of the clang fixtures


class TestLlvmStamps:
    @pytest.mark.parametrize("stamp", ["402*", "408*", "B11*"])
    def test_llvm_cov_reads_the_stamps_llvm_writes(self, stamp):
        assert is_llvm_stamp(stamp)

    @pytest.mark.parametrize("stamp", ["A95*", "B10*", "B12*", "B15*", "B33*", "B52*", "407*", ""])
    def test_every_other_stamp_is_gnu_gcovs(self, stamp):
        # B12*-B15* are gcc 11.2-11.5: only gcc 11.1.0 shares clang 21's stamp
        assert not is_llvm_stamp(stamp)


class TestLayout:
    @pytest.mark.parametrize(
        ("version", "layout"),
        [
            ("402*", LLVM_402),
            ("408*", LLVM_408),
            ("A80*", GCC_8),
            ("A95*", GCC_9),
            ("B11*", GCC_9),
            ("B15*", GCC_9),
            ("B24*", GCC_12),
            ("B52*", GCC_12),
        ],
    )
    def test_the_version_alone_picks_the_layout(self, version, layout):
        assert layout_for(version) is layout

    @pytest.mark.parametrize("version", ["407*", "A73*", "????", ""])
    def test_an_unsupported_version_has_no_layout(self, version):
        assert layout_for(version) is None

    def test_the_sublayouts_differ_where_the_parsers_need_them(self):
        assert (GCC_8.notes_cwd, GCC_8.notes_support_word, GCC_8.function_tail_words) == (
            False,
            True,
            3,
        )
        assert (GCC_9.header_len, GCC_9.byte_lengths, GCC_9.notes_cwd) == (12, False, True)
        assert (GCC_12.header_len, GCC_12.byte_lengths, GCC_12.notes_cwd) == (16, True, True)
        assert (LLVM_408.notes_cwd, LLVM_408.notes_support_word, LLVM_408.announce_words) == (
            False,
            False,
            3,
        )
        assert LLVM_402.announce_words == 2


class TestHeader:
    def test_a_little_endian_notes_header(self):
        header = parse_header((DATA / "clang-21.1.8-same.gcno").read_bytes())
        assert header is not None
        assert (header.notes, header.big_endian, header.version, header.stamp) == (
            True,
            False,
            "B11*",
            CLANG21_STAMP,
        )

    def test_a_big_endian_data_header(self):
        header = parse_header(b"gcdaB33*" + struct.pack(">I", 7))
        assert header is not None
        assert (header.notes, header.big_endian, header.version, header.stamp) == (
            False,
            True,
            "B33*",
            7,
        )

    @pytest.mark.parametrize("data", [b"oncg", b"\x7fELF\x02\x01\x01\x00"])
    def test_short_or_foreign_bytes_are_no_header(self, data):
        assert parse_header(data) is None


class TestFunctionRecords:
    def test_clang21_notes_and_data_walk_to_the_same_records(self):
        notes = (DATA / "clang-21.1.8-run.gcno").read_bytes()
        data = (DATA / "clang-21.1.8-run.gcda").read_bytes()
        want = [(0, 0xF9BECDE3, CLANG21_STAMP), (1, 0x62011EBB, CLANG21_STAMP)]
        for raw in (notes, data):
            header = parse_header(raw)
            assert header is not None
            assert function_records(raw, header, GCC_9) == want

    def test_gcc12_data_skips_zeroed_counters_and_absent_functions(self):
        raw = (
            b"adcg*33B"
            + struct.pack("<II", 0xABCD, 0)
            + struct.pack("<II", 0xA1000000, 8)
            + struct.pack("<II", 1, 0)
            + struct.pack("<II", TAG_FUNCTION, 12)
            + struct.pack("<III", 1, 10, 20)
            + struct.pack("<Ii", TAG_ARCS_COUNTER, -16)  # two zero counters, no payload
            + struct.pack("<II", TAG_FUNCTION, 0)  # a function another unit owns
            + struct.pack("<II", TAG_FUNCTION, 12)
            + struct.pack("<III", 2, 30, 40)
            + struct.pack("<II", TAG_ARCS_COUNTER, 8)
            + struct.pack("<Q", 5)
            + struct.pack("<I", 0)
        )
        header = parse_header(raw)
        assert header is not None
        assert function_records(raw, header, GCC_12) == [(1, 10, 20), (2, 30, 40)]

    def test_gcc11_data_skips_zeroed_counters(self):
        # a real gcc 11.4 .gcda: one function's arc counters are all zero, length -2 words
        raw = (DATA / "gcc-11.4-zero-counters.gcda").read_bytes()
        header = parse_header(raw)
        assert header is not None
        assert header.version == "B14*"
        assert function_records(raw, header, GCC_9) == [
            (0x0EA7D626, 0xD581357B, 0xDB5DE9E8),
            (0x090A34EC, 0xF57D68DC, 0xDB5DE9E8),
        ]

    def test_a_truncated_record_does_not_walk(self):
        raw = gcc9_notes("/w")[:-6]
        header = parse_header(raw)
        assert header is not None
        assert function_records(raw, header, GCC_9) is None

    def test_a_dangling_partial_record_head_does_not_walk(self):
        raw = gcc9_data(funcs=[(1, 2, 3)])[:-4] + b"\x00\x00\x00\x01\x05"
        header = parse_header(raw)
        assert header is not None
        assert function_records(raw, header, GCC_9) is None

    def test_a_big_endian_file_walks_in_its_own_byte_order(self):
        raw = (
            b"gcdaB11*"
            + struct.pack(">I", 9)
            + struct.pack(">II", TAG_FUNCTION, 3)
            + struct.pack(">III", 4, 5, 6)
        )
        header = parse_header(raw)
        assert header is not None
        assert function_records(raw, header, GCC_9) == [(4, 5, 6)]


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not installed")
def test_real_gcc_notes_and_data_walk(tmp_path):
    (tmp_path / "a.c").write_text(
        "int never(int x){return x*3;}\nint main(void){return 0;}\n"  # never(): zero counters
    )
    subprocess.run(["gcc", "--coverage", "a.c", "-o", "a"], cwd=tmp_path, check=True)
    subprocess.run(["./a"], cwd=tmp_path, check=True)
    notes, data = next(tmp_path.glob("*.gcno")), next(tmp_path.glob("*.gcda"))
    walked = []
    for path in (notes, data):
        raw = path.read_bytes()
        header = parse_header(raw)
        assert header is not None
        layout = layout_for(header.version)
        assert layout is not None
        records = function_records(raw, header, layout)
        assert records is not None
        walked.append(set(records))
    assert len(walked[1]) == 2
    assert walked[1] <= walked[0]
