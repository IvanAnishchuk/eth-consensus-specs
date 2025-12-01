"""
Trace Models
------------
Pydantic models defining the schema for the generated test vector artifacts:
- Trace
- TraceStep
- Context
"""

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, PrivateAttr

from eth2spec.utils.ssz.ssz_impl import serialize as ssz_serialize
from eth2spec.utils.ssz.ssz_typing import View  # used to check SSZ objects
import snappy


# TODO: recheck how it all works without this now
def _clean_value(value: Any) -> Any:
    """
    Hexify raw bytes.

    Recursively sanitizes values for the trace:
    - Bytes -> Hex string (with 0x prefix)
    - Lists/Dicts -> Recursive clean
    """
    if isinstance(value, bytes):
        return f"0x{value.hex()}"
    if isinstance(value, list):
        return [_clean_value(elem) for elem in value]
    if isinstance(value, dict):
        return {key: _clean_value(val) for key, val in value.items()}
    return value


class TraceStepModel(BaseModel):  # TODO: add ABC or whatever required for abstract class
    """
    A single step in the execution trace.
    Represents a function call ('op'), its inputs, and its outcome.
    """

    #op: str = Field(..., description="The operation name e.g. load_state, spec_call")


class LoadStateStepModel(TraceStepModel):
    """
    Load state step in the execution trace.

    Used when a previously-unseen state is used in spec all.
    State root is recorded as 'state_root'.
    """

    op: str = Field(description="The operation name", default="load_state")
    state_root: str = Field(description="The state root hash as hex string")


class AssertStateStepModel(TraceStepModel):
    """
    Assert state step in the execution trace.

    Auto-added at the end of the trace with the last known state root.
    State root is recorded as 'state_root'.
    """

    op: str = Field(description="The operation name", default="assert_state")
    state_root: str = Field(description="The state root hash as hex string")

class SpecCallStepModel(TraceStepModel):
    """
    Spec call step in the execution trace.

    Spec method called is recorded as 'method'.
    """

    op: str = Field(description="The operation name", default="spec_call")
    method: str = Field(description="The spec function name, e.g., 'process_slots'")
    input: dict[str, Any] = Field(
        default_factory=dict, description="Arguments passed to the function"
    )
    output: Any | None = Field(
        None, description="The return value (context var reference or primitive)"
    )

    # FIXME: perhaps we should use serializer rather than validator? sounds like more idiomatic pydantic maybe
    @field_validator("input", "output", mode="before")
    @classmethod
    def sanitize_data(cls, value: Any) -> Any:
        return _clean_value(value)

class TraceModel(BaseModel):
    """
    The root schema for the trace file.
    Contains metadata, context, and the execution trace.
    """

    trace: list[AssertStateStepModel | LoadStateStepModel | SpecCallStepModel] = Field(default_factory=list)

    # TODO: remove this one as well?
    # it's used to temporary keep artifacts before dumping but really we should probably dump them right away and just save the hashes in trace
    # FIXME: but if we need to pass all artifacts as objects in output - we should keep them somewhere...
    # Private registry state (not serialized directly, used to build the trace)
    _artifacts: dict[str, View] = PrivateAttr(default_factory=dict)

    # TODO: if we are using these to store artifacts to return to the runner we should probably enshrine it and make sure to serialize early (to avoid problems with mutation)
