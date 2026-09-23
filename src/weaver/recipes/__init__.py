"""Transformation recipes with explicit preconditions (pointer-tracker plan §4)."""

from weaver.recipes.base import Precondition, RecipeContext, RecipeResult  # noqa: F401
from weaver.recipes.local_alias import LocalAliasRecipe
from weaver.recipes.output_param import OutputParamRecipe
from weaver.recipes.scalar_input import ScalarInputRecipe

CATALOG = {r.id: r for r in [LocalAliasRecipe(), ScalarInputRecipe(), OutputParamRecipe()]}


def recipes_for_finding(finding: dict) -> list:
    return [r for r in CATALOG.values() if r.applicable(finding)]
