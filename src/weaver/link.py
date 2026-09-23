"""Link images and programs (compiler plan §3; tracker plan §5 "complete callers").

A profile's compile database says which translation units exist; the captured
link invocations (``links.json``) say how they become *images*: executables,
shared objects and relocatable objects.  A *program* is the set of images
that run together in one address space: a flight executable plus the modules
it loads, or a single host tool.  Flow evidence is computed per program, and
"who can call this function?" is answered per image:

* objects and archive members produced by analysed units are accounted for;
  archive members are read with ``ar t`` and mapped back to units;
* ``-l`` libraries are resolved to files, and their undefined symbols say
  whether they could call a project function by name;
* exported dynamic symbols of built shared objects, and the entry points a
  loader looks up by name (declared in ``weaver.yaml``), are the ways code
  outside the analysed program can reach a function.

``programs:`` in weaver.yaml groups images; without it every executable is
its own closed program and every shared object is an *open* program whose
exported functions may be called by unknown code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from weaver.capture.compdb import load_compdb
from weaver.config import Profile, Project
from weaver.util import read_json, sha256_file


@dataclass
class Image:
    name: str
    output: str  # absolute path of the linked file
    kind: str  # executable | shared | relocatable
    link: dict[str, Any]
    units: list[str] = field(default_factory=list)  # unit ids linked into the image
    archives: dict[str, list[str]] = field(default_factory=dict)  # archive path -> unit ids
    unmapped: list[str] = field(default_factory=list)  # inputs/members not produced by analysed units
    libraries: list[dict[str, Any]] = field(default_factory=list)  # {"flag", "path"}
    exports: set[str] | None = None  # defined dynamic symbols of the built file
    imports: set[str] | None = None  # undefined dynamic symbols of the built file

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "output": self.output,
            "kind": self.kind,
            "units": self.units,
            "archives": self.archives,
            "unmapped": self.unmapped,
            "libraries": self.libraries,
            "exports": sorted(self.exports) if self.exports is not None else None,
        }


@dataclass
class Program:
    name: str
    images: list[str]
    entry_points: list[str]
    closed: bool
    configured: bool
    units: list[str] = field(default_factory=list)


class LinkModel:
    def __init__(self, project: Project, profile: Profile):
        self.project = project
        self.profile = profile
        self.images: dict[str, Image] = {}
        self.problems: list[str] = []
        manifest = profile.link_manifest or profile.compile_commands.parent / "links.json"
        self.manifest = manifest if manifest.exists() else None
        cmds = load_compdb(profile.compile_commands)
        self.unit_file: dict[str, str] = {}
        by_output: dict[str, str] = {}
        by_basename: dict[str, list[tuple[str, str]]] = {}
        # (directory, source, argv) -> unit: a source compiled by the link command itself
        by_invocation: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for c in cmds:
            uid = c.unit_id(profile.id)
            self.unit_file[uid] = c.file
            by_invocation[
                (os.path.realpath(c.directory), os.path.realpath(os.path.join(c.directory, c.file)), tuple(c.arguments))
            ] = uid
            if c.output:
                out = os.path.realpath(os.path.join(c.directory, c.output))
                by_output[out] = uid
                by_basename.setdefault(os.path.basename(out), []).append((out, uid))
        self.output_unit = by_output  # realpath of a compile output -> unit id
        if self.manifest is None:
            self.problems.append(f"profile {profile.id}: no link manifest")
            self.programs: list[Program] = []
            self.unit_images: dict[str, list[str]] = {}
            return
        names: dict[str, int] = {}
        for link in read_json(self.manifest):
            out = os.path.realpath(os.path.join(link["cwd"], link.get("output") or "a.out"))
            argv = link.get("argv", [])
            kind = (
                "shared" if "-shared" in argv else "relocatable" if ("-r" in argv or "-Wl,-r" in argv) else "executable"
            )
            base = os.path.basename(out)
            names[base] = names.get(base, 0) + 1
            name = base if names[base] == 1 else f"{base}#{names[base]}"
            img = Image(name, out, kind, link)
            archives = [
                os.path.realpath(os.path.join(link["cwd"], i)) for i in link.get("inputs", []) if i.endswith(".a")
            ]
            loaded = self._loaded_members(name, link, archives) if archives else {}
            for inp in link.get("inputs", []):
                path = os.path.realpath(os.path.join(link["cwd"], inp))
                if path in by_output:
                    img.units.append(by_output[path])
                elif path.endswith(".a"):
                    members, unmapped = _archive_units(path, by_basename, loaded.get(path))
                    img.archives[path] = members
                    img.units.extend(members)
                    img.unmapped.extend(f"{inp}({m})" for m in unmapped)
                else:
                    img.unmapped.append(inp)
            srcs = link.get("compiled_sources", [])
            for src in srcs:
                path = os.path.realpath(os.path.join(link["cwd"], src))
                # a one-step "cc -o app a.c" is recorded as a compile entry with the link's arguments
                # (or, for several sources, the arguments without the other sources)
                others = {os.path.realpath(os.path.join(link["cwd"], o)) for o in srcs if o != src}
                argv = tuple(
                    a for a in link.get("argv", []) if os.path.realpath(os.path.join(link["cwd"], a)) not in others
                )
                uid = by_invocation.get((os.path.realpath(link["cwd"]), path, argv))
                if uid is not None:
                    img.units.append(uid)
                else:
                    img.unmapped.append(f"{src} (compiled during the link)")
            img.libraries = _libraries(argv, link["cwd"])
            if os.path.exists(out):
                img.exports, img.imports = _dynamic_symbols(out)
            self.images[name] = img
        self.unit_images = {}
        for img in self.images.values():
            for u in img.units:
                self.unit_images.setdefault(u, []).append(img.name)
        self.programs = self._programs()

    def _loaded_members(self, name: str, link: dict[str, Any], archives: list[str]) -> dict[str, set[str]]:
        """Archive members the linker actually loads, from a replay of the link with ``-t -t``.

        A static link pulls in only the members that resolve undefined symbols; the
        rest of an archive is not part of the image.  The replay writes to a scratch
        file and is cached by the command and the inputs' hashes.
        """
        from weaver.store import Store
        from weaver.util import short_hash, write_json

        argv = list(link.get("argv", []))
        key_parts = [argv, link["cwd"]]
        for a in archives:
            key_parts.append(sha256_file(a) if os.path.exists(a) else "missing")
        key = short_hash(*[str(k) for k in key_parts])
        cache = Store(self.project.state_dir).root / "links" / self.profile.id / f"{name}.members.json"
        if cache.exists():
            c = read_json(cache)
            if c.get("key") == key:
                return {k: set(v) for k, v in c["members"].items()}
        scratch = cache.parent / f"{name}.trace.out"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cmd = list(argv)
        if "-o" in cmd and cmd.index("-o") + 1 < len(cmd):
            cmd[cmd.index("-o") + 1] = str(scratch)
        else:
            cmd += ["-o", str(scratch)]
        try:
            p = subprocess.run([*cmd, "-Wl,-t,-t"], cwd=link["cwd"], capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.problems.append(f"{name}: link replay failed ({e}); archive membership over-approximated")
            return {}
        finally:
            if scratch.exists():
                scratch.unlink()
        if p.returncode != 0:
            self.problems.append(f"{name}: link replay failed; archive membership over-approximated")
            return {}
        members: dict[str, set[str]] = {}
        for line in p.stdout.splitlines():
            line = line.strip()
            if line.startswith("(") and ")" in line:
                arch, member = line[1:].split(")", 1)
                members.setdefault(os.path.realpath(os.path.join(link["cwd"], arch)), set()).add(member)
        write_json(cache, {"key": key, "members": {k: sorted(v) for k, v in members.items()}})
        return members

    # -- programs -------------------------------------------------------------
    def _programs(self) -> list[Program]:
        out: list[Program] = []
        used: set[str] = set()
        for spec in self.project.programs:
            if spec.get("profile") not in (None, self.profile.id):
                continue
            imgs = [str(i) for i in spec.get("images", [])]
            missing = [i for i in imgs if i not in self.images]
            if missing:
                self.problems.append(f"program {spec['name']}: no captured link produced {missing}")
            imgs = [i for i in imgs if i in self.images]
            used.update(imgs)
            out.append(
                Program(
                    name=str(spec["name"]),
                    images=imgs,
                    entry_points=[str(e) for e in spec.get("entry_points", [])],
                    closed=bool(spec.get("closed", True)),
                    configured=True,
                )
            )
        for img in self.images.values():
            if img.name in used or img.kind == "relocatable":
                continue
            if img.kind == "executable":
                out.append(Program(img.name, [img.name], ["main"], closed=True, configured=False))
            else:
                out.append(Program(img.name, [img.name], sorted(img.exports or []), closed=False, configured=False))
        for p in out:
            seen: set[str] = set()
            for i in p.images:
                for u in self.images[i].units:
                    if u not in seen:
                        seen.add(u)
                        p.units.append(u)
        return out

    def programs_of_unit(self, unit: str) -> list[Program]:
        return [p for p in self.programs if unit in p.units]

    def program(self, name: str) -> Program | None:
        return next((p for p in self.programs if p.name == name), None)

    # -- callers from outside the analysed code -------------------------------
    def external_callers(self, function: str, units: list[str], static: bool) -> list[tuple[str, str]]:
        """Ways code Weaver did not analyse could call ``function`` defined in ``units``.

        Returns (status, reason) pairs: status is ``violated`` when a caller is
        known to exist, ``unresolved`` when one cannot be excluded.
        """
        out: list[tuple[str, str]] = []
        if static:
            return out
        linked = [i for u in units for i in self.unit_images.get(u, [])]
        if not linked:
            if self.manifest is None:
                out.append(("unresolved", f"profile {self.profile.id}: no link manifest"))
            return out
        for name in sorted(set(linked)):
            img = self.images[name]
            for inp in img.unmapped:
                out.append(("unresolved", f"{name}: linked input {inp} was not produced by an analysed unit"))
            for lib in img.libraries:
                und = _undefined_symbols(lib["path"]) if lib.get("path") else None
                if und is None:
                    out.append(("unresolved", f"{name}: library {lib['flag']} could not be inspected"))
                elif function in und:
                    out.append(("violated", f"{name}: library {lib['flag']} ({lib['path']}) calls {function} by name"))
            progs = [p for p in self.programs if name in p.images]
            # Declared entry points (and main of an executable) are called from outside by construction.
            # An undeclared shared object's exports are only *possible* entry points: handled below.
            if function in {e for p in progs if p.configured or p.closed for e in p.entry_points}:
                out.append(("violated", f"{name}: {function} is an entry point looked up by name at run time"))
                continue
            if img.kind == "shared" and img.exports is not None and function in img.exports:
                if not any(p.closed and p.configured for p in progs):
                    out.append(
                        (
                            "unresolved",
                            f"{name}: {function} is exported from a shared object; code outside the analysed "
                            "program may call it (declare the program and its entry points in weaver.yaml)",
                        )
                    )
                else:
                    others = [
                        o
                        for p in progs
                        for o in p.images
                        if o != name and function in (self.images[o].imports or set())
                    ]
                    if others and not all(self.images[o].units for o in others):
                        out.append(("unresolved", f"{name}: {function} is imported by unanalysed images {others}"))
            elif img.kind == "shared" and img.exports is None:
                out.append(("unresolved", f"{name}: the built shared object is missing; exports unknown"))
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile.id,
            "manifest": str(self.manifest) if self.manifest else None,
            "manifest_sha256": sha256_file(self.manifest) if self.manifest else None,
            "images": [i.to_json() for i in self.images.values()],
            "programs": [p.__dict__ for p in self.programs],
            "problems": self.problems,
        }


def link_model(project: Project, profile: Profile) -> LinkModel:
    return LinkModel(project, profile)


# ---------------------------------------------------------------------------
# helpers (binutils)
# ---------------------------------------------------------------------------


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _archive_units(
    path: str, by_basename: dict[str, list[tuple[str, str]]], loaded: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """Map the (loaded) members of an archive to units that produced them.

    A member maps to the unit whose object has the same file name under the
    archive's build tree.  With ``loaded`` (from a traced link), members the
    linker did not load are left out; without it, every member counts.
    """
    ar = _tool("ar")
    if ar is None or not os.path.exists(path):
        return [], [os.path.basename(path)]
    p = subprocess.run([ar, "t", path], capture_output=True, text=True)
    if p.returncode != 0:
        return [], [os.path.basename(path)]
    root = os.path.dirname(path)
    units, unmapped = [], []
    for member in p.stdout.split():
        if loaded is not None and member not in loaded:
            continue
        cands = [u for out, u in by_basename.get(member, []) if out.startswith(root + os.sep)]
        if len(cands) == 1:
            units.append(cands[0])
        else:
            unmapped.append(member if not cands else f"{member} (ambiguous)")
    return units, unmapped


def _libraries(argv: list[str], cwd: str) -> list[dict[str, Any]]:
    dirs = []
    libs = []
    it = iter(range(len(argv)))
    for i in it:
        a = argv[i]
        if a.startswith("-L"):
            d = a[2:] or (argv[i + 1] if i + 1 < len(argv) else "")
            dirs.append(os.path.join(cwd, d))
        elif a.startswith("-l") and len(a) > 2:
            libs.append(a)
    out = []
    compiler = argv[0] if argv else "cc"
    for flag in libs:
        name = flag[2:]
        path = None
        for d in dirs:
            for ext in (".so", ".a"):
                cand = os.path.join(d, f"lib{name}{ext}")
                if os.path.exists(cand):
                    path = cand
                    break
            if path:
                break
        if path is None:
            path = _print_file_name(compiler, f"lib{name}.so") or _print_file_name(compiler, f"lib{name}.a")
        out.append({"flag": flag, "path": path})
    return out


@lru_cache(maxsize=256)
def _print_file_name(compiler: str, name: str) -> str | None:
    try:
        p = subprocess.run([compiler, f"-print-file-name={name}"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = p.stdout.strip()
    return os.path.realpath(out) if out and os.path.isabs(out) and os.path.exists(out) else None


@lru_cache(maxsize=256)
def _undefined_symbols(path: str) -> frozenset[str] | None:
    """Symbols a library references but does not define (it may call these by name)."""
    nm = _tool("nm")
    if nm is None or not os.path.exists(path):
        return None
    real = _linker_script_target(path)
    args = [nm, "--undefined-only", "--format=posix"]
    if real.endswith(".so") or ".so." in real:
        args.insert(1, "-D")
    p = subprocess.run([*args, real], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return frozenset(
        line.split()[0].split("@")[0] for line in p.stdout.splitlines() if line.strip() and ":" not in line
    )


def _linker_script_target(path: str) -> str:
    """libc.so and friends are often GNU ld scripts; follow to the first real shared object."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if head == b"\x7fELF" or head.startswith(b"!<ar"):
            return path
        text = Path(path).read_text(errors="replace")
    except OSError:
        return path
    import re

    m = re.search(r"GROUP\s*\(\s*([^\s)]+)", text) or re.search(r"INPUT\s*\(\s*([^\s)]+)", text)
    return m.group(1) if m and os.path.exists(m.group(1)) else path


def _dynamic_symbols(path: str) -> tuple[set[str] | None, set[str] | None]:
    nm = _tool("nm")
    if nm is None:
        return None, None
    d = subprocess.run([nm, "-D", "--defined-only", "--format=posix", path], capture_output=True, text=True)
    u = subprocess.run([nm, "-D", "--undefined-only", "--format=posix", path], capture_output=True, text=True)
    if d.returncode != 0:
        return set(), set()  # a static executable exports nothing dynamically

    def names(out: str, kinds: str | None) -> set[str]:
        res = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and (kinds is None or parts[1] in kinds):
                res.add(parts[0].split("@")[0])
        return res

    return names(d.stdout, "TtWiDdBbRrVv"), names(u.stdout, None) if u.returncode == 0 else set()
