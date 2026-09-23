"""Flow evidence: external effect models, SVF points-to jobs, and may-modify queries.

Artifact plan §12: SVF runs as a separate, pinned analysis job over
target-correct LLVM IR; an adapter exports source-linked evidence in Weaver's
own schema; recipes combine it with AST facts.  An incomplete or unavailable
analysis never authorises a rewrite through an apparent absence of aliases.
"""
