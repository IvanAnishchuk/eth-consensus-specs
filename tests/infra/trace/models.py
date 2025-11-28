"""
Trace Models
------------
Pydantic models defining the schema for the generated test vector artifacts:
- Trace
- TraceStep
- Context
"""

import os
from typing import Any, cast, TypeAlias

import yaml
from pydantic import BaseModel, Field, field_validator, PrivateAttr
from pydantic.types import constr
from remerkleable.complex import Container

from eth2spec.utils.ssz.ssz_impl import serialize as ssz_serialize
from eth2spec.utils.ssz.ssz_typing import View  # used to check SSZ objects


# simple way to make sure primitive subclasses are coerced to base types
# FIXME: perhaps not the cleanest way, to be reviewed
def _clean_value(v: Any) -> Any:
    """
    Recursively sanitizes values for the trace:
    - Bytes -> Hex string (with 0x prefix)
    - Int subclasses -> int
    - Lists/Dicts -> Recursive clean
    """
    if isinstance(v, bytes):
        return f"0x{v.hex()}"
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

    op: str = Field(..., description="The operation name e.g. load_state, spec_call")
    # TODO we might want an abstract base class where op defines the subclass
    method: str = Field(..., description="The spec function name, e.g., 'process_slots'")
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
    The root schema for the trace file.
    Contains metadata, context, and the execution trace.
    """

    # TODO: perhaps str, str unless it can int, bool, etc. sometimes
    metadata: dict[str, Any] = Field(..., default_factory=list, description="Test run metadata (fork, preset, etc.)")
    trace: list[TraceStepModel] = Field(default_factory=list)

    # Private registry state (not serialized directly, used to build the trace)
    _artifacts: dict[str, View] = PrivateAttr(default_factory=dict)

    # TODO make a standalone utility function maybe
    def dump_to_dir(self, output_dir: str, config: dict[str, Any] = None) -> None:
        """
        Writes the trace and all artifacts to the specified directory.
        """
        os.makedirs(output_dir, exist_ok=True)

        # 1. Write SSZ artifacts
        for filename, obj in self._artifacts.items():
            self._write_ssz(os.path.join(output_dir, filename), obj)

        # 2. Write YAML files
        self._write_yaml(os.path.join(output_dir, "trace.yaml"), self.model_dump(exclude_none=True))

        print(f"[Trace Recorder] Saved artifacts to {output_dir}")

    def _write_ssz(self, path: str, obj: Any) -> None:
        """Helper to write an SSZ object to disk."""
        try:
            with open(path, "wb") as f:
                f.write(ssz_serialize(obj))
        except Exception as e:
            print(f"ERROR: Failed to write SSZ artifact {path}: {e}")

    def _write_yaml(self, path: str, data: Any) -> None:
        """Helper to write data as YAML to disk."""
        try:
            with open(path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as e:
            print(f"ERROR: Failed to write YAML {path}: {e}")
