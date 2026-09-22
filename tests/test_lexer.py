from weaver.analysis.coverage import continuation_parent, uncovered_lines
from weaver.frontend.lexer import lex

SRC = b"""\
#include <stdio.h>
#define TWICE(x) \\
    ((x) + (x))
/* p in a comment */
int f(int *p) {
    const char *s = "p in a string";  // p here too
#if 0
    return *p;
#endif
    return TWICE(*p +
                 s[0]);
}
"""


def test_directives_and_tokens():
    lx = lex(SRC)
    names = [d.name for d in lx.directives]
    assert names == ["include", "define", "if", "endif"]
    define = lx.directives[1]
    assert (define.line_start, define.line_end) == (2, 3)
    assert any(t.kind == "header" and t.text == "<stdio.h>" for t in lx.tokens)
    # comments and string contents are not identifier tokens
    ps = [t for t in lx.tokens if t.kind == "ident" and t.text == "p"]
    assert sorted({t.line for t in ps}) == [5, 8, 10]
    # the p inside the directive-less '#if 0' body is still lexed (raw lexer)
    assert all(t.directive is None for t in ps)


def test_code_lines_exclude_directives_and_comments():
    lx = lex(SRC)
    assert lx.code_lines() == {5, 6, 8, 10, 11, 12}


def test_offsets_are_bytes():
    data = "int \xe9; int x;".encode("latin-1")
    lx = lex(data)
    x = [t for t in lx.tokens if t.text == "x"][0]
    assert data[x.start : x.end] == b"x"


def test_multiline_macro_arguments_count_as_covered():
    lx = lex(SRC)
    parents = continuation_parent(lx)
    assert parents[11] == 10  # line 11 is inside the TWICE( ... ) invocation opened on line 10
    # -E emits a multi-line macro invocation on its first line; the continuation inherits coverage
    active = {5, 6, 10, 12}
    unc = uncovered_lines(lx, active)
    assert 8 in unc  # '#if 0' body: unexamined
    assert 11 not in unc
