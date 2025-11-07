"""
An example of a test refactored to use the new `@traced_test` decorator.
This test is linear and does not use `yield`.
"""

from eth2spec.test.context import (
    record_spec_trace,
    spec_state_test,
    with_all_phases,
)


@with_all_phases
@spec_state_test
@record_spec_trace
def test_example_block_processing(spec, state):
    """
    A simple test that processes a block.
    The state starts at slot 0. We build a block for slot 1.
    """
    # 1. Replace `yield "config", ...`
    spec.config(
        {
            "PRESET_BASE": "mainnet",
            "PHASE_0_FORK_EPOCH": 0,
        }
    )

    # 2. Replace `yield "meta", ...`
    spec.meta("description", "A test showing linear block processing")

    # 3. --- FIX: Save the pre-state at slot 0 ---
    spec.ssz("pre_state.ssz", state)

    # 4. --- FIX: Define block for slot 1 ---
    block_slot = state.slot + 1

    # 5. --- FIX: Get parent root from state.slot (which is 0) ---
    parent_root = spec.get_block_root_at_slot(state, state.slot)

    block = spec.BeaconBlock(
        slot=block_slot, proposer_index=0, parent_root=parent_root, body=spec.BeaconBlockBody()
    )

    # 6. --- FIX: process_block advances the state to slot 1 ---
    # This call is auto-recorded as a trace step
    spec.process_block(state, block)

    # 7. Replace `yield "post.ssz", ...`
    # This state is now at slot 1
    spec.ssz("post_state.ssz", state)
