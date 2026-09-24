"""Hand-written and AI-drafted patches, checked like recipe output.

Most de-pointering work is manual: a recipe can prove only a narrow class of
changes safe.  A patch — a unified diff against the analysed tree, written by an
engineer or drafted by an AI model — becomes a transaction like any recipe's.
Its edits are bound to the hashes of the files they change, so validation,
acceptance, the checkpoint and revert work unchanged.

What differs is the mechanical re-check.  A recipe states post-conditions of its
own; a patch states only which pointers it means to remove.  Validation
therefore analyses every affected unit twice, from the baseline and from the
patched workspace, and compares their pointer facts with the rules of change
impact (``weaver.impact.finding_changes``): pointers removed and added, uses
that appeared (a read-only pointer now written, a value that now escapes), types
and access classes that changed.  It fails when a named pointer still exists or
a pinned or implied contract no longer holds, and lists every other change for
review.  It never claims that behaviour is preserved: the configured tests and
differential runs say what they can, as for a recipe.

Diffs are applied by content: each hunk's context and removed lines must appear
in the file, at the stated line or, failing that, at the nearest place where they
match (trailing whitespace ignored).  A hunk that matches nowhere refuses the
whole patch.  Files are created or deleted outside Weaver.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weaver.config import Project
from weaver.errors import WeaverError
from weaver.recipes.base import ESTABLISHED, UNRESOLVED, VIOLATED, Precondition
from weaver.rewrite import Edit
from weaver.util import is_within, sha256_bytes

RECIPE = "patch"
VERSION = "1"


@dataclass
class Hunk:
    old_start: int
    new_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (' ' | '-' | '+', text without newline)

    @property
    def old(self) -> list[str]:
        return [t for k, t in self.lines if k in " -"]

    @property
    def new(self) -> list[str]:
        return [t for k, t in self.lines if k in " +"]


@dataclass
class FilePatch:
    path: str
    hunks: list[Hunk] = field(default_factory=list)


_HUNK = re.compile(r"^@@+ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def as_bytes_text(text: str | bytes) -> str:
    """Patches and sources are compared as latin-1 text, one character per byte (as the rewriter does)."""
    if isinstance(text, bytes):
        return text.decode("latin-1")
    return text.encode("utf-8", "surrogateescape").decode("latin-1")


def _path(p: str) -> str:
    p = p.split("\t")[0].strip()
    return p[2:] if p.startswith(("a/", "b/")) else p


def parse_diff(text: str) -> list[FilePatch]:
    """Unified diffs (``diff -u``, ``git diff``).  Hunk line counts are not trusted: a hunk runs until the next
    header or the first line that is not context, removal or addition, since hand edits and models get them
    wrong."""
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = line[4:].split("\t")[0].strip(), lines[i + 1][4:].split("\t")[0].strip()
            if new == "/dev/null":
                raise WeaverError(f"the patch deletes {_path(old)}; delete files outside Weaver")
            if old == "/dev/null":
                raise WeaverError(f"the patch creates {_path(new)}; create new files first, then analyse and patch")
            cur, hunk = FilePatch(_path(new)), None
            files.append(cur)
            i += 2
            continue
        m = _HUNK.match(line)
        if m:
            if cur is None:
                raise WeaverError(f"line {i + 1}: a hunk before any '--- / +++' file header")
            hunk = Hunk(int(m.group(1)), int(m.group(2)))
            cur.hunks.append(hunk)
            i += 1
            continue
        if hunk is not None:
            if line.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            if line == "" or line[0] in " -+":
                hunk.lines.append((line[:1] or " ", line[1:]))
                i += 1
                continue
            hunk = None  # prose or another header ends the hunk
        i += 1
    for fp in files:
        for h in fp.hunks:
            while h.lines and h.lines[-1] == (" ", ""):  # blank lines after a hunk are not context
                h.lines.pop()
    files = [fp for fp in files if fp.hunks]
    if not files:
        raise WeaverError("no unified diff found: expected '--- a/file', '+++ b/file' and '@@ ... @@' hunks")
    return files


def _find(body: list[str], old: list[str], want: int, floor: int) -> int | None:
    if not old:
        return min(max(want, floor), len(body))
    n = len(old)
    if floor <= want <= len(body) - n and body[want : want + n] == old:
        return want
    for norm in (lambda s: s, str.rstrip):
        o = [norm(x) for x in old]
        hits = [i for i in range(floor, len(body) - n + 1) if [norm(x) for x in body[i : i + n]] == o]
        if hits:
            return min(hits, key=lambda i: (abs(i - want), i))
    return None


def apply_file(text: str, fp: FilePatch) -> tuple[str, list[str]]:
    """Apply one file's hunks in order; (new text, notes on hunks placed away from their stated line)."""
    rows = text.splitlines(keepends=True)
    body = [r.rstrip("\r\n") for r in rows]
    ends = [r[len(b) :] for r, b in zip(rows, body)]
    eol = "\r\n" if ends and ends[0] == "\r\n" else "\n"
    notes: list[str] = []
    delta, floor = 0, 0
    for n, h in enumerate(fp.hunks, 1):
        old, new = h.old, h.new
        want = max(h.old_start - 1 + delta, 0) if h.old_start else 0
        at = _find(body, old, want, floor)
        if at is None:
            first = next((t for k, t in h.lines if k in " -"), "")
            raise WeaverError(
                f"{fp.path}: hunk {n} (stated at line {h.old_start}) does not match the file: its context and "
                f"removed lines, starting {first.strip()[:60]!r}, appear nowhere after the previous hunk"
            )
        if at != want and old:
            notes.append(f"{fp.path}: hunk {n} applied at line {at + 1}, not {want + 1} as stated")
        # context lines keep the file's own text (a hunk may have matched with trailing blanks ignored)
        new, k = [], at
        for tag, t in h.lines:
            if tag == " ":
                new.append(body[k])
            if tag in " -":
                k += 1
            if tag == "+":
                new.append(t)
        last = at + len(old) == len(body) and bool(ends) and ends[-1] == ""
        new_ends = [eol] * len(new)
        if last and new_ends:
            new_ends[-1] = ""  # the file had no newline at its end, and the hunk reaches it
        body[at : at + len(old)] = new
        ends[at : at + len(old)] = new_ends
        delta += len(new) - len(old)
        floor = at + len(new)
    return "".join(b + e for b, e in zip(body, ends)), notes


def edits_from_texts(rel: str, old: str, new: str, reason: str) -> list[Edit]:
    """Line-level edits (byte offsets) that turn ``old`` into ``new``."""
    a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
    offs = [0]
    for line in a:
        offs.append(offs[-1] + len(line))
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        s, e = offs[i1], offs[i2]
        out.append(Edit(rel, s, e, old[s:e], "".join(b[j1:j2]), f"{reason}, line {i1 + 1}"))
    return out


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


def origin_label(origin: dict[str, Any] | None) -> str:
    o = origin or {}
    if o.get("kind") == "ai":
        return f"an AI draft ({o.get('provider')}/{o.get('model')})"
    return "a hand-written change"


def candidate_from_patch(
    project: Project,
    inv: dict[str, Any],
    diff_text: str | bytes,
    removes: list[str],
    title: str = "",
    origin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A transaction candidate with the shape of a recipe's (``RecipeResult.to_json``)."""
    from weaver.analysis.inventory import find_finding

    P = {
        "applies": Precondition("PATCH.applies", "The patch applies to the current source of every file it names"),
        "current": Precondition("PATCH.evidence-current", "Every file it edits matches the analysed revision"),
        "analysed": Precondition(
            "PATCH.analysed",
            "An analysed configuration compiles or includes every edited file, so its pointer facts can be re-checked",
        ),
        "targets": Precondition(
            "PATCH.targets", "Every pointer it claims to remove is a current finding declared in an edited file"
        ),
    }
    edits: list[Edit] = []
    hashes: dict[str, str] = {}
    notes: list[str] = []
    first_line: dict[str, int] = {}
    root = project.root.resolve()
    try:
        files = parse_diff(as_bytes_text(diff_text))
    except WeaverError as e:
        P["applies"].fail(VIOLATED, str(e))
        files = []
    covered = set((inv.get("coverage") or {}).get("files", {})) | set(inv.get("files", {}))
    hunks = 0
    for fp in files:
        rel = fp.path
        p = (root / rel).resolve()
        if Path(rel).is_absolute() or not is_within(p, root):
            P["applies"].fail(VIOLATED, f"{rel} is not a path inside the project")
            continue
        rel = str(p.relative_to(root))
        if not p.is_file():
            P["applies"].fail(VIOLATED, f"{rel} does not exist in the project")
            continue
        data = p.read_bytes()
        text = data.decode("latin-1")
        try:
            new, placed = apply_file(text, fp)
        except WeaverError as e:
            P["applies"].fail(VIOLATED, str(e))
            continue
        notes += placed
        hunks += len(fp.hunks)
        if new == text:
            notes.append(f"{rel}: the patch leaves it unchanged")
            continue
        hashes[rel] = sha256_bytes(data)
        es = edits_from_texts(rel, text, new, "patch")
        edits += es
        first_line[rel] = text.count("\n", 0, es[0].start) + 1
        if inv["files"].get(rel) not in (None, hashes[rel]):
            P["current"].fail(VIOLATED, f"{rel} changed since the inventory was built", "re-run 'weaver refresh'")
        if rel not in covered:
            P["analysed"].fail(
                UNRESOLVED,
                f"no analysed configuration compiles or includes {rel}; its change cannot be checked",
                "add a configuration that builds it, or change it outside Weaver",
            )
    if files and not edits and P["applies"].status == ESTABLISHED:
        P["applies"].fail(VIOLATED, "the patch changes nothing")
    if P["applies"].status == ESTABLISHED and edits:
        P["applies"].ok(f"{hunks} hunk(s) change {len(hashes)} file(s): {len(edits)} edit(s)")
    if P["current"].status == ESTABLISHED and hashes:
        P["current"].ok(f"{len(hashes)} edited file(s) match the analysed revision")
    if P["analysed"].status == ESTABLISHED and hashes:
        P["analysed"].ok("every edited file is compiled or included by an analysed configuration")

    targets: list[dict[str, Any]] = []
    for t in removes:
        try:
            f = find_finding(inv, t)
        except WeaverError:
            P["targets"].fail(VIOLATED, f"{t} is not a pointer finding in the current inventory")
            continue
        targets.append(f)
        if hashes and f.get("file") not in hashes:
            P["targets"].fail(
                VIOLATED,
                f"{f['id']} '{f.get('name')}' is declared in {f.get('file')}, which the patch does not edit",
            )
    if P["targets"].status == ESTABLISHED:
        P["targets"].ok(
            "removes " + ", ".join(f"{f['id']} '{f.get('name')}'" for f in targets)
            if targets
            else "no pointer named: the re-check reports every change to pointer facts without a target"
        )

    who = origin_label(origin)
    names = [f"'{f.get('name')}'" for f in targets]
    return {
        "recipe": RECIPE,
        "recipe_version": VERSION,
        "finding_id": targets[0]["id"] if targets else "",
        "eligible": all(p.status == ESTABLISHED for p in P.values()) and bool(edits),
        "preconditions": [p.to_json() for p in P.values()],
        "edits": [e.to_json() for e in edits],
        "file_hashes": hashes,
        "capabilities_required": [],
        "preservation_argument": (
            f"This is {who}; Weaver does not derive why it preserves behaviour. It checks what it can: every "
            "configuration that compiles an edited file still compiles, with no new diagnostics; the pointer "
            "facts of every affected unit, analysed before and after, differ only as the validation lists them "
            f"(pointers removed, added or changed{'; ' + ', '.join(names) + ' must be gone' if names else ''}); "
            "pinned and implied contracts still hold; and the configured tests and differential runs give the "
            "same results on both trees."
        ),
        "validation_plan": [
            "compile every unit that includes an edited file, in every profile, with its production command",
            "pointer-fact re-check: analyse every affected unit before and after the patch and compare"
            + (f"; {', '.join(names)} must be gone" if names else ""),
            "check pinned and implied contracts on the patched facts",
            "run the configured tests and differential comparisons against the unpatched baseline",
        ],
        "affected": {
            "objects": [f.get("name") for f in targets],
            "files": sorted(hashes),
            "functions": sorted({f["function"] for f in targets if f.get("function")}),
            "interfaces": [],
        },
        "units": [],
        "notes": ([f"title: {title}"] if title else []) + [f"origin: {who}"] + notes,
        "recheck": {"targets": [f["id"] for f in targets], "names": {f["id"]: f.get("name") for f in targets}},
        "title": title,
        "origin": origin or {"kind": "manual"},
        "first_line": first_line,
    }


# ---------------------------------------------------------------------------
# Re-check: pointer facts before and after
# ---------------------------------------------------------------------------


class FactCheck:
    """Collects each affected unit's facts before and after the patch, and judges them."""

    def __init__(self, cand: dict[str, Any]):
        self.targets: list[str] = list((cand.get("recheck") or {}).get("targets", []))
        self.names: dict[str, str] = dict((cand.get("recheck") or {}).get("names", {}))
        self.changes: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.seen_before: dict[str, list[str]] = {}
        self.seen_after: dict[str, list[str]] = {}
        self.after: dict[str, dict[str, Any]] = {}  # merged patched findings, for contracts
        self.decls: list[dict[str, Any]] = []
        self.units = 0

    def add_unit(
        self, name: str, before: dict[str, Any], after: dict[str, Any], root: Path, hunks: dict[str, Any]
    ) -> None:
        from weaver.impact import finding_changes

        self.units += 1
        bf = {f["id"]: f for f in before["findings"]}
        cf = {f["id"]: f for f in after["findings"]}
        for fid in bf:
            self.seen_before.setdefault(fid, []).append(name)
        for fid, f in cf.items():
            self.seen_after.setdefault(fid, []).append(name)
            if fid in self.after:
                known = {(u["kind"], u.get("line"), u.get("col")) for u in self.after[fid].get("uses", [])}
                self.after[fid]["uses"] += [
                    u for u in f.get("uses", []) if (u["kind"], u.get("line"), u.get("col")) not in known
                ]
            else:
                self.after[fid] = {**f, "uses": list(f.get("uses", []))}
        self.decls += after.get("function_decls", [])
        for c in finding_changes(bf, cf, root, hunks):
            key = (c["finding"], c["aspect"], c["text"])
            if key not in self.changes:
                self.changes[key] = {**c, "units": [name]}
            elif name not in self.changes[key]["units"]:
                self.changes[key]["units"].append(name)

    def record(self, project: Project) -> dict[str, Any]:
        """One mechanical re-check record for the whole patch."""
        from weaver.evidence import ValidationKind, ValidationOutcome
        from weaver.impact import HIGH, REVIEW, check_contracts

        problems: list[str] = []
        targets = []
        for t in self.targets:
            label = f"{t} '{self.names.get(t, '?')}'"
            if t not in self.seen_before:
                problems.append(f"{label} is in no unit the patch affects")
                status = "not-seen"
            elif t in self.seen_after:
                where = ", ".join(self.seen_after[t][:4])
                problems.append(f"{label} still exists after the patch ({where})")
                status = "present"
            else:
                status = "removed"
            targets.append({"finding": t, "name": self.names.get(t), "status": status})
        inv_like = {"findings": list(self.after.values()), "function_decls": self.decls}
        contracts = check_contracts(project, inv_like, subjects=set(self.seen_before), whole_program=False)
        for c in contracts:
            if c["status"] == "violated":
                problems.append(f"contract {c['contract']} no longer holds: {c['text']}")
        changes = sorted(self.changes.values(), key=lambda c: (-_rank(c["severity"]), c["file"] or "", c["text"]))
        removed = [c for c in changes if c["aspect"] == "removed"]
        added = [c for c in changes if c["aspect"] == "added"]
        review = [c for c in changes if c["severity"] in (HIGH, REVIEW) and c["aspect"] != "added"]
        review += [c for c in added if c["severity"] in (HIGH, REVIEW)]
        review += [
            {"severity": REVIEW, "aspect": "contract", "text": c["text"], "finding": None, "file": None}
            for c in contracts
            if c["status"] in ("unknown", "subject-gone")
        ]
        summary = (
            f"{self.units} unit(s) analysed before and after: {len(removed)} pointer(s) removed, "
            f"{len(added)} added, {len(review)} change(s) to review"
        )
        if removed:
            summary += "; removed: " + ", ".join(f"'{c['name']}' ({c['function'] or c['file']})" for c in removed[:6])
        detail = "; ".join(problems) + "; " + summary if problems else summary
        compact = ("severity", "finding", "name", "kind", "function", "file", "aspect", "text", "line", "units")
        return {
            "kind": ValidationKind.MECHANICAL_RECHECK.value,
            "name": "pointer facts",
            "outcome": (ValidationOutcome.FAILED if problems else ValidationOutcome.PASSED).value,
            "detail": detail,
            "facts": {
                "targets": targets,
                "removed": [{k: c.get(k) for k in compact} for c in removed],
                "added": [{k: c.get(k) for k in compact} for c in added],
                "review": [{k: c.get(k) for k in compact} for c in review],
                "other": sum(1 for c in changes if c not in removed and c not in added and c not in review),
                "contracts": [c for c in contracts if c["status"] != "held"],
                "units": self.units,
            },
        }


def _rank(sev: str | None) -> int:
    from weaver.impact import RANK

    return RANK.get(sev, 0)


def review_items(txn: dict[str, Any]) -> list[dict[str, Any]]:
    """The changes a reviewer must look at before accepting a validated patch."""
    for r in (txn.get("validation") or {}).get("records", []):
        if r.get("name") == "pointer facts":
            return (r.get("facts") or {}).get("review", [])
    return []
