import functools
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

# from tests.infra.trace.models import CLASS_NAME_MAP, NON_SSZ_FIXTURES
from tests.infra.trace.traced_spec import RecordingSpec

DEFAULT_TRACE_DIR = Path("traces").resolve()  # FIXME: better default? TODO: probably move constant to traced_spec and import here

# TODO simplify this module, it can be very simple


def _get_trace_output_dir(
    fn: Callable,
    real_spec: Any,  # FIXME typing
) -> Path:
    """Calculates the output directory path for the trace artifacts."""

    test_module = fn.__module__.split(".")[-1]
    test_name = fn.__name__
    fork_name = real_spec.fork
    preset_name = real_spec.config.PRESET_BASE

    path = DEFAULT_TRACE_DIR / fork_name / preset_name / test_module / test_name
    # TODO: handle parameterization?

    return path


def record_spec_trace(fn: Callable) -> Callable:
    """
    Decorator to wrap a pyspec test and record execution traces.
    Usage:
        @with_all_phases  # or other decorators
        @spec_state_test  # still needed as before
        @record_spec_trace  # new decorator to record trace
        def test_my_feature(spec, ...):
            ...
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # 1. Bind arguments to find 'spec' and fixtures
        try:
            bound_args = inspect.signature(fn).bind(*args, **kwargs)
            bound_args.apply_defaults()
        except TypeError:
            # FIXME: simplify?
            # Fallback for non-test invocations
            return fn(*args, **kwargs)

        if "spec" not in bound_args.arguments:
            return fn(*args, **kwargs)

        real_spec = bound_args.arguments["spec"]

        metadata = {
            "fork": real_spec.fork,
            "preset": real_spec.config.PRESET_BASE,
        }

        # FIXME: we don't actually properly parametrize these, perhaps remove and simplify for now
        parameters = {
            k: v
            for k, v in bound_args.arguments.items()
            if isinstance(v, (int, str, bool, type(None)))
        }

        # 3. Inject the recorder
        recorder = RecordingSpec(real_spec, metadata=metadata, parameters=parameters)
        bound_args.arguments["spec"] = recorder

        # 4. Run test & Save trace
        try:
            return fn(*bound_args.args, **bound_args.kwargs)
        finally:
            try:
                # Use the *original* spec's fork name for the path
                artifact_dir = _get_trace_output_dir(fn, real_spec)

                print(f"\n[Trace Recorder] Saving trace for {fn.__name__} to: {artifact_dir}")
                recorder.save_trace(artifact_dir)
            except Exception as e:
                print(f"[Trace Recorder] ERROR: FAILED to save trace for {fn.__name__}: {e}")

    return wrapper
