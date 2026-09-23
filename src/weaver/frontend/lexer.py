"""A raw (unpreprocessed) C lexer over source bytes.

The AST only shows code that was active in the analysed configurations.  The
raw lexer sees *all* source text, including inactive conditional groups and
macro bodies, so Weaver can (a) check the exact tokens inside an edit span,
(b) find textual references to a name that no AST explains, and (c) compute
which token-bearing lines each configuration actually compiled.

Offsets are byte offsets (the text is decoded as Latin-1 so indices match the
file bytes exactly, as in Clang's ``offset`` fields).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

PUNCTUATORS = sorted(
    [
        "%:%:",
        "...",
        "<<=",
        ">>=",
        "->",
        "++",
        "--",
        "<<",
        ">>",
        "<=",
        ">=",
        "==",
        "!=",
        "&&",
        "||",
        "*=",
        "/=",
        "%=",
        "+=",
        "-=",
        "&=",
        "^=",
        "|=",
        "##",
        "<:",
        ":>",
        "<%",
        "%>",
        "%:",
        "::",
        "[",
        "]",
        "(",
        ")",
        "{",
        "}",
        ".",
        "&",
        "*",
        "+",
        "-",
        "~",
        "!",
        "/",
        "%",
        "<",
        ">",
        "^",
        "|",
        "?",
        ":",
        ";",
        "=",
        ",",
        "#",
        "@",
        "$",
        "\\",
        "`",
    ],
    key=len,
    reverse=True,
)

IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
IDENT_CONT = IDENT_START | set("0123456789")
STRING_PREFIXES = ("u8", "u", "U", "L")


@dataclass(slots=True)
class Token:
    kind: str  # ident | number | string | char | punct | header | other
    text: str
    start: int
    end: int
    line: int
    directive: str | None = None  # name of the directive this token belongs to, if any

    @property
    def in_directive(self) -> bool:
        return self.directive is not None


@dataclass(slots=True)
class Directive:
    name: str
    start: int  # offset of '#'
    end: int  # offset of the terminating newline (exclusive of it)
    line_start: int
    line_end: int
    tokens: list[Token] = field(default_factory=list)

    @property
    def is_conditional(self) -> bool:
        return self.name in ("if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif")


@dataclass
class LexResult:
    text: str
    tokens: list[Token]
    directives: list[Directive]
    line_starts: list[int]

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def _token_starts(self) -> list[int]:
        starts = self.__dict__.get("_starts")
        if starts is None:
            starts = self.__dict__["_starts"] = [t.start for t in self.tokens]
        return starts

    def index_at(self, offset: int) -> int | None:
        """Index of the token starting exactly at ``offset``."""
        starts = self._token_starts()
        i = bisect.bisect_left(starts, offset)
        return i if i < len(starts) and starts[i] == offset else None

    def tokens_in(self, start: int, end: int) -> list[Token]:
        """Tokens lying entirely within [start, end)."""
        i = bisect.bisect_left(self._token_starts(), start)
        out = []
        while i < len(self.tokens) and self.tokens[i].start < end:
            if self.tokens[i].end <= end:
                out.append(self.tokens[i])
            i += 1
        return out

    def code_lines(self) -> set[int]:
        """Physical lines carrying at least one non-directive token."""
        out: set[int] = set()
        for t in self.tokens:
            if t.directive is None:
                out.add(t.line)
        return out


def lex(data: bytes | str) -> LexResult:
    text = data.decode("latin-1") if isinstance(data, bytes) else data
    n = len(text)
    line_starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            line_starts.append(i + 1)

    tokens: list[Token] = []
    directives: list[Directive] = []
    i = 0
    line = 1
    at_line_start = True  # only whitespace/comments seen since the last logical newline
    cur_dir: Directive | None = None
    expect_dir_name = False

    def newline_at(pos: int) -> None:
        nonlocal line, at_line_start, cur_dir, expect_dir_name
        line += 1
        at_line_start = True
        if cur_dir is not None:
            cur_dir.end = pos
            cur_dir.line_end = line - 1
            directives.append(cur_dir)
            cur_dir = None
            expect_dir_name = False

    while i < n:
        c = text[i]
        # Line splice: backslash-newline continues the logical line.
        if c == "\\" and i + 1 < n and (text[i + 1] == "\n" or text.startswith("\r\n", i + 1)):
            i += 2 if text[i + 1] == "\n" else 3
            line += 1
            continue
        if c == "\n":
            newline_at(i)
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += text.count("\n", i, j)
            i = j
            continue
        if text.startswith("//", i):
            j = i
            while j < n and text[j] != "\n":
                if text[j] == "\\" and j + 1 < n and text[j + 1] == "\n":
                    line += 1
                    j += 2
                    continue
                j += 1
            i = j
            continue

        start = i
        start_line = line
        kind = "other"
        # Directive start
        if c == "#" and at_line_start and cur_dir is None:
            cur_dir = Directive(name="", start=i, end=n, line_start=line, line_end=line)
            expect_dir_name = True
            at_line_start = False
            i += 1
            tok = Token("punct", "#", start, i, start_line, directive="")
            cur_dir.tokens.append(tok)
            tokens.append(tok)
            continue
        at_line_start = False

        # Header name inside #include
        if cur_dir is not None and cur_dir.name in ("include", "include_next", "import") and c == "<":
            j = text.find(">", i)
            if j >= 0 and "\n" not in text[i:j]:
                i = j + 1
                kind = "header"
        if kind == "other":
            pref = next(
                (p for p in STRING_PREFIXES if text.startswith(p, i) and i + len(p) < n and text[i + len(p)] in "\"'"),
                None,
            )
            if c in "\"'" or pref:
                q_at = i + (len(pref) if pref else 0)
                quote = text[q_at]
                j = q_at + 1
                while j < n and text[j] != quote and text[j] != "\n":
                    if text[j] == "\\" and j + 1 < n:
                        if text[j + 1] == "\n":
                            line += 1
                        j += 2
                        continue
                    j += 1
                i = min(j + 1, n)
                kind = "string" if quote == '"' else "char"
            elif c in IDENT_START or (c == "\\" and i + 1 < n and text[i + 1] in "uU"):
                j = i + 1
                while j < n and text[j] in IDENT_CONT:
                    j += 1
                i = j
                kind = "ident"
            elif c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
                j = i + 1
                while j < n:
                    if text[j] in "eEpP" and j + 1 < n and text[j + 1] in "+-":
                        j += 2
                        continue
                    if text[j].isalnum() or text[j] in "._'":
                        j += 1
                        continue
                    break
                i = j
                kind = "number"
            else:
                p = next((p for p in PUNCTUATORS if text.startswith(p, i)), None)
                if p is None:
                    i += 1
                    kind = "other"
                else:
                    i += len(p)
                    kind = "punct"
        tok = Token(kind, text[start:i], start, i, start_line)
        if cur_dir is not None:
            if expect_dir_name:
                cur_dir.name = tok.text if kind == "ident" else ""
                expect_dir_name = False
                for t in cur_dir.tokens:
                    t.directive = cur_dir.name
            tok.directive = cur_dir.name
            cur_dir.tokens.append(tok)
        tokens.append(tok)

    if cur_dir is not None:
        cur_dir.end = n
        cur_dir.line_end = line
        directives.append(cur_dir)
    return LexResult(text, tokens, directives, line_starts)
