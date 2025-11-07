"""
This file defines the Pydantic models for the trace.yaml structure.
This ensures all generated traces are valid and provides a clear
schema for any future "playback" or analysis tools.

This model is aligned with the structure proposed in:
https://github.com/ethereum/consensus-specs/issues/4603
"""

from typing import Any, constr, TypeAlias

from pydantic import BaseModel, Field

# Regex to match a context variable, e.g., "$context.states.initial"
CONTEXT_VAR_REGEX = r"^\$context\.\w+\.\w+$"
ContextVar: TypeAlias = constr(pattern=CONTEXT_VAR_REGEX)
SimpleValue: TypeAlias = str | int | bool | None


class ContextObjectsModel(BaseModel):
    """
    Defines the SSZ objects (artifacts) to be loaded.
    This corresponds to the `context.objects` block in the YAML.
    """

    states: dict[str, str] = Field(
        default_factory=dict, description="Map of state names to SSZ filenames"
    )
    blocks: dict[str, str] = Field(
        default_factory=dict, description="Map of block names to SSZ filenames"
    )
    # --- FIXED: Added attestations ---
    attestations: dict[str, str] = Field(
        default_factory=dict, description="Map of attestation names to SSZ filenames"
    )
    # ---------------------------------


class ContextModel(BaseModel):
    """Defines the 'context' block of the trace."""

    fixtures: list[str] = Field(
        default_factory=list, description="List of non-SSZ fixtures to inject, e.g., 'store'"
    )
    objects: ContextObjectsModel = Field(default_factory=ContextObjectsModel)


class TraceStepModel(BaseModel):
    """Defines a single step in the 'trace' list."""

    op: str = Field(..., description="The spec function operation to call, e.g., 'tick'")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Parameters to pass to the operation"
    )
    result: Any | None = Field(
        None, description="The expected result, often a new context var or None"
    )


class TraceModel(BaseModel):
    """The root model for the entire trace.yaml file."""

    context: ContextModel
    trace: list[TraceStepModel]
