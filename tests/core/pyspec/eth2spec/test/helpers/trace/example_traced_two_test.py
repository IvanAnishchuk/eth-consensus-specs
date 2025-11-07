"""
This file is a port of the `test_sanity_slots` test to the new
linear, non-yield-based tracing system.
It serves as the simplest "hello world" for the new framework.
"""

from eth2spec.test.context import (
    record_spec_trace,
    spec_state_test,
    with_all_phases,
)


@with_all_phases
@spec_state_test
@record_spec_trace
def test_linear_sanity_slots(spec, state):
    """
    Run a sanity test checking that `process_slot` works.
    This demonstrates the simplest possible state transition.
    """
    # 1. Register the pre-state
    spec.ssz("pre_state.ssz", state)

    # 2. Advance the state by one slot
    # We must re-assign the `state` variable, as `process_slot`
    # is a pure function that returns a new, modified state.
    state = spec.process_slot(state)

    # 3. Register the post-state
    spec.ssz("post_state.ssz", state)
