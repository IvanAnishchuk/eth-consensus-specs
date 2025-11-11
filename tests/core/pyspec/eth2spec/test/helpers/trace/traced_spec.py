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

import inspect  # <--- ADDED
import os
from collections.abc import Sized
from typing import Any, cast

import wrapt
import yaml

from eth2spec.utils.ssz.ssz_impl import serialize as ssz_serialize
from remerkleable.complex import Container


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
    configure, meta, and ssz artifact recording.
    """

    _self_trace_steps: list[dict[str, Any]]
    _self_context_fixture_names: list[str]
    _self_obj_to_name_map: dict[int, ContextVar]
    _self_name_to_obj_map: dict[ContextVar, Any]
    _self_artifacts_to_write: dict[str, Container]
    _self_obj_counter: dict[str, int]
    _self_config_data: dict[str, Any]
    _self_metadata: dict[str, Any]
    _self_parameters: dict[str, Any]

    def __init__(
        self,
        wrapped_spec: Any,
        initial_context_fixtures: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
    ):
        super().__init__(wrapped_spec)

        # Internal state for recording
        self._self_trace_steps = []
        self._self_context_fixture_names = []
        self._self_obj_to_name_map = {}
        self._self_name_to_obj_map = {}
        self._self_artifacts_to_write = {}  # Manually-added SSZ
        self._self_auto_artifacts: dict[str, Container] = {}  # Auto-recorded SSZ
        self._self_obj_counter = {}
        self._self_config_data = {}
        # Merged metadata: holds both static (init) and dynamic (yielded) metadata
        self._self_metadata = metadata or {}
        self._self_parameters = parameters or {}

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

    def configure(self, config_dict: dict[str, Any]) -> None:
        """
        Records configuration data. Replaces `yield "config", ...`.
        """
        self._self_config_data.update(config_dict)

    def meta(self, key: str, value: Any) -> None:
        """
        Records a metadata entry. Replaces `yield "meta", ...`.
        This updates the unified metadata dictionary.
        """
        self._self_metadata[key] = value

    def ssz(self, name: str, ssz_object: Container) -> None:
        """
        Manually records an SSZ artifact to be saved.
        Replaces `yield "filename.ssz", ...`.
        Note: The object's context name is automatically derived.
        """
        step: dict[str, Any] = {"op": "ssz", "params": {"name": name}}
        if not name.endswith(".ssz"):
            raise ValueError(f"SSZ filename must end with .ssz, got: {name}")

        # Serialize the object to give it a context name
        self._serialize_arg(ssz_object)
        step["result"] = self._serialize_arg(ssz_object, preferred_name=name)

        # Add it to the manual write list
        self._self_artifacts_to_write[name] = ssz_object
        self._self_trace_steps.append(step)

    # --- Core Proxy Logic ---

    def __getattr__(self, name: str) -> Any:
        """
        Main proxy entry point.
        - Intercepts `spec.function()` calls to auto-record them.
        - Passes through to `self.meta()`, `self.configure()`, etc.
        """
        # 1. Check for recorder's own methods first (configure, meta, ssz, etc.)
        if name in ("configure", "meta", "ssz", "save_trace"):
            return object.__getattribute__(self, name)

        # 2. Get the real attribute from the wrapped 'spec'
        real_attr = super().__getattr__(name)

        # print(f'{name} ')
        if not name.islower():
            # this is weird but types like List are functions, not classes here, need to just return them
            return real_attr

        if not callable(real_attr) or name.startswith("_"):
            return real_attr

        # 3. Create the recording wrapper
        def record_wrapper(*args: Any, **kwargs: Any) -> Any:
            """
            Intercepts the function call, records it,
            detects state changes, and then executes the real function.
            """
            # --- CHANGED: Use inspect to bind args/kwargs to parameter names ---
            sig = inspect.signature(real_attr)
            bound_args = sig.bind(*args, **kwargs)
            bound_args.apply_defaults()

            serial_params = {}
            for param_name, param_value in bound_args.arguments.items():
                serial_params[param_name] = self._serialize_arg(param_value)

            step: dict[str, Any] = {"op": name, "params": serial_params}
            # ------------------------------------------------------------------

            # --- BUG FIX: Find the state object in args or kwargs ---
            state_obj = None
            # print(args, kwargs)
            if "state" in kwargs:
                state_obj = kwargs["state"]
            elif (
                len(args) > 0
                and isinstance(args[0], Container)
                and type(args[0]).__name__ == "BeaconState"
            ):
                # Assume state is the first positional arg if it's a BeaconState
                state_obj = args[0]
                # FIXME it's almost never the first arg
            # --- END BUG FIX ---

            # FIXME: something is wrong here, state is always recorded the same

            old_hash: bytes | None = None
            old_id: int | None = None
            if state_obj and isinstance(state_obj, Container):
                old_hash = state_obj.hash_tree_root()
                old_id = id(state_obj)

            # --- Execute the real function ---
            # (With Patch 2 Exception handling included)
            try:
                result = real_attr(*args, **kwargs)
            except Exception as e:
                step["result"] = None
                step["error"] = {"type": type(e).__name__, "message": str(e)}
                self._self_trace_steps.append(step)
                raise e

            # Handle new objects returned
            # if result and (not state_obj or id(result) != id(state_obj)):
            if True:
                if isinstance(result, Container):
                    step["result"] = self._serialize_arg(result, auto_artifact=True)
                elif isinstance(result, (int, str, bool, bytes, type(None))):
                    step["result"] = result

            self._self_trace_steps.append(step)

            # Handle state mutation
            # TODO: this should be a separate thing: we need to record both result and state change
            if state_obj and isinstance(state_obj, Container):
                new_hash = state_obj.hash_tree_root()
                if old_id is not None:
                    old_state_name = self._self_obj_to_name_map.get(old_id)
                else:
                    old_state_name = None

                load_state_step: dict[str, Any] = {"op": "load_state", "params": {}}

                if old_hash != new_hash:
                    # STATE CHANGED
                    load_state_step["result"] = self._serialize_arg(state_obj, auto_artifact=True)
                    self._self_trace_steps.append(load_state_step)
                else:
                    # STATE NOT CHANGED
                    load_state_step["result"] = old_state_name

            return result

        return record_wrapper

    def _serialize_arg(
        self, arg: Any, preferred_name: str | None = None, auto_artifact: bool = False
    ) -> Any:
        """
        Turns a Python object into its YAML context name (e.g., "$context.states.v0")
        and marks it for artifact saving if it's a new SSZ object.
        """
        # Handle primitives and ensure they are standard Python types
        # (not subclasses like Slot, ValidatorIndex, etc.)
        if isinstance(arg, bool):
            return bool(arg)
        if isinstance(arg, int):
            return int(arg)
        if isinstance(arg, str):
            return str(arg)
        if isinstance(arg, type(None)):
            return None
        if isinstance(arg, bytes):
            return bytes(arg)
        if isinstance(arg, Sized) and not isinstance(arg, Container):
            return arg

        class_name = type(arg).__name__
        if class_name not in CLASS_NAME_MAP:
            return f"<unserializable {class_name}>"

        cast_arg = cast(Container, arg)

        arg_id = id(cast_arg)
        if arg_id in self._self_obj_to_name_map:
            return self._self_obj_to_name_map[arg_id]
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
        self._self_name_to_obj_map[name] = cast_arg

        if auto_artifact or preferred_name:
            # Add to the *auto* artifact list
            self._self_auto_artifacts[filename] = cast_arg

        return name

    def save_trace(self, output_dir: str) -> None:
        """
        Saves all recorded data (trace, configure, meta, ssz)
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
                # Use the object's own .serialize() method
                with open(artifact_path, "wb") as f:
                    f.write(ssz_serialize(obj))
            except AttributeError:
                print(f"ERROR: Object for {filename} is not SSZ-serializable (no .serialize())")
            except Exception as e:
                print(f"ERROR: Failed to write SSZ artifact {filename}: {e}")

        # --- 2. Write trace.yaml ---
        try:
            trace_model = TraceModel(
                metadata=self._self_metadata,
                context=ContextModel(
                    fixtures=self._self_context_fixture_names,
                    parameters=self._self_parameters,
                    objects=context_objects,
                ),
                trace=[TraceStepModel(**step) for step in self._self_trace_steps],
            )
            with open(os.path.join(output_dir, "trace.yaml"), "w") as f:
                yaml.dump(trace_model.model_dump(exclude_none=True), f, sort_keys=False, default_flow_style=False)
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
        if self._self_metadata:
            try:
                meta_model = MetaModel(meta=self._self_metadata)
                with open(os.path.join(output_dir, "meta.yaml"), "w") as f:
                    yaml.dump(meta_model.model_dump(), f, sort_keys=False, default_flow_style=False)
            except Exception as e:
                print(f"ERROR: Failed to write meta.yaml: {e}")

        print(f"[Trace Recorder] Saved artifacts to {output_dir}")
