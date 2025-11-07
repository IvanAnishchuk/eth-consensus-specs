"""
This file contains the core logic for the spec trace recording framework.
It uses 'wrapt' to create a proxy object that wraps the pyspec 'spec'
and records all interactions.

It provides explicit methods to replace the old `yield` system:
- `spec.config(...)` replaces `yield "config", ...`
- `spec.meta(...)` replaces `yield "meta", ...`
- `spec.ssz(...)` replaces `yield "filename.ssz", ...`

All other calls to `spec.function(...)` are auto-recorded as trace steps.
"""

import os
from collections.abc import Sized
from typing import Any, cast

import wrapt
import yaml

try:
    from ssz.api import encode as ssz_encode
    from ssz.hashable_container import HashableContainer
except ImportError:
    print("WARNING: ssz library not found. Using placeholder for ssz_encode.")

    def ssz_encode(obj: Any) -> bytes:
        return f"SSZ_ENCODED_{type(obj).__name__}".encode()

    class HashableContainer:
        def hash_tree_root(self) -> bytes:
            return b"\0" * 32


# Import all Pydantic models for artifact generation
from .trace_models import (
    ConfigModel,
    ContextModel,
    ContextObjectsModel,
    ContextVar,
    MetaModel,
    TraceModel,
    TraceStepModel,
)

# --- Type Maps (from your context) ---
# These maps tell the recorder which classes are SSZ-serializable
# and what their 'type' name is in the YAML context.
# NOTE: In a real implementation, these would be imported from a central
# definitions file, not re-defined here.
CLASS_NAME_MAP: dict[str, str] = {
    "BeaconState": "states",
    "BeaconBlock": "blocks",
    "Attestation": "attestations",
    # Add all other SSZ types that can be passed as arguments
}

NON_SSZ_FIXTURES: set[str] = {
    "store",
    # Add other non-SSZ fixtures to be tracked
}
# --- End Type Maps ---


class RecordingSpec(wrapt.ObjectProxy):
    """
    A wrapt.ObjectProxy that records all calls to the wrapped 'spec' object.
    Replaces the `yield` system by providing explicit methods for
    config, meta, and ssz artifact recording.
    """

    _self_trace_steps: list[dict[str, Any]]
    _self_context_fixture_names: list[str]
    _self_obj_to_name_map: dict[int, ContextVar]
    _self_name_to_obj_map: dict[ContextVar, Any]
    _self_artifacts_to_write: dict[str, HashableContainer]
    _self_obj_counter: dict[str, int]
    _self_config_data: dict[str, Any]
    _self_meta_data: dict[str, Any]

    def __init__(self, wrapped_spec: Any, initial_context_fixtures: dict[str, Any]):
        super().__init__(wrapped_spec)

        # Internal state for recording
        self._self_trace_steps = []
        self._self_context_fixture_names = []
        self._self_obj_to_name_map = {}
        self._self_name_to_obj_map = {}
        self._self_artifacts_to_write = {}  # Manually-added SSZ
        self._self_auto_artifacts: dict[str, HashableContainer] = {}  # Auto-recorded SSZ
        self._self_obj_counter = {}
        self._self_config_data = {}
        self._self_meta_data = {}

        # Pre-populate with fixtures
        for name, obj in initial_context_fixtures.items():
            class_name = type(obj).__name__
            if class_name not in CLASS_NAME_MAP:
                if name in NON_SSZ_FIXTURES:
                    self._self_context_fixture_names.append(name)
            else:
                # Seed the context with initial SSZ objects
                self._serialize_arg(obj, preferred_name=name)

    # --- Public API to Replace `yield` ---

    def config(self, config_dict: dict[str, Any]) -> None:
        """
        Records configuration data. Replaces `yield "config", ...`.
        """
        self._self_config_data.update(config_dict)

    def meta(self, key: str, value: Any) -> None:
        """
        Records a metadata entry. Replaces `yield "meta", ...`.
        """
        self._self_meta_data[key] = value

    def ssz(self, filename: str, ssz_object: HashableContainer) -> None:
        """
        Manually records an SSZ artifact to be saved.
        Replaces `yield "filename.ssz", ...`.
        Note: The object's context name is automatically derived.
        """
        if not filename.endswith(".ssz"):
            raise ValueError(f"SSZ filename must end with .ssz, got: {filename}")

        # Serialize the object to give it a context name
        self._serialize_arg(ssz_object)

        # Add it to the manual write list
        self._self_artifacts_to_write[filename] = ssz_object

    # --- Core Proxy Logic ---

    def __getattr__(self, name: str) -> Any:
        """
        Main proxy entry point.
        - Intercepts `spec.function()` calls to auto-record them.
        - Passes through to `self.meta()`, `self.config()`, etc.
        """
        # 1. Check for recorder's own methods first (config, meta, ssz, etc.)
        if name in ("config", "meta", "ssz", "save_all"):
            return object.__getattribute__(self, name)

        # 2. Get the real attribute from the wrapped 'spec'
        real_attr = super().__getattr__(name)

        if not callable(real_attr) or name.startswith("_"):
            return real_attr

        # 3. Create the recording wrapper
        def record_wrapper(*args: Any, **kwargs: Any) -> Any:
            """
            Intercepts the function call, records it,
            detects state changes, and then executes the real function.
            """
            serial_kwargs = self._serialize_kwargs(kwargs)
            step: dict[str, Any] = {"op": name, "params": serial_kwargs}

            state_obj = kwargs.get("state", None)
            old_hash: bytes | None = None
            if state_obj and isinstance(state_obj, HashableContainer):
                old_hash = state_obj.hash_tree_root()

            # --- Execute the real function ---
            result = real_attr(*args, **kwargs)

            # Handle state mutation
            if state_obj and isinstance(state_obj, HashableContainer) and old_hash is not None:
                new_hash = state_obj.hash_tree_root()
                old_state_name = self._self_obj_to_name_map[id(state_obj)]

                if old_hash != new_hash:
                    # STATE CHANGED
                    step["result"] = self._serialize_arg(state_obj, auto_artifact=True)
                else:
                    # STATE NOT CHANGED
                    step["result"] = old_state_name

            # Handle new objects returned
            if result and (not state_obj or id(result) != id(state_obj)):
                if isinstance(result, HashableContainer):
                    step["result"] = self._serialize_arg(result, auto_artifact=True)
                elif isinstance(result, (int, str, bool, bytes)):
                    step["result"] = result

            self._self_trace_steps.append(step)
            return result

        return record_wrapper

    def _serialize_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        serial = {}
        for key, value in kwargs.items():
            serial[key] = self._serialize_arg(value)
        return serial

    def _serialize_arg(
        self, arg: Any, preferred_name: str | None = None, auto_artifact: bool = False
    ) -> Any:
        """
        Turns a Python object into its YAML context name (e.g., "$context.states.v0")
        and marks it for artifact saving if it's a new SSZ object.
        """
        if isinstance(arg, (int, str, bool, type(None), bytes)) or (
            isinstance(arg, Sized) and not isinstance(arg, HashableContainer)
        ):
            return arg

        arg_id = id(arg)
        if arg_id in self._self_obj_to_name_map:
            return self._self_obj_to_name_map[arg_id]

        class_name = type(arg).__name__
        if class_name not in CLASS_NAME_MAP:
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
            # Use 'v' for states (versions), 'b' for blocks/others (created)
            prefix = "v" if obj_type == "states" else "b"
            name = cast(ContextVar, f"$context.{obj_type}.{prefix}{count}")
            filename = f"{obj_type}_{prefix}{count}.ssz"
            self._self_obj_counter[obj_type] = count + 1

        self._self_obj_to_name_map[arg_id] = name
        self._self_name_to_obj_map[name] = arg

        if auto_artifact or preferred_name:
            # Add to the *auto* artifact list
            self._self_auto_artifacts[filename] = cast(HashableContainer, arg)

        return name

    def save_all(self, output_dir: str) -> None:
        """
        Saves all recorded data (trace, config, meta, ssz)
        to the specified output directory.
        This is called by the `@traced_test` decorator.
        """
        os.makedirs(output_dir, exist_ok=True)

        # --- 1. Combine and Write SSZ artifacts ---
        context_objects = ContextObjectsModel()

        # Merge auto-artifacts and manual SSZ artifacts
        # Manual artifacts (`spec.ssz()`) take precedence
        all_artifacts = {**self._self_auto_artifacts, **self._self_artifacts_to_write}

        for filename, obj in all_artifacts.items():
            # Get the object's context name (e.g., "initial", "b0", "v1")
            obj_id = id(obj)
            if obj_id not in self._self_obj_to_name_map:
                print(
                    f"WARNING: SSZ object for {filename} was never used in trace, skipping context."
                )
                continue

            context_name = self._self_obj_to_name_map[obj_id].split(".")[-1]
            obj_type_name = CLASS_NAME_MAP[type(obj).__name__]

            if not hasattr(context_objects, obj_type_name):
                print(f"ERROR: Object type '{obj_type_name}' not in ContextObjectsModel schema.")
                continue

            obj_type_dict = getattr(context_objects, obj_type_name)
            obj_type_dict[context_name] = filename

            artifact_path = os.path.join(output_dir, filename)
            try:
                with open(artifact_path, "wb") as f:
                    f.write(ssz_encode(obj))
            except Exception as e:
                print(f"ERROR: Failed to write SSZ artifact {filename}: {e}")
                # Don't raise, try to write other files

        # --- 2. Write trace.yaml ---
        try:
            trace_model = TraceModel(
                context=ContextModel(
                    fixtures=self._self_context_fixture_names, objects=context_objects
                ),
                trace=[TraceStepModel(**step) for step in self._self_trace_steps],
            )
            with open(os.path.join(output_dir, "trace.yaml"), "w") as f:
                yaml.dump(trace_model.model_dump(), f, sort_keys=False, default_flow_style=False)
        except Exception as e:
            print(f"ERROR: Failed to write trace.yaml: {e}")

        # --- 3. Write config.yaml ---
        if self._self_config_data:
            try:
                config_model = ConfigModel(config=self._self_config_data)
                with open(os.path.join(output_dir, "config.yaml"), "w") as f:
                    yaml.dump(
                        config_model.model_dump(), f, sort_keys=False, default_flow_style=False
                    )
            except Exception as e:
                print(f"ERROR: Failed to write config.yaml: {e}")

        # --- 4. Write meta.yaml ---
        if self._self_meta_data:
            try:
                meta_model = MetaModel(meta=self._self_meta_data)
                with open(os.path.join(output_dir, "meta.yaml"), "w") as f:
                    yaml.dump(meta_model.model_dump(), f, sort_keys=False, default_flow_style=False)
            except Exception as e:
                print(f"ERROR: Failed to write meta.yaml: {e}")

        print(f"[Trace Recorder] Saved artifacts to {output_dir}")
