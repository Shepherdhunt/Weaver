"""Transformation recipes with explicit preconditions (pointer-tracker plan §4)."""

from weaver.recipes.base import Precondition, RecipeContext, RecipeResult  # noqa: F401
from weaver.recipes.local_alias import LocalAliasRecipe

CATALOG = {r.id: r for r in [LocalAliasRecipe()]}


def recipes_for_finding(finding: dict) -> list:
    return [r for r in CATALOG.values() if r.applicable(finding)]
