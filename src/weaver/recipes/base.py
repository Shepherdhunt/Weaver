"""Recipe interface and shared evaluation context.

A recipe has an identifier, applicability checks, target-capability
requirements, edit rules, a preservation argument, a targeted validation plan
and explicit rejection reasons (pointer-tracker plan §4).  Every precondition
is reported as ``established`` (with evidence), ``violated`` (with the
counter-evidence) or ``unresolved`` (with what would resolve it).  A candidate
is eligible only when every precondition is established.
"""

from __future__ import annotations

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
        self._tus: dict[str, TranslationUnit | None] = {}
        self._lex: dict[str, LexResult] = {}
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

    def tu(self, unit: dict[str, Any]) -> TranslationUnit | None:
        uid = unit["unit_id"]
        if uid not in self._tus:
            m = self.manifest(unit)
            self._tus[uid] = load_unit_ast(m, self.store.unit_dir(unit["profile"], uid), str(self.root))
        return self._tus[uid]

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

    def lexed(self, rel: str) -> LexResult:
        if rel not in self._lex:
            self._lex[rel] = lex(Path(self.abs(rel)).read_bytes())
        return self._lex[rel]

    def current_hash(self, rel: str) -> str:
        return sha256_file(self.abs(rel))

    def evidence_ok(self, status: str) -> bool:
        return EVIDENCE_RANK[EvidenceStatus(status)] >= EVIDENCE_RANK[self.min_evidence]

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
        self, result: dict[str, Any], tu: TranslationUnit, offset_map: Any, root: str
    ) -> list[str]:
        """Mechanically re-check a patched unit; return a list of problems (empty = passed)."""
        raise NotImplementedError
