"""
An example of a test refactored to use the new `@traced_test` decorator.
This test is linear and does not use `yield`.
"""

from eth2spec.test.context import (
    record_spec_trace,
    # Note: spec_state_test is no longer imported
    with_all_phases,
    spec_state_test,
)


@with_all_phases
@spec_state_test
@record_spec_trace
def test_example_block_processing(spec, state):
    """
    A simple test that processes a block.
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

    # 3. Just call the spec. This is auto-recorded.
    # We advance the state to the next slot
    spec.process_slot(state)

    # 4. Manually save the pre-state if needed
    spec.ssz("pre_state.ssz", state)

    # 5. Create and process a block
    block = spec.BeaconBlock(
        slot=state.slot,
        proposer_index=0,
        parent_root=spec.get_block_root_at_slot(state, state.slot - 1),
        body=spec.BeaconBlockBody(),
    )

    # This call is also auto-recorded as a trace step
    spec.process_block(state, block)

    # 6. Replace `yield "post.ssz", ...`
    spec.ssz("post_state.ssz", state)
