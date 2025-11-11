"""
Pydantic schemas for all test artifacts (trace, config, meta).
This defines the structure for all generated YAML files, ensuring
that the test artifacts are valid and machine-readable.
"""

from typing import Any, TypeAlias

from pydantic import BaseModel, Field
from pydantic.types import constr

# --- Trace Model Schemas (from your example) ---

# Regex to match a context variable, e.g., "$context.states.initial"
CONTEXT_VAR_REGEX = r"^\$context\.\w+\.\w+$"
ContextVar: TypeAlias = constr(pattern=CONTEXT_VAR_REGEX)


class ContextObjectsModel(BaseModel):
    """
    Defines the SSZ objects (artifacts) to be loaded.
    """

    states: dict[str, str] = Field(
        default_factory=dict, description="Map of state names to SSZ filenames"
    )
    blocks: dict[str, str] = Field(
        default_factory=dict, description="Map of block names to SSZ filenames"
    )
    attestations: dict[str, str] = Field(
        default_factory=dict, description="Map of attestation names to SSZ filenames"
    )
    # Add other SSZ types here as needed


class ContextModel(BaseModel):
    """Defines the 'context' block of the trace."""

    fixtures: list[str] = Field(
        default_factory=list, description="List of non-SSZ fixtures to inject, e.g., 'store'"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Simple test setup parameters (e.g., validator_count)"
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
    """The root model for the trace.yaml file."""

    metadata: dict[str, Any] = Field(..., description="Test run metadata (fork, preset, etc.)")
    context: ContextModel
    trace: list[TraceStepModel]


# --- Config and Meta Model Schemas ---


class ConfigModel(BaseModel):
    """
    The root model for the config.yaml file.
    We use a simple key-value store.
    """

    config: dict[str, Any] = Field(..., description="A dictionary of config variables.")


class MetaModel(BaseModel):
    """
    The root model for the meta.yaml file.
    We use a simple key-value store.
    """

    meta: dict[str, Any] = Field(..., description="A dictionary of metadata key/value pairs.")
