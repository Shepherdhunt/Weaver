"""Shared evidence vocabulary.

Every fact Weaver stores carries provenance and an explicit status, so that an
absent artifact or an unsupported construct is recorded as *unknown* evidence
rather than silently turned into a "no pointer" or "no alias" conclusion
(compiler-artifact plan §§9-11).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class CapabilityStatus(str, Enum):
    """Status of a toolchain capability in one profile (compiler plan §2)."""

    DOCUMENTED = "documented"  # public documentation for the family; not probed here
    PROBE_PASSED = "probe-passed"  # a fixture ran and the artifact validated
    UNVERIFIED = "unverified"  # probe not run, or failed for reasons that may be configuration
    UNAVAILABLE = "unavailable-in-this-profile"  # the installed tool rejected the interface


class EvidenceStatus(str, Enum):
    """Fidelity of the frontend that produced source facts (compiler plan §9)."""

    NATIVE = "native"  # produced by the production compiler itself
    SECONDARY_CHECKED = "secondary-checked"  # secondary frontend; macro/include/layout checks passed
    SECONDARY_PARTIAL = "secondary-partial"  # secondary frontend; some checks differ or were unavailable
    # Extension of the plan's list: a secondary frontend whose fidelity checks
    # have not been run yet.  Kept distinct so "not checked" is never read as
    # "partially checked".
    SECONDARY_UNCHECKED = "secondary-unchecked"
    UNSUPPORTED = "unsupported"  # no adequate frontend for this unit/profile


class FactKind(str, Enum):
    """Where a fact comes from (pointer-tracker plan §3)."""

    COMPILER = "compiler-established"
    API_CONTRACT = "api-contract"
    RUNTIME_OBSERVATION = "runtime-observation"
    HYPOTHESIS = "hypothesis"


class ValidationKind(str, Enum):
    """What a validation record actually establishes (pointer-tracker plan §6)."""

    COMPILE = "compile"
    MECHANICAL_RECHECK = "mechanical-recheck"
    TEST = "testing"
    DIFFERENTIAL_TEST = "differential-testing"
    COVERAGE = "coverage"  # which changed lines the tests executed
    CONFIGURATION = "configuration"  # the validation build compiles what was analysed, as analysed
    SANITIZER = "sanitizer"
    BOUNDED_CHECK = "bounded-check"
    PROOF = "proof-under-assumptions"


class ValidationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_EVALUATED = "not-evaluated"  # could not be run: result stays provisional


class TxnState(str, Enum):
    """Transaction states (pointer-tracker plan §6)."""

    DISCOVERED = "discovered"
    ANALYZED = "analyzed"
    BLOCKED = "blocked"
    PROPOSED = "proposed"
    VALIDATED = "validated"
    PROVISIONAL = "provisional"  # validation ran but a required check could not be evaluated
    REJECTED = "rejected"  # validation failed; patch discarded
    ACCEPTED = "accepted"
    SKIPPED = "skipped"
    REVERTED = "reverted"


EVIDENCE_RANK = {
    EvidenceStatus.NATIVE: 4,
    EvidenceStatus.SECONDARY_CHECKED: 3,
    EvidenceStatus.SECONDARY_PARTIAL: 2,
    EvidenceStatus.SECONDARY_UNCHECKED: 1,
    EvidenceStatus.UNSUPPORTED: 0,
}


def weakest(statuses: list[EvidenceStatus]) -> EvidenceStatus:
    if not statuses:
        return EvidenceStatus.UNSUPPORTED
    return min(statuses, key=lambda s: EVIDENCE_RANK[s])


@dataclass
class ToolRef:
    """Identity of the tool that produced an artifact."""

    path: str
    sha256: str | None
    family: str
    version: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Provenance:
    """Where a fact came from.  Attached to inventory findings and graph edges."""

    profile: str
    unit: str
    file: str
    file_sha256: str | None
    artifact: str | None
    producer: ToolRef | None
    evidence_status: EvidenceStatus
    fact_kind: FactKind = FactKind.COMPILER
    assumptions: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence_status"] = self.evidence_status.value
        d["fact_kind"] = self.fact_kind.value
        return d
