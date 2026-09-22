"""Weaver: compiler-assisted, evidence-driven incremental C pointer migration.

The package is organised around the two planning documents in the repository
root (``pointer-tracker-plan.md`` and ``compiler-artifact-plan.md``):

* ``weaver.capture``   – build capture (compile databases, response files,
  compiler wrappers, tool identity).
* ``weaver.toolchain`` – production-compiler adapters, collection recipes and
  capability probes.
* ``weaver.frontend``  – readers for collected artifacts (Clang JSON AST,
  preprocessed output, a raw C lexer and a C type-string parser).
* ``weaver.analysis``  – the source-linked pointer inventory, configuration
  coverage and the normalized evidence graph.
* ``weaver.fidelity``  – secondary-frontend compatibility checks.
* ``weaver.recipes``   – transformation recipes with explicit preconditions.
* ``weaver.rewrite``, ``weaver.validate``, ``weaver.ledger`` – deterministic
  editing, independent validation and the transaction ledger.
* ``weaver.llm``       – focused evidence slices and the planner instruction.
"""

__version__ = "0.1.0"

#: Version of the on-disk evidence schemas written by this package.  Bump when
#: an incompatible change is made so that resumed migrations can detect it.
SCHEMA_VERSION = 1
