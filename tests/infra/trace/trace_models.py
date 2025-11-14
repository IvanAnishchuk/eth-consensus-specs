"""
Trace Models
------------
Pydantic models defining the schema for the generated test vector artifacts:
- trace.yaml: The sequence of operations.
- config.yaml: The system configuration.
- meta.yaml: Test metadata.
"""

from typing import Any, cast, TypeAlias

from pydantic import BaseModel, Field, field_validator, PrivateAttr
from pydantic.types import constr
from remerkleable.complex import Container

# --- Configuration ---

# Classes that should be treated as tracked SSZ objects in the trace.
# Maps class name -> context collection name.
CLASS_NAME_MAP: dict[str, str] = {
    "BeaconState": "states",
    "BeaconBlock": "blocks",
    "Attestation": "attestations",
}

# Non-SSZ fixtures that should be captured by name.
NON_SSZ_FIXTURES: set[str] = {"store"}

# Regex to match a context variable reference, e.g., "$context.states.initial"
CONTEXT_VAR_REGEX = r"^\$context\.\w+\.\w+$"
ContextVar: TypeAlias = constr(pattern=CONTEXT_VAR_REGEX)


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

    # Private registry state (not serialized directly, used to build the trace)
    _obj_to_name: dict[int, str] = PrivateAttr(default_factory=dict)
    _name_to_obj: dict[str, Any] = PrivateAttr(default_factory=dict)
    _artifacts: dict[str, Container] = PrivateAttr(default_factory=dict)

    def register_object(self, obj: Any, preferred_name: str = None) -> str | None:
        """
        Registers an object in the trace context.
        - If it's an SSZ object (Container), it gets a hash-based name.
        - If it's a primitive, it returns None (passed through).
        """
        if obj is None:
            return None
        
        if not isinstance(obj, Container):
            return None

        class_name = type(obj).__name__
        if class_name not in CLASS_NAME_MAP:
            return None # Unknown object type

        obj_type = CLASS_NAME_MAP[class_name]
        obj_id = id(obj)

        # Check if already registered
        if obj_id in self._obj_to_name and not preferred_name:
            existing_name = self._obj_to_name[obj_id]
            # Special check for states: handle out-of-band mutation re-registration
            if obj_type == "states":
                current_root = obj.hash_tree_root().hex()
                if not existing_name.endswith(f".{current_root}"):
                    pass # Root changed, re-register with new hash
                else:
                    return existing_name
            else:
                return existing_name

        # Generate Name (Content-Addressed)
        root_hex = obj.hash_tree_root().hex()
        context_name = cast(ContextVar, f"$context.{obj_type}.{root_hex}")
        
        filename: str
        if preferred_name:
            # Manual naming (e.g., via fixture)
            filename = preferred_name if preferred_name.endswith(".ssz") else f"{preferred_name}.ssz"
        else:
            # Auto naming (content-addressed)
            filename = f"{obj_type}_{root_hex}.ssz"

        # Update Registry
        self._obj_to_name[obj_id] = context_name
        self._name_to_obj[context_name] = obj
        self._artifacts[filename] = obj
        
        # Update the public ContextObjectsModel (for output)
        if hasattr(self.context.objects, obj_type):
            getattr(self.context.objects, obj_type)[root_hex] = filename
        
        return context_name


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
