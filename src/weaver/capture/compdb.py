"""Compilation databases and response files.

A compilation database records translation-unit commands and permits several
commands for one file; each entry becomes one *unit* (a file compiled under one
configuration).  Response files are expanded for analysis but their paths and
hashes are retained so the original invocation can be reproduced.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weaver.errors import ConfigError
from weaver.util import sha256_bytes, short_hash


@dataclass
class ResponseFile:
    path: str
    sha256: str
    tokens: list[str]


@dataclass
class CompileCommand:
    directory: str
    file: str  # absolute path of the source file
    arguments: list[str]  # original argv (response files NOT expanded)
    expanded: list[str]  # argv with response files expanded
    output: str | None = None
    response_files: list[ResponseFile] = field(default_factory=list)
    index: int = 0  # position in the database (a file may appear more than once)

    @property
    def compiler(self) -> str:
        return self.expanded[0]

    def unit_id(self, profile_id: str) -> str:
        return "u" + short_hash(profile_id, self.directory, self.file, self.arguments, length=10)

    def to_json(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "file": self.file,
            "arguments": self.arguments,
            "expanded": self.expanded,
            "output": self.output,
            "index": self.index,
            "response_files": [rf.__dict__ for rf in self.response_files],
        }


def split_gnu_response(text: str) -> list[str]:
    """Tokenise a response file the way GCC's ``buildargv`` does.

    Whitespace separates arguments; single and double quotes group; a
    backslash escapes the next character (inside or outside quotes).
    """
    out: list[str] = []
    cur: list[str] = []
    have = False
    quote: str | None = None
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            cur.append(text[i + 1])
            have = True
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
            i += 1
            continue
        if c in "'\"":
            quote = c
            have = True
        elif c.isspace():
            if have:
                out.append("".join(cur))
                cur = []
                have = False
        else:
            cur.append(c)
            have = True
        i += 1
    if have:
        out.append("".join(cur))
    return out


def quote_gnu_response(args: list[str]) -> str:
    """Inverse of :func:`split_gnu_response`; accepted by GCC and Clang."""

    def q(a: str) -> str:
        if a == "":
            return "''"
        return "".join("\\" + c if c in "\\'\" \t\n\r\f\v" else c for c in a)

    return "\n".join(q(a) for a in args) + "\n"


def expand_response_files(argv: list[str], cwd: str, depth: int = 0) -> tuple[list[str], list[ResponseFile]]:
    if depth > 16:
        raise ConfigError("response files nested too deeply")
    out: list[str] = []
    rfs: list[ResponseFile] = []
    for i, a in enumerate(argv):
        if i > 0 and a.startswith("@") and len(a) > 1:
            p = Path(a[1:])
            if not p.is_absolute():
                p = Path(cwd) / p
            if p.is_file():
                data = p.read_bytes()
                toks = split_gnu_response(data.decode(errors="surrogateescape"))
                rfs.append(ResponseFile(str(p), sha256_bytes(data), toks))
                sub, subrfs = expand_response_files(["_"] + toks, cwd, depth + 1)
                out.extend(sub[1:])
                rfs.extend(subrfs)
                continue
            # GCC treats a missing @file as a literal argument.
        out.append(a)
    return out, rfs


def load_compdb(path: str | os.PathLike[str]) -> list[CompileCommand]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"compilation database not found: {path}")
    try:
        entries = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: expected a JSON array")
    cmds = []
    for idx, e in enumerate(entries):
        try:
            directory = e["directory"]
            file = e["file"]
        except (KeyError, TypeError) as ex:
            raise ConfigError(f"{path}: entry {idx} lacks directory/file") from ex
        if "arguments" in e:
            args = [str(a) for a in e["arguments"]]
        elif "command" in e:
            args = shlex.split(e["command"])
        else:
            raise ConfigError(f"{path}: entry {idx} lacks arguments/command")
        if not os.path.isabs(directory):
            directory = str((path.parent / directory).resolve())
        fpath = file if os.path.isabs(file) else os.path.join(directory, file)
        expanded, rfs = expand_response_files(args, directory)
        out = e.get("output")
        cmds.append(
            CompileCommand(
                directory=os.path.normpath(directory),
                file=os.path.normpath(fpath),
                arguments=args,
                expanded=expanded,
                output=out,
                response_files=rfs,
                index=idx,
            )
        )
    return cmds


def write_compdb(path: str | os.PathLike[str], cmds: list[dict[str, Any]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(cmds, indent=2) + "\n")
