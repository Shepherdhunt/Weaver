"""Turn a production compile command into an analysis option list.

Following compiler plan §4: source filenames and conflicting output, action and
dependency-output options are removed *into a logged record*; every other
option is preserved in order.  The original invocation is retained separately
by the caller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from weaver.capture.compdb import CompileCommand
from weaver.toolchain.options import ACTION_FLAGS, DEP_FLAGS, OUTPUT_OPTIONS_SEPARATE, SEPARATE_VALUE

LINK_ONLY_SEPARATE = {"-Xlinker", "-T", "-e", "-u", "-z", "-L", "-l"}
LINK_ONLY_FLAGS = {
    "-static",
    "-shared",
    "-rdynamic",
    "-nostartfiles",
    "-nodefaultlibs",
    "-nostdlib",
    "-pie",
    "-no-pie",
    "-static-libgcc",
    "-shared-libgcc",
    "-s",
}


@dataclass
class Removed:
    option: list[str]
    reason: str

    def to_json(self) -> dict:
        return {"option": self.option, "reason": self.reason}


@dataclass
class SanitizedCommand:
    compiler: str
    options: list[str]
    source: str  # absolute path
    directory: str
    removed: list[Removed] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "compiler": self.compiler,
            "options": self.options,
            "source": self.source,
            "directory": self.directory,
            "removed": [r.to_json() for r in self.removed],
        }


def _same_file(arg: str, directory: str, target: str) -> bool:
    p = arg if os.path.isabs(arg) else os.path.join(directory, arg)
    return os.path.normpath(p) == os.path.normpath(target)


def sanitize(cmd: CompileCommand) -> SanitizedCommand:
    argv = cmd.expanded
    opts: list[str] = []
    removed: list[Removed] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None

        if a in OUTPUT_OPTIONS_SEPARATE and nxt is not None:
            removed.append(Removed([a, nxt], "output/side-output option; recipes write to the collection directory"))
            i += 2
            continue
        joined_output = (a.startswith("-o") and len(a) > 2) or (a.startswith(("-MF", "-MT", "-MQ")) and len(a) > 3)
        if joined_output:
            removed.append(Removed([a], "output/side-output option; recipes write to the collection directory"))
            i += 1
            continue
        if a in ACTION_FLAGS:
            removed.append(Removed([a], "action replaced by each recipe"))
            i += 1
            continue
        if a in DEP_FLAGS or a.startswith(("-Wp,-MD", "-Wp,-MMD")):
            removed.append(Removed([a], "dependency output replaced by the deps recipe"))
            i += 1
            continue
        if a.startswith(("-save-temps", "-fdump-", "-fcallgraph-info", "-fstack-usage", "-dumpdir", "-dumpbase")):
            removed.append(Removed([a], "production side output; recipes request their own"))
            i += 1
            continue
        if a.startswith("-flto") or a in ("-ffat-lto-objects", "-fno-fat-lto-objects"):
            removed.append(
                Removed([a], "LTO changes where artifacts appear; removed for per-unit collection (deviation)")
            )
            i += 1
            continue
        if a in LINK_ONLY_SEPARATE and nxt is not None:
            removed.append(Removed([a, nxt], "link-only option"))
            i += 2
            continue
        if a.startswith(("-Wl,", "-l", "-L")) or a in LINK_ONLY_FLAGS:
            removed.append(Removed([a], "link-only option"))
            i += 1
            continue
        if not a.startswith("-") and _same_file(a, cmd.directory, cmd.file):
            removed.append(Removed([a], "source file; supplied by each recipe"))
            i += 1
            continue
        if a in SEPARATE_VALUE and nxt is not None:
            opts.extend([a, nxt])
            i += 2
            continue
        if not a.startswith("-") and a != "-":
            # An input other than this unit's source (object, second source): not
            # part of this translation unit's compilation.
            removed.append(Removed([a], "other input; not part of this translation unit"))
            i += 1
            continue
        opts.append(a)
        i += 1
    return SanitizedCommand(compiler=argv[0], options=opts, source=cmd.file, directory=cmd.directory, removed=removed)
