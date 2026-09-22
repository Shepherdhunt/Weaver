import subprocess

from conftest import needs_clang

from weaver.analysis.uses import classify_ref
from weaver.fidelity.elf import symbol_sizes
from weaver.frontend.clang_ast import TranslationUnit

SRC = """\
#include "h.h"
#define DEREF(q) (*(q))
int use(int *p, int (*fp)(int)) {
    int x = *p;
    x += DEREF(p);
    if (p) x++;
    helper(p);
    return fp(x) + (p == 0);
}
"""


def _dump(tmp_path, src: str) -> TranslationUnit:
    (tmp_path / "h.h").write_text("static inline int helper(int *q) { return *q; }\n")
    (tmp_path / "u.c").write_text(src)
    out = tmp_path / "u.ast.json"
    with open(out, "wb") as f:
        subprocess.run(
            ["clang", "-fsyntax-only", "-Xclang", "-ast-dump=json", "u.c"], cwd=tmp_path, stdout=f, check=True
        )
    return TranslationUnit(out, str(tmp_path), str(tmp_path / "u.c"), str(tmp_path))


@needs_clang
def test_locations_resolve_elided_fields(tmp_path):
    tu = _dump(tmp_path, SRC)
    text = (tmp_path / "u.c").read_bytes()
    # Every DeclRefExpr to p in the main file starts at a byte offset holding 'p'.
    refs = [
        n for n in tu.all_nodes() if n.kind == "DeclRefExpr" and (n.raw.get("referencedDecl") or {}).get("name") == "p"
    ]
    assert len(refs) == 5
    for r in refs:
        off = r.begin.spelling.offset if r.begin.in_macro else r.begin.offset
        assert text[off : off + 1] == b"p"
        assert r.begin.file_loc.file == str((tmp_path / "u.c").resolve())
    # declarations from the header are materialised (kept under the root) with the header's path
    helper = [n for n in tu.top if n.name == "helper"][0]
    assert helper.loc.file.endswith("h.h") and helper.loc.line == 1


@needs_clang
def test_use_classification(tmp_path):
    tu = _dump(tmp_path, SRC)
    refs = sorted(
        (
            n
            for n in tu.all_nodes()
            if n.kind == "DeclRefExpr" and (n.raw.get("referencedDecl") or {}).get("name") in ("p", "fp")
        ),
        key=lambda n: n.begin.file_loc.offset,
    )
    kinds = [(classify_ref(r).kind, classify_ref(r).access) for r in refs]
    assert kinds == [
        ("deref", "read"),
        ("deref", "read"),  # inside DEREF(): classified, and flagged as macro by span checks
        ("null-test", None),
        ("call-arg", None),
        ("indirect-call", None),
        ("compare", None),
    ]
    assert refs[1].begin.in_macro and refs[1].file_span() is None


@needs_clang
def test_elf_symbol_sizes(tmp_path):
    (tmp_path / "s.c").write_text("char a[7] = {0}; char b[1] = {0}; struct { int x; long y; } c;\n")
    subprocess.run(["clang", "-c", "s.c", "-o", "s.o"], cwd=tmp_path, check=True)
    sizes = symbol_sizes(tmp_path / "s.o")
    assert sizes["a"] == 7 and sizes["b"] == 1 and sizes["c"] == 16
    (tmp_path / "x.txt").write_text("not elf")
    assert symbol_sizes(tmp_path / "x.txt") is None
