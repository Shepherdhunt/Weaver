import pytest

from weaver.errors import StaleEvidenceError, WeaverError
from weaver.rewrite import Edit, OffsetMap, apply_edits, apply_to_bytes, check_overlaps, unified_diff
from weaver.util import sha256_bytes

SRC = b"int t = 3;\nint *p = &t;\n*p += 2;\n"


def edits():
    return [
        Edit("a.c", 11, 24, "int *p = &t;\n", "", "remove"),
        Edit("a.c", 24, 26, "*p", "t", "direct access"),
    ]


def test_apply_and_diff(tmp_path):
    (tmp_path / "a.c").write_bytes(SRC)
    changes = apply_edits(tmp_path, edits(), {"a.c": sha256_bytes(SRC)})
    assert changes["a.c"][1] == b"int t = 3;\nt += 2;\n"
    d = unified_diff(changes)
    assert "-*p += 2;" in d and "+t += 2;" in d


def test_refuses_stale_sources(tmp_path):
    (tmp_path / "a.c").write_bytes(SRC + b"// edited\n")
    with pytest.raises(StaleEvidenceError):
        apply_edits(tmp_path, edits(), {"a.c": sha256_bytes(SRC)})


def test_refuses_unexpected_bytes():
    bad = [Edit("a.c", 24, 26, "*q", "t", "x")]
    with pytest.raises(StaleEvidenceError):
        apply_to_bytes(SRC, bad)


def test_overlap_detection():
    with pytest.raises(WeaverError):
        check_overlaps([Edit("a.c", 0, 5, "", "", ""), Edit("a.c", 3, 8, "", "", "")])


def test_offset_map():
    m = OffsetMap(edits())
    assert m.map(0) == 0
    assert m.map(15) is None  # inside the removed declaration
    assert m.map(26) == 26 - 13 - 1  # after both edits
    assert m.replaced_range(edits()[1]) == (11, 12)
