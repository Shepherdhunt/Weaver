"""Recipe interface and shared evaluation context.

A recipe has an identifier, applicability checks, target-capability
requirements, edit rules, a preservation argument, a targeted validation plan
and explicit rejection reasons (pointer-tracker plan §4).  Every precondition
is reported as ``established`` (with evidence), ``violated`` (with the
counter-evidence) or ``unresolved`` (with what would resolve it).  A candidate
is eligible only when every precondition is established.
"""

from __future__ import annotations

import collections
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weaver.config import Project
from weaver.evidence import EVIDENCE_RANK, EvidenceStatus
from weaver.frontend.clang_ast import TranslationUnit, load_unit_ast
from weaver.frontend.lexer import LexResult, lex
from weaver.rewrite import Edit
from weaver.store import Store
from weaver.toolchain.collect import MANIFEST, read_macros
from weaver.util import read_json, sha256_file

ESTABLISHED = "established"
VIOLATED = "violated"
UNRESOLVED = "unresolved"


@dataclass
class Precondition:
    id: str
    description: str
    status: str = ESTABLISHED
    evidence: list[str] = field(default_factory=list)
    resolve_by: str | None = None  # for unresolved: the analysis or decision that would resolve it

    def fail(self, status: str, msg: str, resolve_by: str | None = None) -> None:
        # violated dominates unresolved
        if self.status != VIOLATED:
            self.status = status
        self.evidence.append(msg)
        if resolve_by and not self.resolve_by:
            self.resolve_by = resolve_by

    def ok(self, msg: str) -> None:
        self.evidence.append(msg)

    def to_json(self) -> dict[str, Any]:
        d = {"id": self.id, "description": self.description, "status": self.status, "evidence": self.evidence}
        if self.resolve_by:
            d["resolve_by"] = self.resolve_by
        return d


@dataclass
class RecipeResult:
    recipe: str
    recipe_version: str
    finding_id: str
    preconditions: list[Precondition]
    edits: list[Edit]
    file_hashes: dict[str, str]
    capabilities_required: list[str]
    preservation_argument: str
    validation_plan: list[str]
    affected: dict[str, Any]
    units: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    recheck: dict[str, Any] = field(default_factory=dict)  # data for the mechanical re-check

    @property
    def eligible(self) -> bool:
        return all(p.status == ESTABLISHED for p in self.preconditions) and bool(self.edits)

    @property
    def blockers(self) -> list[dict[str, Any]]:
        return [p.to_json() for p in self.preconditions if p.status != ESTABLISHED]

    def to_json(self) -> dict[str, Any]:
        return {
            "recipe": self.recipe,
            "recipe_version": self.recipe_version,
            "finding_id": self.finding_id,
            "eligible": self.eligible,
            "preconditions": [p.to_json() for p in self.preconditions],
            "edits": [e.to_json() for e in self.edits],
            "file_hashes": self.file_hashes,
            "capabilities_required": self.capabilities_required,
            "preservation_argument": self.preservation_argument,
            "validation_plan": self.validation_plan,
            "affected": self.affected,
            "units": self.units,
            "notes": self.notes,
            "recheck": self.recheck,
        }


class RecipeContext:
    """Lazy access to evidence for recipe evaluation (TUs, lexers, macros)."""

    def __init__(self, project: Project, inventory: dict[str, Any]):
        self.project = project
        self.inventory = inventory
        self.store = Store(project.state_dir)
        # Parsed ASTs, least recently used first.  Bounded: a whole-program evaluation visits findings
        # file by file, and keeping every unit's AST alive costs gigabytes on projects the size of cFS.
        self._tus: collections.OrderedDict[str, TranslationUnit | None] = collections.OrderedDict()
        self._lex: collections.OrderedDict[str, LexResult] = collections.OrderedDict()
        self._manifests: dict[str, dict[str, Any]] = {}
        self._macros: dict[str, dict[str, str]] = {}
        self.min_evidence = EvidenceStatus(project.acceptance.min_evidence)

    @property
    def root(self) -> Path:
        return self.project.root

    def abs(self, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.realpath(os.path.join(self.root, rel))

    def units_for_file(self, rel: str) -> list[dict[str, Any]]:
        return [u for u in self.inventory["units"] if u["file"] == rel]

    def manifest(self, unit: dict[str, Any]) -> dict[str, Any]:
        uid = unit["unit_id"]
        if uid not in self._manifests:
            self._manifests[uid] = read_json(self.store.unit_dir(unit["profile"], uid) / MANIFEST)
        return self._manifests[uid]

    TU_CACHE = 48

    def tu(self, unit: dict[str, Any]) -> TranslationUnit | None:
        uid = unit["unit_id"]
        if uid in self._tus:
            self._tus.move_to_end(uid)
            return self._tus[uid]
        m = self.manifest(unit)
        tu = self._tus[uid] = load_unit_ast(m, self.store.unit_dir(unit["profile"], uid), str(self.root))
        while len(self._tus) > self.TU_CACHE:
            self._tus.popitem(last=False)
        return tu

    def macros(self, unit: dict[str, Any]) -> dict[str, str]:
        uid = unit["unit_id"]
        if uid not in self._macros:
            d = self.store.unit_dir(unit["profile"], uid)
            m = self.manifest(unit)
            name = (
                "secondary.macros.txt" if (m.get("ast_artifact") or "").startswith("secondary.") else "unit.macros.txt"
            )
            self._macros[uid] = read_macros(d / name)
        return self._macros[uid]

    LEX_CACHE = 64

    def lexed(self, rel: str) -> LexResult:
        if rel in self._lex:
            self._lex.move_to_end(rel)
            return self._lex[rel]
        lx = self._lex[rel] = lex(Path(self.abs(rel)).read_bytes())
        while len(self._lex) > self.LEX_CACHE:
            self._lex.popitem(last=False)
        return lx

    def ident_occurrences(self, name: str, files: list[str]) -> list[tuple[str, int]]:
        """(file, token index) of every identifier token ``name`` in ``files``.

        Which files mention a name comes from a compact, disk-cached index; only those files are
        lexed in full (see ``weaver.analysis.identindex``).
        """
        if not hasattr(self, "_idents"):
            from weaver.analysis.identindex import IdentIndex

            self._idents = IdentIndex(self.root, self.store.root / "analysis" / "idents.json")
        out: list[tuple[str, int]] = []
        for rel in self._idents.files_with(name, files):
            out.extend((rel, i) for i, t in enumerate(self.lexed(rel).tokens) if t.kind == "ident" and t.text == name)
        return out

    def current_hash(self, rel: str) -> str:
        return sha256_file(self.abs(rel))

    def evidence_ok(self, status: str) -> bool:
        return EVIDENCE_RANK[EvidenceStatus(status)] >= EVIDENCE_RANK[self.min_evidence]

    # -- whole-program views ------------------------------------------------
    @property
    def program(self) -> Any:
        if not hasattr(self, "_program"):
            from weaver.flow.models import load_models
            from weaver.flow.program import Program

            unit_programs: dict[str, set[str]] = {}
            for u in self.inventory["units"]:
                try:
                    progs = self.link(u["profile"]).programs_of_unit(u["unit_id"])
                except Exception:  # noqa: BLE001 - no link model: resolve by name only
                    progs = []
                unit_programs[u["unit_id"]] = {f"{u['profile']}/{p.name}" for p in progs}
            self._program = Program(self.inventory, load_models(self.project), unit_programs)
        return self._program

    def link(self, profile_id: str) -> Any:
        """The profile's link model (images, programs, exports); see ``weaver.link``."""
        if not hasattr(self, "_links"):
            self._links: dict[str, Any] = {}
        if profile_id not in self._links:
            from weaver.link import link_model

            self._links[profile_id] = link_model(self.project, self.project.profile(profile_id))
        return self._links[profile_id]

    def flows(self, profile_id: str) -> dict[str, Any]:
        """Program name -> current flow evidence (None when absent or stale) for one profile."""
        if not hasattr(self, "_flows"):
            self._flows: dict[str, Any] = {}
        if profile_id not in self._flows:
            from weaver.flow.evidence import load_flows

            self._flows[profile_id] = load_flows(self.project, profile_id, self.inventory)
        return self._flows[profile_id]

    def gcc(self, profile_id: str, program: str) -> dict[str, Any]:
        """``program/image`` -> current GCC points-to solution for one program (see ``weaver.flow.gcc_pta``)."""
        if not hasattr(self, "_gcc"):
            self._gcc: dict[tuple[str, str], dict[str, Any]] = {}
        key = (profile_id, program)
        if key not in self._gcc:
            from weaver.flow.gcc_pta import load_gcc_pta

            self._gcc[key] = (
                load_gcc_pta(self.project, profile_id, program, self.inventory) if self.project.flow.uses("gcc") else {}
            )
        return self._gcc[key]

    def tasks(self, profile_id: str, program: str) -> Any:
        """The task model of one program (``preservation.concurrency: {model: tasks}``; see weaver.flow.tasks)."""
        if not hasattr(self, "_tasks"):
            self._tasks: dict[tuple[str, str], Any] = {}
        key = (profile_id, program)
        if key not in self._tasks:
            from weaver.flow.svf import _safe
            from weaver.flow.tasks import TaskModel, spec_of

            lm = self.link(profile_id)
            prog = lm.program(program)
            self._tasks[key] = TaskModel(
                self.program,
                self.flows(profile_id).get(_safe(program)),
                f"{profile_id}/{program}",
                spec_of(self.project.preservation) or {},
                prog.entry_points if prog is not None else [],
            )
        return self._tasks[key]

    def programs_for_units(self, units: list[str]) -> dict[str, list[str]]:
        """``profile/program`` keys of the programs that link any of ``units``, with their unit ids."""
        out: dict[str, list[str]] = {}
        by_id = {u["unit_id"]: u for u in self.inventory["units"]}
        for uid in units:
            u = by_id.get(uid)
            if u is None:
                continue
            for prog in self.link(u["profile"]).programs_of_unit(uid):
                out.setdefault(f"{u['profile']}/{prog.name}", prog.units)
        return out

    def flows_for_units(self, units: list[str]) -> dict[str, Any]:
        """Flow evidence of every program that links any of ``units`` (None where missing)."""
        from weaver.flow.svf import _safe

        out: dict[str, Any] = {}
        for key in self.programs_for_units(units):
            profile, _, name = key.partition("/")
            flows = self.flows(profile)
            out[key] = flows.get(_safe(name))
        return out

    def whole_program(self, units: list[str] | None = None) -> list[tuple[str, str]]:
        """Problems preventing a whole-program claim: (status, message) pairs.

        With ``units``, only the programs that link those units must be fully
        analysed; otherwise every unit of every profile.
        """
        from weaver.capture.compdb import load_compdb
        from weaver.errors import WeaverError

        out: list[tuple[str, str]] = []
        analysed = {(u["profile"], u["unit_id"]): u for u in self.inventory["units"]}
        scope: set[str] | None = None
        if units is not None:
            progs = self.programs_for_units(units)
            if progs:
                scope = {u for us in progs.values() for u in us}
        for p in self.project.profiles:
            try:
                cmds = load_compdb(p.compile_commands)
            except WeaverError as e:
                out.append((UNRESOLVED, f"profile {p.id}: {e}"))
                continue
            for c in cmds:
                if scope is not None and c.unit_id(p.id) not in scope:
                    continue
                u = analysed.get((p.id, c.unit_id(p.id)))
                rel = os.path.relpath(c.file, self.root)
                if u is None:
                    out.append((UNRESOLVED, f"profile {p.id}: {rel} was not analysed"))
                elif not u["analyzed"]:
                    out.append((UNRESOLVED, f"profile {p.id}: {rel} has no AST evidence"))
                elif not self.evidence_ok(u["evidence_status"]):
                    out.append((UNRESOLVED, f"profile {p.id}: {rel} evidence is {u['evidence_status']}"))
        return out

    def profiles_compiling(self, rel: str) -> dict[str, bool]:
        """Profile id -> whether its compile database lists ``rel`` (None-safe)."""
        from weaver.capture.compdb import load_compdb
        from weaver.errors import WeaverError

        out: dict[str, bool] = {}
        target = self.abs(rel)
        for p in self.project.profiles:
            try:
                cmds = load_compdb(p.compile_commands)
            except WeaverError:
                out[p.id] = False
                continue
            out[p.id] = any(os.path.realpath(c.file) == target for c in cmds)
        return out


class Recipe:
    id = "abstract"
    version = "0"
    title = ""
    finding_kinds: tuple[str, ...] = ()

    def applicable(self, finding: dict[str, Any]) -> bool:
        return finding.get("kind") in self.finding_kinds

    def evaluate(self, ctx: RecipeContext, finding: dict[str, Any]) -> RecipeResult:  # pragma: no cover
        raise NotImplementedError

    def recheck(  # pragma: no cover
        self, result: dict[str, Any], tu: TranslationUnit, offset_maps: dict[str, Any], root: str
    ) -> list[str] | None:
        """Mechanically re-check a patched unit.

        ``offset_maps`` maps each edited project-relative file to its OffsetMap.
        Returns problems (empty list = passed) or None when the unit is not
        relevant to this transaction.
        """
        raise NotImplementedError
