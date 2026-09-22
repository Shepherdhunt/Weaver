"""GCC/Clang driver option tables shared by capture, collection and translation.

Only the GCC-compatible driver syntax is modelled here.  Vendor drivers with a
different syntax need their own adapter; nothing here is assumed to apply to
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_EXTS = {".c", ".i"}
ASM_EXTS = {".s", ".S", ".sx", ".asm"}
CXX_EXTS = {".cc", ".cpp", ".cxx", ".c++", ".C", ".ii"}
OBJECT_EXTS = {".o", ".obj", ".a", ".so", ".lo"}

# Options whose value is the *next* argument when not joined.
SEPARATE_VALUE = {
    "-o",
    "-I",
    "-D",
    "-U",
    "-include",
    "-imacros",
    "-isystem",
    "-iquote",
    "-idirafter",
    "-iprefix",
    "-iwithprefix",
    "-iwithprefixbefore",
    "-isysroot",
    "--sysroot",
    "-MF",
    "-MT",
    "-MQ",
    "-x",
    "-Xlinker",
    "-Xassembler",
    "-Xpreprocessor",
    "-Xclang",
    "-Xanalyzer",
    "-arch",
    "-target",
    "-L",
    "-l",
    "-T",
    "-e",
    "-u",
    "-z",
    "-aux-info",
    "-dumpdir",
    "-dumpbase",
    "-dumpbase-ext",
    "-G",
    "-specs",
    "-B",
    "-imultilib",
    "-ivfsoverlay",
    "-resource-dir",
    "--param",
    "-gcc-toolchain",
    "--gcc-toolchain",
    "-mllvm",
    "-working-directory",
}

# Joined prefixes of the options above (``-Ifoo``, ``-DX=1``, ``--sysroot=/x``).
JOINED_PREFIXES = (
    "-I",
    "-D",
    "-U",
    "-L",
    "-l",
    "-o",
    "-x",
    "-T",
    "-B",
    "--sysroot=",
    "-isystem",
    "-iquote",
    "-idirafter",
    "-MF",
    "-MT",
    "-MQ",
    "--target=",
    "-G",
)

ACTION_FLAGS = {"-c", "-S", "-E", "-fsyntax-only"}

# Options that name an output or a side output; removed from analysis argv.
OUTPUT_OPTIONS_SEPARATE = {"-o", "-MF", "-MT", "-MQ", "-dumpdir", "-dumpbase", "-dumpbase-ext", "-aux-info"}
DEP_FLAGS = {"-M", "-MM", "-MD", "-MMD", "-MP", "-MG"}

# Environment variables that change what the compiler does (recorded at capture).
RELEVANT_ENV_PREFIXES = (
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
    "LIBRARY_PATH",
    "GCC_EXEC_PREFIX",
    "COMPILER_PATH",
    "SOURCE_DATE_EPOCH",
    "DEPENDENCIES_OUTPUT",
    "SUNPRO_DEPENDENCIES",
    "SDKROOT",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PATH",
    "WIND_",
    "VSB_",
    "GHS_",
    "DIAB",
    "LYNX",
    "ENV_PREFIX",
    "CROSS_COMPILE",
    "CCACHE_",
)


def relevant_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k.startswith(RELEVANT_ENV_PREFIXES)}


def ext_of(path: str) -> str:
    dot = path.rfind(".")
    slash = path.rfind("/")
    return path[dot:] if dot > slash else ""


@dataclass
class ParsedArgs:
    """A driver command split into inputs, action and remaining options."""

    compiler: str
    sources: list[str] = field(default_factory=list)  # C sources (positions in argv)
    source_positions: list[int] = field(default_factory=list)
    asm_sources: list[str] = field(default_factory=list)
    other_inputs: list[str] = field(default_factory=list)  # objects, archives, unknown
    action: str | None = None  # -c / -S / -E / -fsyntax-only / None (compile+link)
    output: str | None = None
    language: str | None = None  # last -x value seen before the first source
    has_dep_flags: bool = False


def parse_driver_args(argv: list[str]) -> ParsedArgs:
    pa = ParsedArgs(compiler=argv[0])
    i = 1
    lang: str | None = None
    while i < len(argv):
        a = argv[i]
        if a in SEPARATE_VALUE and i + 1 < len(argv):
            if a == "-o":
                pa.output = argv[i + 1]
            if a == "-x":
                lang = argv[i + 1]
            i += 2
            continue
        if a.startswith("-o") and len(a) > 2:
            pa.output = a[2:]
        elif a.startswith("-x") and len(a) > 2:
            lang = a[2:]
        elif a in ACTION_FLAGS:
            pa.action = a
        elif a in DEP_FLAGS:
            pa.has_dep_flags = True
        elif a == "-" or not a.startswith("-"):
            ext = ext_of(a)
            if lang not in (None, "none"):
                if lang in ("c", "cpp-output"):
                    pa.sources.append(a)
                    pa.source_positions.append(i)
                elif lang.startswith("assembler"):
                    pa.asm_sources.append(a)
                else:
                    pa.other_inputs.append(a)
            elif ext in SOURCE_EXTS:
                pa.sources.append(a)
                pa.source_positions.append(i)
            elif ext in ASM_EXTS:
                pa.asm_sources.append(a)
            else:
                pa.other_inputs.append(a)
            if pa.language is None and lang is not None:
                pa.language = lang
        i += 1
    return pa
