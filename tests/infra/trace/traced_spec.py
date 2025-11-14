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
from typing import Any

import wrapt
import yaml

from eth2spec.utils.ssz.ssz_impl import serialize as ssz_serialize

from .trace_models import (
    CLASS_NAME_MAP,
    ConfigModel,
    NON_SSZ_FIXTURES,
    TraceModel,
    TraceStepModel,
)


class RecordingSpec(wrapt.ObjectProxy):
    """
    A proxy that wraps the 'spec' object to record execution traces.

    It automatically handles state versioning and deduplication.
    It automatically intercepts all other function calls.
    """

    # Internal state
    _model: TraceModel
    _self_config_data: dict[str, Any]
    _self_last_root: str | None

    def __init__(
        self,
        wrapped_spec: Any,
        initial_context_fixtures: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
    ):
        super().__init__(wrapped_spec)

        self._self_config_data = {}
        self._self_last_root = None

        self._model = TraceModel(
            metadata=metadata or {},
            context={"parameters": parameters or {}}
        )

        # Register initial fixtures
        for name, obj in initial_context_fixtures.items():
            self._self_register_fixture(name, obj)

    def _self_register_fixture(self, name: str, obj: Any) -> None:
        """Registers an initial fixture in the recording context."""
        class_name = type(obj).__name__
        if class_name not in CLASS_NAME_MAP:
            if name in NON_SSZ_FIXTURES:
                self._model.context.fixtures.append(name)
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
            serial_params = {k: self._self_process_arg(v, auto_artifact=True) 
                             for k, v in bound_args.arguments.items()}

            # B. Identify State object and handle Context Switching
            state_obj, old_hash, old_id = self._self_capture_pre_state(bound_args)
            
            if old_hash is not None:
                current_root_hex = old_hash.hex()
                # If the state passed to this function is different from the last one we saw,
                # inject a `load_state` operation to switch context.
                if self._self_last_root != current_root_hex:
                    # Handle out-of-band mutation:
                    # The model's register_object logic handles re-registration if hash changed
                    expected_name_suffix = f".{current_root_hex}"
                    
                    # Ensure the state is registered with its current hash
                    state_var = self._self_process_arg(state_obj, auto_artifact=True)

                    if state_var:
                        self._model.trace.append(TraceStepModel(
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
        # Auto-register the result if it's an SSZ object (by calling process_arg)
        serialized_result = self._self_process_arg(result, auto_artifact=True) if result is not None else None
        
        # Create the model to validate and sanitize data (bytes->hex, etc.)
        step_model = TraceStepModel(
            op=op,
            params=params,
            result=serialized_result,
            error=error
        )
        self._model.trace.append(step_model.model_dump(exclude_none=True))

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

        # State changed: Register the new state version in the model
        # This updates the mapping so future calls with this object ID get the new name
        self._self_process_arg(state_obj, auto_artifact=True)

    def _self_process_arg(
        self, arg: Any, preferred_name: str | None = None, auto_artifact: bool = False
    ) -> Any:
        """
        Delegates to TraceModel to register objects/artifacts.
        Returns the context variable string or the original primitive.
        """
        # Delegate registration to the model
        context_name = self._model.register_object(arg, preferred_name)
        if context_name:
            return context_name
        
        # If register_object returns None, it's a primitive (or unknown type)
        # Pass it through for Pydantic to handle
        return arg

    def save_trace(self, output_dir: str) -> None:
        """
        Writes the captured trace and artifacts to the filesystem.
        """
        os.makedirs(output_dir, exist_ok=True)

        # 1. Write SSZ artifacts
        for filename, obj in self._model._artifacts.items():
            self._self_write_ssz(os.path.join(output_dir, filename), obj)

        # 2. Write YAML files
        self._self_write_yaml(
            os.path.join(output_dir, "trace.yaml"),
            self._model.model_dump(exclude_none=True)
        )

        if self._self_config_data:
            self._self_write_yaml(
                os.path.join(output_dir, "config.yaml"),
                ConfigModel(config=self._self_config_data).model_dump()
            )

        # Meta is usually part of trace.yaml 'metadata' now

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
