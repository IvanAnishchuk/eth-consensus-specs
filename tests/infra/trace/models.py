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
    error: dict[str, str] | None = Field(
        None, description="Error details if the operation raised an exception"
    )
    # TODO: verify if we actually need to trace exceptions like ever

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

    # TODO: perhaps str, str unless it can int, bool, etc. sometimes
    #metadata: dict[str, Any] = Field(
    #    ..., default_factory=list, description="Test run metadata (fork, preset, etc.)"
    #)
    trace: list[AssertStateStepModel | LoadStateStepModel | SpecCallStepModel] = Field(default_factory=list)

    # TODO: remove this one as well?
    # it's used to temporary keep artifacts before dumping but really we should probably dump them right away and just save the hashes in trace
    # FIXME: but if we need to pass all artifacts as objects in output - we should keep them somewhere...
    # Private registry state (not serialized directly, used to build the trace)
    _artifacts: dict[str, View] = PrivateAttr(default_factory=dict)

    # TODO: if we are using these to store artifacts to return to the runner we should probably enshrine it and make sure to serialize early (to avoid problems with mutation)


# TODO most of these are not needed with the new approach
# TODO make a standalone utility function maybe
def dump_to_dir(trace_obj: TraceModel, output_dir: Path) -> None:
    """
    Writes the trace and all artifacts to the specified directory.
    """
    os.makedirs(output_dir, exist_ok=True)

    # TODO if we're not keeping the mapping, perhaps dump the objects right away?
    # 1. Write SSZ artifacts
    for filename, obj in trace_obj._artifacts.items():
        write_ssz_artifact(obj, output_dir / filename)  # TODO pass dir only to be combined with hash filename later

    # 2. Write YAML files
    path = output_dir / "trace.yaml"
    try:
        with open(path, "w") as f:
            # TODO: mode='json' is recommended to convert to JSON-compatible types
            # yeah, I think it works
            yaml.dump(trace_obj.model_dump(mode="json", exclude_none=True), f, sort_keys=False, default_flow_style=False)
    except Exception as e:
        print(f"[Trace Recorder] ERROR: Failed to write YAML {path}: {e}")
        raise

    print(f"[Trace Recorder] Saved artifacts to {output_dir}")

def write_ssz_artifact(obj: View, path: Path) -> None:
    """Helper to write an SSZ object to disk (snappy compresed)."""
    # TODO: we can get hash from obj here, perhaps we should use that and pass dirpath in path only to combine here
    try:
        with open(path, "wb") as f:
            # FIXME: not completely officially sure I'm doing this right, there's no standard helper
            f.write(snappy.compress(ssz_serialize(obj)))
    # TODO: make sure we apply snappy compression everywhere and use ssz_snappy extension
    # FIXME: is there any serialization+compression helper we should be using?
    except Exception as e:
        print(f"[Trace Recorder] ERROR: Failed to write SSZ artifact {path}: {e}")
        raise
