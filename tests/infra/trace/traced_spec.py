"""
Traced Spec Proxy
-----------------
A wrapper around the spec object that records all interactions.

It uses `wrapt.ObjectProxy` to intercept function calls, recording their:
1. Arguments (sanitized and mapped to context variables)
2. Return values
3. State context (injecting 'load_state' when the context switches)
"""

import inspect
from pathlib import Path
from typing import Any

import wrapt
from remerkleable.complex import Container

from eth2spec.utils.ssz.ssz_typing import View

from .models import (
    # NON_SSZ_FIXTURES,
    TraceModel,
    TraceStepModel,
    AssertStateStepModel,
    LoadStateStepModel,
    SpecCallStepModel,
    dump_to_dir,
    write_ssz_artifact,
)


def ssz_object_to_filename(obj: View) -> str:
    """
    Registers an object in the trace context.
    - If it's an SSZ object (Container), it gets a hash-based name.
    - If it's a primitive, it returns None (passed through).
    """
    # FIXME: we should use raw root hashes as filenames, we probably don't need this logic, but we still want to only use ssz serialization for containers not primitive types...
    # Use View to determine whether it's an SSZ object
    print("ssz_object_to_filename called with:", type(obj))
    if not isinstance(obj, View):
        return None
    # FIXME: some primitive subclasses might still be Views? we don't want to ssz every single thing...
    if isinstance(obj, bytes):
        None
    if isinstance(obj, int):
        None

    obj_type = type(obj).__name__.lower()

    # FIXME: let's whitelist for now but this needs a better solution
    if obj_type not in ['beaconstate', 'attestation', 'beaconblock']:
         return None

    # Generate Name (Content-Addressed by raw root hash)
    root_hex = obj.hash_tree_root().hex()
    filename = root_hex

    return filename


class RecordingSpec(wrapt.ObjectProxy):
    """
    A proxy that wraps the 'spec' object to record execution traces.

    It automatically handles state versioning and deduplication.
    It automatically intercepts all other function calls.
    """

    # Internal state
    _model: TraceModel
    # FIXME Perhaps _self thing is unnecessary after all? Just underscore it...
    _self_config_data: dict[str, Any]  # FIXME is this used?
    _self_last_state_root: str | None  # TODO rename to last_state_root probably

    def __init__(
        self,
        wrapped_spec: Any,
        # initial_context_fixtures: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
    ):
        super().__init__(wrapped_spec)

        self._self_config_data = {}
        self._self_state_last_root = None

        # FIXME let's just add parameters to metadata dict? to be reviewed
        self._model = TraceModel(metadata=(metadata or {}) | {"parameters": parameters or {}})

    # --- Interception Logic ---

    def __getattr__(self, name: str) -> Any:
        """
        Intercepts attribute access on the spec object.
        If the attribute is a callable (function), it is wrapped to record execution.
        """
        # 1. Access recorder's own methods first
        if name in ("save_trace", "finalize"):
            return object.__getattribute__(self, name)

        # 2. Retrieve the real attribute from the wrapped spec
        real_attr = super().__getattr__(name)

        # 3. If it's not a function or shouldn't be traced, return as-is
        if not callable(real_attr) or not name.islower() or name.startswith("_"):
            return real_attr

        # 4. Return the recording wrapper
        return self._self_create_wrapper("spec_call", name, real_attr)

    # FIXME: there might be another way: we can inspect the spec and wrap all functions at init time

    def _self_create_wrapper(self, op_name: str, method: str, real_func: callable) -> Any:
        """Creates a closure to record the function call."""

        def record_wrapper(*args: Any, **kwargs: Any) -> Any:
            # A. Prepare arguments: bind to signature and serialize
            bound_args = self._self_bind_args(real_func, args, kwargs)

            # Process arguments and auto-register any NEW SSZ objects as artifacts
            serial_params = {k: self._self_process_arg(v) for k, v in bound_args.arguments.items()}

            # B. Identify State object and handle Context Switching
            # FIXME: why is this called old_hash? it's not really old, but it's captured before the call and then we have result after the call
            state_obj, old_hash = self._self_capture_pre_state(bound_args)

            if old_hash is not None:
                current_root_hex = old_hash.hex()
                # If the state passed to this function is different from the last one we saw,
                # inject a `load_state` operation to switch context.
                if self._self_state_last_root != current_root_hex:
                    # Handle out-of-band mutation:
                    # The model's register_object logic handles re-registration if hash changed

                    # Ensure the state is registered with its current hash
                    state_var = self._self_process_arg(state_obj)

                    if state_var:
                        self._model.trace.append(
                            # FIXME: probably a different class so there's no weird empty method here
                            # FIXME: Not sure about params here, we probably want to pass the root in params? but then result will be redundant... it's filename which is basically the same hash - recheck format in the issue
                            LoadStateStepModel(state_root=current_root_hex)
                        )
                        name = ssz_object_to_filename(state_obj)
                        # FIXME: ^^ isn't this already done in _self_process_arg?
                        self._model._artifacts[name] = state_obj
                        # FIXME: we are removing the mappings and just using the root hashes directly now
                        self._self_state_last_root = current_root_hex

            # C. Execute the real function
            try:
                result = real_func(*args, **kwargs)
                error = None
            except Exception as e:
                result = None
                error = {"type": type(e).__name__, "message": str(e)}
                # We must record the step before re-raising
                self._self_record_step(op_name, method, serial_params, result, error)
                raise e

            # D. Record the successful step
            self._self_record_step(op_name, method, serial_params, result, None)

            # E. Update tracked state if mutated
            if state_obj is not None:
                self._self_update_state_tracker(state_obj, old_hash)

            return result

        return record_wrapper

    def _self_bind_args(self, func: Any, args: tuple, kwargs: dict) -> inspect.BoundArguments:
        """
        Binds positional and keyword arguments to the function signature.

        We do this because we often use positional arguments in spec tests,
        but for recording we want to have a consistent mapping of parameter names to values.
        """
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return bound

    # FIXME: typing for state_obj is tricky because specific implementation is in the spec
    # TODO: use View
    def _self_capture_pre_state(
        self, bound_args: inspect.BoundArguments
    ) -> tuple[Container | None, bytes | None]:
        """Finds the BeaconState argument (if any) and captures its root hash."""
        state_obj = None

        # Look for 'state' in arguments
        state_obj = bound_args.arguments.get("state")

        # If found, capture the hash
        # Use duck typing for hash_tree_root to support Mocks
        if state_obj and hasattr(state_obj, "hash_tree_root"):
            return state_obj, state_obj.hash_tree_root()

        return None, None

    def _self_record_step(
        self, op: str, method: str, params: dict, result: Any, error: dict | None
    ) -> None:
        """Appends a step to the trace."""
        # Auto-register the result if it's an SSZ object (by calling process_arg)
        serialized_result = self._self_process_arg(result) if result is not None else None

        # Create the model to validate and sanitize data (bytes->hex, etc.)
        step_model = SpecCallStepModel(
            op=op, method=method, input=params, output=serialized_result, error=error
        )
        self._model.trace.append(step_model)

    # TODO perhaps add a record_state_mutation method for load_state and similar

    def _self_update_state_tracker(
        self,
        state_obj: Container,
        old_hash: bytes | None,
    ) -> None:
        """Updates the internal state tracker if the state object was mutated."""
        # FIXME: we are doing this check in multiple places, perhaps unify
        if not hasattr(state_obj, "hash_tree_root") or old_hash is None:
            return

        new_hash = state_obj.hash_tree_root()
        new_root_hex = new_hash.hex()

        # Always update the last root to the current state's new root
        # This ensures subsequent operations know what the current state is.
        self._self_last_state_root = new_root_hex

        if old_hash == new_hash:
            return  # No content change

        # State changed: Register the new state version in the model
        # This updates the mapping so future calls with this object ID get the new name
        self._self_process_arg(state_obj)

    def _self_process_arg(self, arg: Any) -> Any:
        """
        FIXME: we are changing logic here
        Delegates to TraceModel to register objects/artifacts.
        Returns the context variable string or the original primitive.
        """
        # FIXME: these checks are super redundant
        if not isinstance(arg, View):
            return arg
        # TODO: dump SSZ artifacts right away
        # just use hashes and keep this simple
        ssz_filename = ssz_object_to_filename(arg)
        if ssz_filename:  # and ssz_filename not in NON_SSZ_FIXTURES:
            print("Registering SSZ object as artifact:", ssz_filename)
            self._model._artifacts[ssz_filename] = arg
            # TODO dump here
            return ssz_filename

        # If register_object returns None, it's a primitive (or unknown type)
        # Pass it through for Pydantic to handle
        return arg

    def _self_record_auto_assert_step(self) -> None:
        """Appends assert_state step to the trace."""
        # Auto-register last state root in assert_state step

        step_model = AssertStateStepModel(state_root=self._self_last_state_root)
        self._model.trace.append(step_model)

    def finalize(self) -> None:
        self._self_record_auto_assert_step()

    # FIXME: probably not doing this anymore, cleanup
    def save_trace(self, output_dir: Path) -> None:
        """
        Writes the captured trace and artifacts to the filesystem.
        Delegates the actual writing to the helper function.
        """
        # TODO: add assert_state in the end based on last known state
        dump_to_dir(self._model, output_dir)
