"""
Trace Models
------------
Pydantic models defining the schema for the generated test vector artifacts:
- trace.yaml: The sequence of operations.
- config.yaml: The system configuration.
- meta.yaml: Test metadata.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic.types import constr

# Regex to match a context variable reference, e.g., "$context.states.initial"
CONTEXT_VAR_REGEX = r"^\$context\.\w+\.\w+$"
ContextVar = constr(pattern=CONTEXT_VAR_REGEX)


class ContextObjectsModel(BaseModel):
    """
    Defines the SSZ objects (artifacts) loaded in the 'context' block.
    Maps logical names (e.g., 'v0') to filenames (e.g., 'state_root.ssz').
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


class ContextModel(BaseModel):
    """
    The 'context' block of the trace file.
    Contains static fixtures, parameters, and references to binary objects.
    """

    fixtures: list[str] = Field(
        default_factory=list, description="List of non-SSZ fixtures to inject (e.g. 'store')"
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Simple test setup parameters (e.g. validator_count)"
    )
    objects: ContextObjectsModel = Field(default_factory=ContextObjectsModel)


def _clean_value(v: Any) -> Any:
    """
    Recursively sanitizes values for the trace:
    - Bytes -> Hex string (raw)
    - Int subclasses -> int
    - Lists/Dicts -> Recursive clean
    """
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, int):
        return int(v)
    if isinstance(v, list):
        return [_clean_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean_value(val) for k, val in v.items()}
    return v


class TraceStepModel(BaseModel):
    """
    A single step in the execution trace.
    Represents a function call ('op'), its inputs, and its outcome.
    """

    op: str = Field(..., description="The spec function name, e.g., 'process_slots'")
    params: dict[str, Any] = Field(
        default_factory=dict, description="Arguments passed to the function"
    )
    result: Any | None = Field(
        None, description="The return value (context var reference or primitive)"
    )
    error: dict[str, str] | None = Field(
        None, description="Error details if the operation raised an exception"
    )

    @field_validator("params", "result", mode="before")
    @classmethod
    def sanitize_data(cls, v: Any) -> Any:
        return _clean_value(v)


class TraceModel(BaseModel):
    """
    The root schema for 'trace.yaml'.
    """

    metadata: dict[str, Any] = Field(..., description="Test run metadata (fork, preset, etc.)")
    context: ContextModel = Field(default_factory=ContextModel)
    trace: list[dict[str, Any]] = Field(default_factory=list)  # Stored as dicts internally


class ConfigModel(BaseModel):
    """
    Schema for 'config.yaml'.
    """

    config: dict[str, Any] = Field(..., description="Dictionary of config constants")


class MetaModel(BaseModel):
    """
    Schema for 'meta.yaml'.
    """

    meta: dict[str, Any] = Field(..., description="Dictionary of metadata key/value pairs")
