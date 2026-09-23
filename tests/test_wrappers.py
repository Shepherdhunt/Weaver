"""Secondary-only forwarding wrappers (glibc fortify macros under Clang) and conditional segments."""

from __future__ import annotations

import subprocess

from conftest import needs_clang

from weaver.analysis.functions import callee_ref
from weaver.frontend.clang_ast import TranslationUnit
from weaver.frontend.preproc import conditional_segments
from weaver.frontend.wrappers import ArgUnwrapper, argument_capturing, forwarding_target, transparent_wrappers

PRINTF = "(fn)(...) __printf_chk (__USE_FORTIFY_LEVEL - 1, __VA_ARGS__)"


def test_forwarding_target_accepts_only_exact_forwarding():
    assert forwarding_target("printf", PRINTF) == "__printf_chk"
    assert (
        forwarding_target("fprintf", "(fn)(stream, ...) __fprintf_chk (stream, __USE_FORTIFY_LEVEL - 1, __VA_ARGS__)")
        == "__fprintf_chk"
    )
    rejected = {
        # a parameter used twice (once inside __glibc_objsize)
        "sprintf": "(fn)(str, ...) __builtin___sprintf_chk (str, 1, __glibc_objsize (str), __VA_ARGS__)",
        # stringification changes program text
        "LOG": "(fn)(x) printf(#x, x)",
        # a parameter inside an expression, not a whole argument
        "twice": "(fn)(x) f((x)+(x))",
        # forwards to a different function
        "puts": "(fn)(...) my_puts(__VA_ARGS__)",
        # the call is not the whole replacement list
        "vprintf": "(fn)(...) __vprintf_chk (1, __VA_ARGS__) + 1",
        # object-like
        "stderr": "stderr",
    }
    for name, d in rejected.items():
        assert forwarding_target(name, d) is None, name


def test_transparent_wrappers_require_absence_in_production():
    sec = {"printf": PRINTF, "NULL": "((void*)0)"}
    assert transparent_wrappers({}, sec) == {"printf": "__printf_chk"}
    # production defines the name too: a real difference, not a secondary-only wrapper
    assert transparent_wrappers({"printf": "(fn)(...) printf_impl(__VA_ARGS__)"}, sec) == {}


def test_argument_capturing_macros():
    macros = {"MAX": "(fn)(a, b) ((a) > (b) ? (a) : (b))", "M": "MAX", "stderr": "stderr", "N": "3"}
    assert argument_capturing(macros) == {"MAX", "M"}


def test_unwrapper_regions(tmp_path):
    src = tmp_path / "t.c"
    text = 'printf("%d %d\\n", g(&k), h(x));\nprintf("%d\\n", MAX(a, b));\nfprintf(stderr, "%d\\n", g(&k));\n'
    src.write_text(text)
    un = ArgUnwrapper({"printf": "__printf_chk", "fprintf": "__fprintf_chk"}, {"MAX", "printf", "fprintf"})
    first = text.index("printf")
    r = un.region(str(src), first)
    assert r is not None and text[r[0] : r[1]] == '"%d %d\\n", g(&k), h(x)'
    assert un.plain(str(src), first, text.index("g(&k)"))
    assert not un.plain(str(src), first, text.index("printf") + 1)  # the macro name itself
    # an argument naming a function-like macro may carry nested macro-argument tokens
    assert un.region(str(src), text.index("printf", first + 1)) is None
    # an object-like macro (stderr) cannot capture arguments
    assert un.region(str(src), text.index("fprintf")) is not None
    # not an invocation of a wrapper
    assert un.region(str(src), text.index("g(&k)")) is None


def test_conditional_segments(tmp_path):
    src = tmp_path / "s.c"
    src.write_text(
        "int a;\n"  # 1
        "#ifdef X\n"  # 2
        "int b;\n"  # 3
        "int c;\n"  # 4
        "#else\n"  # 5
        "int d;\n"  # 6
        "#endif\n"  # 7
        "int e;\n"  # 8
        "int f(int x,\n"  # 9  a call spanning lines stays one segment
        "      int y);\n"  # 10
    )
    assert conditional_segments(src) == [(1, 1), (3, 4), (6, 6), (8, 10)]


@needs_clang
def test_unwrapped_macro_arguments_are_plain_text(tmp_path):
    src = tmp_path / "w.c"
    src.write_text(
        "int __printf_chk(int, const char *, ...);\n"
        "#define printf(...) __printf_chk (1, __VA_ARGS__)\n"
        "static int g(const int *p) { return *p; }\n"
        'int main(void) { int k = 3; printf("%d\\n", g(&k)); return 0; }\n'
    )
    ast = tmp_path / "w.json"
    with open(ast, "wb") as out:
        subprocess.run(["clang", "-fsyntax-only", "-Xclang", "-ast-dump=json", str(src)], stdout=out, check=True)

    def call_to_g(tu: TranslationUnit):
        return next(
            n
            for top in tu.top
            for n in top.walk()
            if n.kind == "CallExpr"
            and (callee_ref(n) is not None)
            and callee_ref(n).raw["referencedDecl"]["name"] == "g"
        )

    plain = TranslationUnit(ast, str(tmp_path), str(src), str(tmp_path))
    assert call_to_g(plain).begin.in_macro and call_to_g(plain).file_span() is None

    un = ArgUnwrapper({"printf": "__printf_chk"}, {"printf"})
    tu = TranslationUnit(ast, str(tmp_path), str(src), str(tmp_path), un)
    call = call_to_g(tu)
    assert not call.begin.in_macro
    f, s, e = call.file_span()
    assert src.read_text()[s:e] == "g(&k)"
    assert tu.transparent_macros == ["printf"] and tu.unwrapped_locations > 0
