import random

from eth2spec.test.context import (
    spec_state_test,
    with_all_phases_from_to,
)
from eth2spec.test.helpers.blob import (
    get_max_blob_count,
    get_sample_blob_tx,
)
from eth2spec.test.helpers.block import (
    build_empty_block_for_next_slot,
)
from eth2spec.test.helpers.constants import (
    DENEB,
    GLOAS,
)
from eth2spec.test.helpers.execution_payload import (
    compute_el_block_hash,
    get_random_tx,
)
from eth2spec.test.helpers.state import (
    state_transition_and_sign_block,
)

from eth2spec.test.context import (
    record_spec_trace,
    with_all_phases,
)

def linear_run_block_with_blobs(
    spec,
    state,
    blob_count,
    tx_count=1,
    blob_gas_used=1,
    excess_blob_gas=1,
    non_blob_tx_count=0,
    rng=random.Random(7777),
    valid=True,
):
    spec.ssz("pre_state.ssz", state)

    block = build_empty_block_for_next_slot(spec, state)
    txs = []
    blob_kzg_commitments = []
    for _ in range(tx_count):
        opaque_tx, _, commits, _ = get_sample_blob_tx(spec, blob_count=blob_count)
        txs.append(opaque_tx)
        blob_kzg_commitments += commits

    for _ in range(non_blob_tx_count):
        txs.append(get_random_tx(rng))

    rng.shuffle(txs)

    block.body.blob_kzg_commitments = blob_kzg_commitments
    block.body.execution_payload.transactions = txs
    block.body.execution_payload.blob_gas_used = blob_gas_used
    block.body.execution_payload.excess_blob_gas = excess_blob_gas
    block.body.execution_payload.block_hash = compute_el_block_hash(
        spec, block.body.execution_payload, state
    )

    if valid:
        signed_block = state_transition_and_sign_block(spec, state, block)
    else:
        signed_block = state_transition_and_sign_block(spec, state, block, expect_fail=True)

    spec.ssz("blocks.ssz", [signed_block])
    spec.ssz("post_state.ssz", state if valid else None)


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_zero_blob(spec, state):
    linear_run_block_with_blobs(spec, state, blob_count=0)


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_one_blob(spec, state):
    linear_run_block_with_blobs(spec, state, blob_count=1)


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_one_blob_two_txs(spec, state):
    linear_run_block_with_blobs(spec, state, blob_count=1, tx_count=2)


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_one_blob_max_txs(spec, state):
    linear_run_block_with_blobs(
        spec, state, blob_count=1, tx_count=get_max_blob_count(spec, state)
    )


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_invalid_one_blob_max_plus_one_txs(spec, state):
    linear_run_block_with_blobs(
        spec, state, blob_count=1, tx_count=get_max_blob_count(spec, state) + 1, valid=False
    )


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_max_blobs_per_block(spec, state):
    linear_run_block_with_blobs(spec, state, blob_count=get_max_blob_count(spec, state))


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_invalid_max_blobs_per_block_two_txs(spec, state):
    linear_run_block_with_blobs(
        spec, state, blob_count=get_max_blob_count(spec, state), tx_count=2, valid=False
    )


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_invalid_exceed_max_blobs_per_block(spec, state):
    linear_run_block_with_blobs(
        spec, state, blob_count=get_max_blob_count(spec, state) + 1, valid=False
    )


@with_all_phases_from_to(DENEB, GLOAS)
@spec_state_test
@record_spec_trace
def test_e_mix_blob_tx_and_non_blob_tx(spec, state):
    linear_run_block_with_blobs(spec, state, blob_count=1, tx_count=1, non_blob_tx_count=1)
