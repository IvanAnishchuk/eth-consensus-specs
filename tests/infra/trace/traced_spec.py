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
from eth2spec.utils.ssz.ssz_impl import serialize as ssz_serialize

from .models import (
    TraceModel,
    TraceStepModel,
    AssertStateStepModel,
    LoadStateStepModel,
    SpecCallStepModel,
)


def ssz_root_hex(obj: View) -> str:
    """
    Check that object is one of those we want to ssz and return root hash.

    This is to avoid using ssz files on simple primitive values we can serialize directly.
    """
    # Use View to determine whether it's an SSZ object
    if not isinstance(obj, View):
        return None

    # whitelist data types FIXME: find a better solution
    obj_type = type(obj).__name__.lower()
    if obj_type not in ['beaconstate', 'attestation', 'beaconblock']:
         return None

    # Generate Name (Content-Addressed by raw root hash)
    return obj.hash_tree_root().hex()

class RecordingSpec(wrapt.ObjectProxy):
    """
    A proxy that wraps the 'spec' object to record execution traces.

    It automatically handles state versioning and deduplication.
    It automatically intercepts all other function calls.
    """

    # Internal state
    _model: TraceModel
    _last_state_root: str | None

    def __init__(self, wrapped_spec: Any):
        super().__init__(wrapped_spec)

        self._last_state_root = None

        self._model = TraceModel()

    # --- Interception Logic ---

    def __getattr__(self, name: str) -> Any:
        """
        Intercepts attribute access on the spec object.
        If the attribute is a callable (function), it is wrapped to record execution.
        """
        # 1. Access recorder's own methods first
        if name == "finalize":
            return object.__getattribute__(self, name)

        # 2. Retrieve the real attribute from the wrapped spec
        real_attr = super().__getattr__(name)

        # 3. If it's not a function or shouldn't be traced, return as-is
        if not callable(real_attr) or not name.islower() or name.startswith("_"):
            return real_attr

        # 4. Return the recording wrapper
        return self._create_wrapper("spec_call", name, real_attr)

    # FIXME: there might be another way: we can inspect the spec and wrap all functions at init time

    def _create_wrapper(self, op_name: str, method: str, real_func: callable) -> Any:
        """Creates a closure to record the function call."""

        def record_wrapper(*args: Any, **kwargs: Any) -> Any:
            # A. Prepare arguments: bind to signature and serialize
            bound_args = self._bind_args(real_func, args, kwargs)

            # Process arguments and auto-register any NEW SSZ objects as artifacts
            serial_params = {k: self._process_arg(v) for k, v in bound_args.arguments.items()}

            # B. Identify State object and handle Context Switching
            self._capture_pre_state(state := serial_params.get("state"))

            # C. Execute the real function
            result = real_func(*args, **kwargs)

            # D. Record the successful step
            self._record_step(op_name, method, serial_params, result)

            # E. Update tracked state if mutated
            if state is not None:
                self._capture_post_state(state)

            return result

        return record_wrapper

    def _bind_args(self, func: callable, args: tuple, kwargs: dict) -> inspect.BoundArguments:
        """
        Binds positional and keyword arguments to the function signature.

        We do this because we often use positional arguments in spec tests,
        but for recording we want to have a consistent mapping of parameter names to values.
        """
        sig = inspect.signature(func)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return bound

    def _capture_pre_state(self, state: View | None) -> None:
        """Finds the BeaconState argument (if any) and captures its root hash."""
        if isinstance(state, View):
            if self._last_state_root != (state_root_hex := state.hash_tree_root().hex()):
                # Handle out-of-band mutation:
                self._model.trace.append(LoadStateStepModel(state_root=state_root_hex))
                self._last_state_root = state_root_hex


    def _record_step(
        self, op: str, method: str, params: dict, result: Any,
    ) -> None:
        """Appends a step to the trace."""
        # Auto-register the result if it's an SSZ object (by calling process_arg)
        serialized_result = self._process_arg(result) if result is not None else None

        # Create the model to validate and sanitize data (bytes->hex, etc.)
        step_model = SpecCallStepModel(
            op=op, method=method, input=params, output=serialized_result,
        )
        self._model.trace.append(step_model)

    # TODO perhaps add a record_state_mutation method for load_state and similar

    def _capture_post_state(
        self,
        state: View,
    ) -> None:
        """Updates the internal state tracker if the state object was mutated."""
        # FIXME: we are doing this check in multiple places, confirm
        if not hasattr(state, "hash_tree_root") or self._last_state_root is None:
            return

        new_root = state.hash_tree_root().hex()

        if self._last_state_root == new_root:
            return  # No content change

        self._last_state_root = new_root
        self._process_arg(state)

    def _process_arg(self, arg: Any) -> Any:
        """
        Process a potential container.
        Returns the root hash of container or the original primitive.
        """
        if ssz_hash := ssz_root_hex(arg):
            print("Registering SSZ object as artifact:", ssz_hash)
            self._model._artifacts[ssz_hash] = ssz_serialize(arg)
            return ssz_hash

        # If register_object returns None, it's a primitive (or unknown type)
        # Pass it through for Pydantic to handle
        return arg

    def _record_auto_assert_step(self) -> None:
        """Appends assert_state step to the trace."""
        # Auto-register last state root in assert_state step

        if self._last_state_root:
            step_model = AssertStateStepModel(state_root=self._last_state_root)
            self._model.trace.append(step_model)

    def finalize(self) -> None:
        self._record_auto_assert_step()
