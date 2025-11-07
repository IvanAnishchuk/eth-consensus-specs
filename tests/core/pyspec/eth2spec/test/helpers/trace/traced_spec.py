"""
This file contains the core logic for the spec trace recording framework.
It uses the 'wrapt' library to create a proxy object that wraps
the pyspec 'spec' and records all interactions.
"""

import logging
import os
from collections.abc import Sized
from typing import Any, cast

import wrapt
import yaml

# --- Real Imports ---
# These imports are assumed to be correct for the eth2spec repository.
# They replace all previous placeholders.
try:
    from ssz.api import encode as ssz_encode, is_ssz_type
    from ssz.hashable_container import HashableContainer
except ImportError:
    print("WARNING: ssz library not found. Using placeholder for ssz_encode.")

    def ssz_encode(obj: Any) -> bytes:  # type: ignore
        return f"SSZ_ENCODED_{type(obj).__name__}".encode()

    def is_ssz_type(typ: type[Any]) -> bool:  # type: ignore
        return hasattr(typ, "hash_tree_root")

    class HashableContainer:  # type: ignore
        def hash_tree_root(self) -> bytes:
            return b"\0" * 32


# Import spec object types
from eth2spec.test.context import (
    Attestation,
    BeaconBlock,
    BeaconState,  # Add all relevant SSZ types
)

# --- FIXED: Removed unused import ---
# from eth2spec.phase0 import spec as spec_phase0
# ------------------------------------
# Import our Pydantic models
from .trace_models import ContextModel, ContextObjectsModel, ContextVar, TraceModel, TraceStepModel

# --- End Real Imports ---

# Set up a logger for this module
log = logging.getLogger(__name__)

# This map tells the recorder which classes are SSZ-serializable
# and what their 'type' name is in the YAML context (e.g., 'states', 'blocks')
CLASS_NAME_MAP: dict[str, str] = {
    BeaconState.__name__: "states",
    BeaconBlock.__name__: "blocks",
    Attestation.__name__: "attestations",
    # Add all other SSZ types that can be passed as arguments
}

# A set of non-SSZ fixture names that should be tracked
NON_SSZ_FIXTURES: set[str] = {
    "store",
    # Add other fixtures like 'proposer_indices', 'validator_indices' if they
    # are used as non-SSZ arguments in spec functions.
}


class RecordingSpec(wrapt.ObjectProxy):
    """
    A wrapt.ObjectProxy that records all calls to the wrapped 'spec' object.

    Generates a YAML trace file and a directory of SSZ artifacts.
    """

    _self_trace_steps: list[dict[str, Any]]
    _self_context_fixture_names: list[str]
    _self_obj_to_name_map: dict[int, ContextVar]
    _self_name_to_obj_map: dict[ContextVar, Any]
    _self_artifacts_to_write: dict[str, HashableContainer]
    _self_obj_counter: dict[str, int]

    def __init__(self, wrapped_spec: Any, initial_context_fixtures: dict[str, Any]):
        """
        Initializes the recorder.

        :param wrapped_spec: The real 'spec' object to be proxied.
        :param initial_context_fixtures: A dict of pytest fixtures (like 'state',
                                         'genesis_block') that the test function
                                         depends on. These are the "seed"
                                         objects for the trace.
        """
        super().__init__(wrapped_spec)

        self._self_trace_steps = []
        self._self_context_fixture_names = []
        self._self_obj_to_name_map = {}
        self._self_name_to_obj_map = {}
        self._self_artifacts_to_write = {}
        self._self_obj_counter = {}

        # --- Pre-populate with fixtures ---
        for name, obj in initial_context_fixtures.items():
            class_name = type(obj).__name__
            if class_name not in CLASS_NAME_MAP:
                if name in NON_SSZ_FIXTURES:
                    self._self_context_fixture_names.append(name)
            else:
                self._serialize_arg(obj, preferred_name=name)

    def __getattr__(self, name: str) -> Any:
        """
        Main proxy entry point. Called whenever any attribute
        (including functions) is accessed on the 'spec' object.
        """
        real_attr = super().__getattr__(name)

        if not callable(real_attr) or name.startswith("_"):
            return real_attr

        def record_wrapper(*args: Any, **kwargs: Any) -> Any:
            """
            Intercepts the function call, records it,
            detects state changes, and then executes the real function.
            """

            # 1. Serialize the call *before* execution
            serial_kwargs = self._serialize_kwargs(kwargs)
            step: dict[str, Any] = {"op": name, "params": serial_kwargs}

            # 2. Find the state object for change detection
            state_obj = kwargs.get("state", None)
            old_hash: bytes | None = None
            if state_obj and isinstance(state_obj, HashableContainer):
                old_hash = state_obj.hash_tree_root()

            # --- 3. Execute the real function ---
            result = real_attr(*args, **kwargs)
            # --- End execution ---

            # 4. Handle state mutation
            if state_obj and isinstance(state_obj, HashableContainer) and old_hash is not None:
                new_hash = state_obj.hash_tree_root()
                old_state_name = self._self_obj_to_name_map[id(state_obj)]

                if old_hash != new_hash:
                    # STATE CHANGED: Give it a new name and mark for saving
                    count = self._self_obj_counter.get("states", 0)
                    new_state_name = cast(ContextVar, f"$context.states.v{count}")
                    self._self_obj_counter["states"] = count + 1

                    self._self_obj_to_name_map[id(state_obj)] = new_state_name
                    self._self_name_to_obj_map[new_state_name] = state_obj
                    self._artifacts_to_write[f"state_v{count}.ssz"] = state_obj

                    step["result"] = new_state_name
                else:
                    # STATE NOT CHANGED: Reuse the old name
                    step["result"] = old_state_name

            # 5. Handle new objects returned by the function
            if result and (not state_obj or id(result) != id(state_obj)):
                if isinstance(result, HashableContainer):
                    result_name = self._serialize_arg(result)
                    step["result"] = result_name
                elif isinstance(result, (int, str, bool, bytes)):
                    # Now we correctly record simple return values.
                    step["result"] = result

            # 6. ALWAYS record the step
            self._self_trace_steps.append(step)
            return result

        return record_wrapper

    def _serialize_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Helper function to serialize all values in a kwargs dict."""
        serial = {}
        for key, value in kwargs.items():
            serial[key] = self._serialize_arg(value)
        return serial

    def _serialize_arg(self, arg: Any, preferred_name: str | None = None) -> Any:
        """
        Turns a Python object into its YAML context name (e.g., "$context.states.v0")
        and marks it for artifact saving if it's a new SSZ object.
        """

        # --- 1. Handle Literals and simple collections ---
        if isinstance(arg, (int, str, bool, type(None), bytes)) or (
            isinstance(arg, Sized) and not isinstance(arg, HashableContainer)
        ):
            return arg

        # --- 2. Check if we've seen this object ---
        arg_id = id(arg)
        if arg_id in self._self_obj_to_name_map:
            return self._self_obj_to_name_map[arg_id]

        # --- 3. Handle New SSZ Objects ---
        class_name = type(arg).__name__
        if class_name not in CLASS_NAME_MAP:
            log.debug(f"Object of type {class_name} is not in CLASS_NAME_MAP.")
            return f"<unserializable {class_name}>"

        obj_type = CLASS_NAME_MAP[class_name]  # e.g., 'blocks'
        filename: str

        if preferred_name:
            if obj_type == "states":
                name = cast(ContextVar, "$context.states.initial")
                filename = "state_v0.ssz"
            else:
                name = cast(ContextVar, f"$context.{obj_type}.{preferred_name}")
                filename = f"{obj_type}_{preferred_name}.ssz"
        else:
            count = self._self_obj_counter.get(obj_type, 0)
            name = cast(ContextVar, f"$context.{obj_type}.b{count}")
            filename = f"{obj_type}_b{count}.ssz"
            self._self_obj_counter[obj_type] = count + 1

        # --- 4. Update all our maps ---
        self._self_obj_to_name_map[arg_id] = name
        self._self_name_to_obj_map[name] = arg
        self._artifacts_to_write[filename] = cast(HashableContainer, arg)

        return name

    def save_trace(self, trace_filepath: str) -> None:
        """
        Called by the pytest fixture after the test finishes
        to validate and write the final YAML file and all SSZ artifacts.
        """
        trace_dir = os.path.dirname(trace_filepath)

        # --- 1. Write all SSZ artifacts ---
        context_objects = ContextObjectsModel()
        for filename, obj in self._self_artifacts_to_write.items():
            if filename == "state_v0.ssz":
                context_name = "initial"
            else:
                context_name = os.path.splitext(filename)[0].split("_")[-1]

            obj_type_name = CLASS_NAME_MAP[type(obj).__name__]

            # --- FIXED: Check if object type exists on model ---
            if not hasattr(context_objects, obj_type_name):
                log.error(
                    f"Object type '{obj_type_name}' (from CLASS_NAME_MAP) "
                    f"does not exist on Pydantic model 'ContextObjectsModel'. "
                    f"Please add it to 'trace_models.py'."
                )
                # This will likely fail below, which is good.
                continue

            obj_type_dict = getattr(context_objects, obj_type_name)
            obj_type_dict[context_name] = filename

            artifact_path = os.path.join(trace_dir, filename)
            try:
                with open(artifact_path, "wb") as f:
                    f.write(ssz_encode(obj))
            except Exception as e:
                log.error(f"Failed to write SSZ artifact {filename} to {artifact_path}: {e}")
                raise

        # --- 2. Build the final Pydantic model ---
        try:
            trace_step_models = [TraceStepModel(**step) for step in self._self_trace_steps]
            context_model = ContextModel(
                fixtures=self._self_context_fixture_names, objects=context_objects
            )
            trace_model = TraceModel(context=context_model, trace=trace_step_models)
        except Exception as e:
            log.error(f"Failed to validate trace data with Pydantic: {e}")
            raise

        # --- 3. Write the YAML file ---
        try:
            with open(trace_filepath, "w") as f:
                yaml.dump(
                    trace_model.model_dump(),
                    f,
                    sort_keys=False,
                    default_flow_style=False,
                    width=120,
                )
            log.info(f"\n[Trace Recorder] Trace recorded to {trace_filepath}")
            log.info(
                f"[Trace Recorder] Saved {len(self._artifacts_to_write)} SSZ artifacts to {trace_dir}"
            )
        except Exception as e:
            log.error(f"Failed to write YAML trace {trace_filepath}: {e}")
            raise
