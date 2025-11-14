"""
Traced Spec Proxy
-----------------
A wrapper around the Ethereum Specification object that records all interactions.

It uses `wrapt.ObjectProxy` to intercept function calls, recording their:
1. Arguments (sanitized and mapped to context variables)
2. Return values
3. State context (injecting 'load_state' when the context switches)
"""

import inspect
import os
from collections.abc import Sized
from typing import Any, cast

import wrapt
import yaml
from remerkleable.complex import Container

from eth2spec.utils.ssz.ssz_impl import serialize as ssz_serialize

from .trace_models import (
    ConfigModel,
    ContextModel,
    ContextObjectsModel,
    ContextVar,
    MetaModel,
    TraceModel,
    TraceStepModel,
)

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


class RecordingSpec(wrapt.ObjectProxy):
    """
    A proxy that wraps the 'spec' object to record execution traces.

    It automatically handles state versioning and deduplication.
    It automatically intercepts all other function calls.
    """

    # Internal state
    _self_trace_steps: list[dict[str, Any]]
    _self_context_fixture_names: list[str]
    _self_obj_to_name_map: dict[int, str]
    _self_name_to_obj_map: dict[str, Any]
    _self_auto_artifacts: dict[str, Container]
    _self_metadata: dict[str, Any]
    _self_parameters: dict[str, Any]
    _self_last_root: str | None

    def __init__(
        self,
        wrapped_spec: Any,
        initial_context_fixtures: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
    ):
        super().__init__(wrapped_spec)

        # Initialize internal storage
        self._self_trace_steps = []
        self._self_context_fixture_names = []
        self._self_obj_to_name_map = {}
        self._self_name_to_obj_map = {}
        self._self_auto_artifacts = {}
        
        self._self_metadata = metadata or {}
        self._self_parameters = parameters or {}
        self._self_last_root = None

        # Register initial fixtures
        for name, obj in initial_context_fixtures.items():
            self._self_register_fixture(name, obj)

    def _self_register_fixture(self, name: str, obj: Any) -> None:
        """Registers an initial fixture in the recording context."""
        class_name = type(obj).__name__
        if class_name not in CLASS_NAME_MAP:
            if name in NON_SSZ_FIXTURES:
                self._self_context_fixture_names.append(name)
        else:
            # Seed the context with initial SSZ objects
            self._self_process_arg(obj, preferred_name=name)

    # --- Interception Logic ---

    def __getattr__(self, name: str) -> Any:
        """
        Intercepts attribute access on the spec object.
        If the attribute is a callable (function), it is wrapped to record execution.
        """
        # 1. Access recorder's own methods first
        if name == "save_trace":
            return object.__getattribute__(self, name)

        # 2. Retrieve the real attribute from the wrapped spec
        real_attr = super().__getattr__(name)

        # 3. If it's not a function or shouldn't be traced, return as-is
        if not callable(real_attr) or not name.islower() or name.startswith("_"):
            return real_attr

        # 4. Return the recording wrapper
        return self._self_create_wrapper(name, real_attr)

    def _self_create_wrapper(self, op_name: str, real_func: Any) -> Any:
        """Creates a closure to record the function call."""
        
        def record_wrapper(*args: Any, **kwargs: Any) -> Any:
            # A. Prepare arguments: bind to signature and serialize
            bound_args = self._self_bind_args(real_func, args, kwargs)
            
            # Process arguments and auto-register any NEW SSZ objects as artifacts
            serial_params = {
                k: self._self_process_arg(v, auto_artifact=True) 
                for k, v in bound_args.arguments.items()
            }

            # B. Identify State object and handle Context Switching
            state_obj, old_hash, old_id = self._self_capture_pre_state(bound_args)
            
            if old_hash is not None:
                current_root_hex = old_hash.hex()
                # If the state passed to this function is different from the last one we saw,
                # inject a `load_state` operation to switch context.
                if self._self_last_root != current_root_hex:
                    # We need the context variable name for this state
                    # (It should already be registered via _self_process_arg above)
                    state_var = self._self_obj_to_name_map.get(old_id)
                    if state_var:
                        self._self_trace_steps.append(TraceStepModel(
                            op="load_state",
                            params={},
                            result=state_var
                        ).model_dump(exclude_none=True))
                        self._self_last_root = current_root_hex

            # C. Execute the real function
            try:
                result = real_func(*args, **kwargs)
                error = None
            except Exception as e:
                result = None
                error = {"type": type(e).__name__, "message": str(e)}
                # We must record the step before re-raising
                self._self_record_step(op_name, serial_params, result, error)
                raise e

            # D. Record the successful step
            self._self_record_step(op_name, serial_params, result, None)

            # E. Update tracked state if mutated
            if state_obj is not None:
                self._self_update_state_tracker(state_obj, old_hash, old_id)

            return result

        return record_wrapper

    def _self_bind_args(self, func: Any, args: tuple, kwargs: dict) -> inspect.BoundArguments:
        """Binds positional and keyword arguments to the function signature."""
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return bound

    def _self_capture_pre_state(self, bound_args: inspect.BoundArguments) -> tuple[Any, bytes | None, int | None]:
        """Finds the BeaconState argument (if any) and captures its root hash."""
        state_obj = None
        
        # Look for 'state' in arguments
        if "state" in bound_args.arguments:
            state_obj = bound_args.arguments["state"]
        elif bound_args.args:
            # Fallback: Check first positional arg
            # We check class name string to allow for Mocks in tests
            if type(bound_args.args[0]).__name__ == "BeaconState":
                state_obj = bound_args.args[0]

        # If found, capture identity and hash
        # Use duck typing for hash_tree_root to support Mocks
        if state_obj and hasattr(state_obj, "hash_tree_root"):
            return state_obj, state_obj.hash_tree_root(), id(state_obj)
        
        return None, None, None

    def _self_record_step(self, op: str, params: dict, result: Any, error: dict | None) -> None:
        """Appends a step to the trace."""
        # Auto-register the result if it's an SSZ object
        serialized_result = self._self_process_arg(result, auto_artifact=True) if result is not None else None
        
        step_model = TraceStepModel(
            op=op,
            params=params,
            result=serialized_result,
            error=error
        )
        self._self_trace_steps.append(step_model.model_dump(exclude_none=True))

    def _self_update_state_tracker(self, state_obj: Any, old_hash: bytes | None, old_id: int | None) -> None:
        """Updates the internal state tracker if the state object was mutated."""
        if not hasattr(state_obj, "hash_tree_root") or old_hash is None:
            return

        new_hash = state_obj.hash_tree_root()
        new_root_hex = new_hash.hex()

        # Always update the last root to the current state's new root
        # This ensures subsequent operations know what the current state is.
        self._self_last_root = new_root_hex

        if old_hash == new_hash:
            return # No content change

        # State changed: calculate new context name based on root hash
        new_name = cast(ContextVar, f"$context.states.{new_root_hex}")

        # Update internal mapping for this object ID
        if old_id is not None:
            self._self_obj_to_name_map[old_id] = new_name

        # Queue artifact for saving
        filename = f"states_{new_root_hex}.ssz"
        self._self_name_to_obj_map[new_name] = state_obj
        self._self_auto_artifacts[filename] = state_obj

        # We do NOT record an implicit `load_state` here. 
        # The state was mutated in-place by the operation we just recorded.
        # The trace consumer assumes the result of `op` is the new state.

    def _self_process_arg(
        self, arg: Any, preferred_name: str | None = None, auto_artifact: bool = False
    ) -> Any:
        """
        Processes an argument for the trace.
        - SSZ objects are registered in the context and returned as '$context.type.id'.
        - Primitives are passed through (sanitization happens in Pydantic models).
        """
        if arg is None:
            return None
        
        if not isinstance(arg, Container):
            return arg

        # 2. Handle SSZ Objects
        class_name = type(arg).__name__
        if class_name not in CLASS_NAME_MAP:
            return f"<unserializable {class_name}>"

        cast_arg = cast(Container, arg)
        arg_id = id(cast_arg)

        # If we've seen this object exact instance before, return its existing name
        if arg_id in self._self_obj_to_name_map and not preferred_name:
            return self._self_obj_to_name_map[arg_id]

        # 3. Register New Object
        obj_type = CLASS_NAME_MAP[class_name]  # e.g., 'blocks'
        
        # Always use the content-based hash for the context variable name.
        # This ensures that even if an object is loaded from 'pre.ssz', it is referred 
        # to by its hash in the trace, facilitating deduplication.
        root_hex = cast_arg.hash_tree_root().hex()
        name = cast(ContextVar, f"$context.{obj_type}.{root_hex}")
        
        filename: str
        if preferred_name:
            # Manual naming (e.g., via fixture)
            filename = preferred_name if preferred_name.endswith(".ssz") else f"{preferred_name}.ssz"
        else:
            # Auto naming (content-addressed)
            filename = f"{obj_type}_{root_hex}.ssz"

        # Update mappings
        self._self_obj_to_name_map[arg_id] = name
        self._self_name_to_obj_map[name] = cast_arg

        if auto_artifact or preferred_name:
            self._self_auto_artifacts[filename] = cast_arg

        return name

    def save_trace(self, output_dir: str) -> None:
        """
        Writes the captured trace and artifacts to the filesystem.
        """
        os.makedirs(output_dir, exist_ok=True)

        # 1. Collect all artifacts to write
        # Manual artifacts take precedence over auto-captured ones
        all_artifacts = self._self_auto_artifacts
        
        context_objects = ContextObjectsModel()

        for filename, obj in all_artifacts.items():
            # Skip objects that ended up unused in the trace
            obj_id = id(obj)
            if obj_id not in self._self_obj_to_name_map:
                continue

            # Map context name -> filename
            context_name = self._self_obj_to_name_map[obj_id].split(".")[-1]
            obj_type_name = CLASS_NAME_MAP[type(obj).__name__]

            if hasattr(context_objects, obj_type_name):
                getattr(context_objects, obj_type_name)[context_name] = filename

            # Write binary file
            self._self_write_ssz(os.path.join(output_dir, filename), obj)

        # 2. Write YAML files
        self._self_write_yaml(
            os.path.join(output_dir, "trace.yaml"),
            TraceModel(
                metadata=self._self_metadata,
                context=ContextModel(
                    fixtures=self._self_context_fixture_names,
                    parameters=self._self_parameters,
                    objects=context_objects,
                ),
                trace=self._self_trace_steps,
            ).model_dump(exclude_none=True)
        )

        if self._self_metadata:
            self._self_write_yaml(
                os.path.join(output_dir, "meta.yaml"),
                MetaModel(meta=self._self_metadata).model_dump()
            )

        print(f"[Trace Recorder] Saved artifacts to {output_dir}")

    def _self_write_ssz(self, path: str, obj: Any) -> None:
        try:
            with open(path, "wb") as f:
                f.write(ssz_serialize(obj))
        except Exception as e:
            print(f"ERROR: Failed to write SSZ artifact {path}: {e}")

    def _self_write_yaml(self, path: str, data: Any) -> None:
        try:
            with open(path, "w") as f:
                yaml.dump(data, f, sort_keys=False, default_flow_style=False)
        except Exception as e:
            print(f"ERROR: Failed to write YAML {path}: {e}")
