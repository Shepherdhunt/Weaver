"""Explicit option translation for a secondary Clang analysis frontend.

A secondary frontend needs an explicit translation of every relevant compiler
option (compiler plan §6).  Each kept, dropped or substituted option is
recorded; nothing is silently inherited from the host.  The target triple,
dialect and system include directories come from the *production* compiler, so
the host's default triple and headers never supply the target model (§8).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

from weaver.capture.toolid import ToolIdentity
from weaver.toolchain.options import SEPARATE_VALUE
from weaver.util import run

#: Bump whenever the translation rules change: part of the evidence cache key.
TRANSLATION_VERSION = 2

# Options passed through unchanged (prefix match).  Everything here is
# accepted by Clang with the same meaning as GCC's documented option.
KEEP_PREFIXES = (
    "-I",
    "-D",
    "-U",
    "-std=",
    "-O",
    "-iquote",
    "-isystem",
    "-idirafter",
    "-include",
    "-imacros",
    "--sysroot",
    "-isysroot",
    "-march=",
    "-mcpu=",
    "-mfpu=",
    "-mfloat-abi=",
    "-mabi=",
)
KEEP_EXACT = {
    "-ansi",
    "-m32",
    "-m64",
    "-mthumb",
    "-marm",
    "-mlittle-endian",
    "-mbig-endian",
    "-mno-red-zone",
    "-mgeneral-regs-only",
    "-nostdinc",
    "-ffreestanding",
    "-fno-builtin",
    "-fhosted",
    "-pthread",
    "-fstrict-aliasing",
    "-fno-strict-aliasing",
    "-fwrapv",
    "-fno-wrapv",
    "-fno-strict-overflow",
    "-fsigned-char",
    "-funsigned-char",
    "-fshort-enums",
    "-fno-short-enums",
    "-fshort-wchar",
    "-fcommon",
    "-fno-common",
    "-fPIC",
    "-fpic",
    "-fPIE",
    "-fpie",
    "-fno-pic",
    "-fno-pie",
    "-fno-delete-null-pointer-checks",
    "-fno-omit-frame-pointer",
    "-fomit-frame-pointer",
    "-fstack-protector",
    "-fstack-protector-strong",
    "-fstack-protector-all",
    "-fno-stack-protector",
    "-fopenmp",
    "-fms-extensions",
    "-fgnu89-inline",
    "-fno-asm",
    "-ffast-math",
    "-fno-fast-math",
    "-fno-math-errno",
    "-fexceptions",
    "-fno-exceptions",
    "-funwind-tables",
    "-fasynchronous-unwind-tables",
    "-fvisibility=hidden",
    "-fvisibility=default",
    "-fno-builtin-memcpy",
    "-fno-builtin-memset",
    "-fsingle-precision-constant",
    "-fdollars-in-identifiers",
    "-trigraphs",
}
KEEP_REGEX = re.compile(r"^-fno-builtin-\w+$|^-fpack-struct(=\d+)?$|^-fmax-type-align=\d+$")


@dataclass
class Translation:
    options: list[str]
    log: list[dict] = field(default_factory=list)

    def note(self, action: str, option: list[str], reason: str) -> None:
        self.log.append({"action": action, "option": option, "reason": reason})


def _std_from_macros(macros: dict[str, str]) -> str | None:
    ver = macros.get("__STDC_VERSION__", "").rstrip("L")
    strict = "__STRICT_ANSI__" in macros
    table = {
        "199409": "iso9899:199409" if strict else "gnu89",
        "199901": "c99" if strict else "gnu99",
        "201112": "c11" if strict else "gnu11",
        "201710": "c17" if strict else "gnu17",
    }
    if ver in table:
        return table[ver]
    if ver and ver > "201710":
        return "c2x" if strict else "gnu2x"
    if not ver and "__STDC__" in macros:
        return "c89" if strict else "gnu89"
    return None


@lru_cache(maxsize=16)
def gcc_system_include_dirs(gcc: str) -> tuple[str, ...]:
    r = run([gcc, "-E", "-v", "-x", "c", os.devnull, "-o", os.devnull], timeout=60)
    text = r.stderr.decode(errors="replace")
    dirs: list[str] = []
    active = False
    for line in text.splitlines():
        if line.startswith("#include <...> search starts here"):
            active = True
            continue
        if line.startswith("End of search list"):
            break
        if active and line.startswith(" "):
            dirs.append(os.path.normpath(line.strip().split(" (")[0]))
    return tuple(dirs)


def _is_gcc_internal(d: str) -> bool:
    # GCC's own builtin headers live in lib/gcc/<triple>/<version>/include[-fixed].
    return bool(re.search(r"/lib(exec)?/gcc(-cross)?/[^/]+/[^/]+/include(-fixed)?$", d))


# Feature macros some distributions' GCC builds define implicitly (e.g. Ubuntu's
# default _FORTIFY_SOURCE); they select different library declarations.
IMPLICIT_FEATURE_MACROS = (
    "_FORTIFY_SOURCE",
    "_FILE_OFFSET_BITS",
    "_TIME_BITS",
    "_LARGEFILE_SOURCE",
    "_LARGEFILE64_SOURCE",
    "_REENTRANT",
    "_GNU_SOURCE",
)


@lru_cache(maxsize=64)
def implicit_preincludes(gcc: str, options: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """Headers the production compiler includes before any source (e.g. stdc-predef.h)."""
    from weaver.toolchain.collect import parse_depfile

    r = run([gcc, *options, "-M", "-x", "c", os.devnull], cwd=cwd, timeout=60)
    if not r.ok:
        return ()
    deps = parse_depfile(r.stdout.decode(errors="replace"), cwd)
    return tuple(d for d in deps if d != os.path.normpath(os.devnull))


@lru_cache(maxsize=16)
def clang_resource_include(clang: str) -> str | None:
    r = run([clang, "-print-resource-dir"], timeout=60)
    if not r.ok:
        return None
    d = r.stdout.decode().strip()
    return os.path.join(d, "include") if d else None


def translate_gcc_to_clang(
    options: list[str],
    production: ToolIdentity,
    production_macros: dict[str, str],
    clang: str,
    cwd: str | None = None,
) -> Translation:
    t = Translation(options=[])
    has_target = False
    has_std = False
    nostdinc = False
    i = 0
    while i < len(options):
        a = options[i]
        nxt = options[i + 1] if i + 1 < len(options) else None
        if a in SEPARATE_VALUE and nxt is not None:
            pair = [a, nxt]
            if a in (
                "-I",
                "-D",
                "-U",
                "-iquote",
                "-isystem",
                "-idirafter",
                "-include",
                "-imacros",
                "--sysroot",
                "-isysroot",
                "-x",
            ):
                t.options.extend(pair)
                t.note("keep", pair, "same meaning in Clang")
            elif a in ("-target",):
                has_target = True
                t.options.extend(pair)
                t.note("keep", pair, "explicit target")
            else:
                t.note("drop", pair, "no translated equivalent recorded")
            i += 2
            continue
        if a.startswith("-std="):
            has_std = True
        if a.startswith("--target="):
            has_target = True
        if a == "-nostdinc":
            nostdinc = True
        if a.startswith(KEEP_PREFIXES) or a in KEEP_EXACT or KEEP_REGEX.match(a) or a.startswith("--target="):
            t.options.append(a)
            t.note("keep", [a], "same meaning in Clang")
        elif a.startswith(("-W", "-pedantic", "-fdiagnostics", "-fmessage-length", "-fcolor", "-w")):
            t.note("drop", [a], "diagnostic option; does not change semantics")
        elif a.startswith(("-g", "-fdebug", "-grecord")):
            t.note("drop", [a], "debug-info option; not needed for source analysis")
        elif a.startswith("-mtune="):
            t.note("drop", [a], "tuning only; no semantic effect")
        elif a in ("-pipe", "-v"):
            t.note("drop", [a], "driver behavior only")
        else:
            t.note("drop", [a], "not in the reviewed translation table; semantic effect UNRESOLVED")
        i += 1

    if not has_target and production.target:
        t.options.insert(0, f"--target={production.target}")
        t.note("substitute", [f"--target={production.target}"], "target triple from production compiler -dumpmachine")
    if not has_std:
        std = _std_from_macros(production_macros)
        if std:
            t.options.append(f"-std={std}")
            t.note("substitute", [f"-std={std}"], "dialect pinned to the production compiler's default")
    for name in IMPLICIT_FEATURE_MACROS:
        if name in production_macros and not any(o.startswith(("-D" + name, "-U" + name)) for o in options):
            val = production_macros[name]
            d = f"-D{name}={val}" if val else f"-D{name}"
            t.options.append(d)
            t.note("substitute", [d], "feature macro the production compiler defines implicitly")
    if production.path and cwd is not None:
        for inc in implicit_preincludes(production.path, tuple(options), cwd):
            t.options.extend(["-include", inc])
            t.note("substitute", ["-include", inc], "implicit pre-include of the production compiler")
    if not nostdinc and production.path:
        res = clang_resource_include(clang)
        sysdirs = [d for d in gcc_system_include_dirs(production.path) if not _is_gcc_internal(d)]
        extra = ["-nostdinc"]
        if res:
            extra += ["-isystem", res]
        for d in sysdirs:
            extra += ["-isystem", d]
        t.options.extend(extra)
        t.note(
            "substitute",
            extra,
            "system include search list taken from the production compiler; GCC-internal builtin "
            "headers replaced by Clang's resource headers",
        )
    return t
