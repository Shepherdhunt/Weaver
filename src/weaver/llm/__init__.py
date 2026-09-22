"""LLM planner/explainer support.

The LLM explains and proposes; it never edits or validates.  It receives a
focused, source-linked evidence slice for one pointer (not a raw IR dump), can
request more evidence through read-only tools, and must mark every
precondition as established (with evidence) or unresolved.
"""
