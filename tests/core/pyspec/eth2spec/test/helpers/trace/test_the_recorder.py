"""
This file unit-tests the trace recorder itself using
pytest fixtures and unittest.mock.

This approach is faster and more isolated than using 'pytester'.
"""

import os
from typing import Any
from unittest.mock import mock_open, patch

import pytest

# --- Imports from our framework ---
from .trace_models import TraceModel
from .traced_spec import CLASS_NAME_MAP, NON_SSZ_FIXTURES, RecordingSpec

# --- Mocks for eth2spec objects ---
# We create minimal mocks of the real objects to test the
# recorder's logic in isolation.


class MockState:
    """Mocks a BeaconState"""

    def __init__(self, root: bytes):
        self._root = root

    def hash_tree_root(self) -> bytes:
        return self._root


class MockBlock:
    """Mocks a BeaconBlock"""

    def __init__(self, root: bytes):
        self._root = root

    def hash_tree_root(self) -> bytes:
        return self._root


# --- FIXED: Added MockAttestation ---
class MockAttestation:
    """Mocks an Attestation"""

    def __init__(self, root: bytes):
        self._root = root

    def hash_tree_root(self) -> bytes:
        return self._root


# ------------------------------------


class MockStore:
    """Mocks a Store object (non-SSZ)"""

    pass


class MockSpec:
    """Mocks the 'spec' object"""

    def tick(self, state: MockState, slot: int) -> None:
        # Simulate a state change
        state._root = state._root + b"\x01"

    def process_block(self, state: MockState, block: MockBlock) -> None:
        # Simulate a no-op (no state change)
        pass

    def BeaconBlock(self, body: Any = None) -> MockBlock:
        # Simulate new object creation
        return MockBlock(root=b"\xaa" * 32)

    # --- FIXED: Added mock Attestation method ---
    def Attestation(self, data: Any = None) -> MockAttestation:
        # Simulate new object creation
        return MockAttestation(root=b"\xcc" * 32)

    # ------------------------------------------

    def is_valid_merkle_branch(
        self, leaf: bytes, branch: list[bytes], depth: int, index: int, root: bytes
    ) -> bool:
        return True

    def get_current_epoch(self, state: MockState) -> int:
        return 0

    def get_head(self, store: MockStore) -> bytes:
        return b"\xdd" * 32


# --- End Mocks ---

# Update the real CLASS_NAME_MAP to know about our mocks
# This is a common pattern in testing
REAL_CLASS_MAP = CLASS_NAME_MAP.copy()
CLASS_NAME_MAP[MockState.__name__] = "states"
CLASS_NAME_MAP[MockBlock.__name__] = "blocks"
# --- FIXED: Added attestation to map ---
CLASS_NAME_MAP[MockAttestation.__name__] = "attestations"
# ---------------------------------------

REAL_NON_SSZ_FIXTURES = NON_SSZ_FIXTURES.copy()
NON_SSZ_FIXTURES.add(MockStore.__name__)


@pytest.fixture
def mock_state() -> MockState:
    """A fixture for a mock state object"""
    return MockState(root=b"\x00" * 32)


@pytest.fixture
def mock_block() -> MockBlock:
    """A fixture for a mock block object"""
    return MockBlock(root=b"\xbb" * 32)


@pytest.fixture
def mock_store() -> MockStore:
    """A fixture for a mock store object"""
    return MockStore()


# --- FIXED: Added attestation fixture ---
@pytest.fixture
def mock_attestation() -> MockAttestation:
    """A fixture for a mock attestation object"""
    return MockAttestation(root=b"\xdd" * 32)


# ----------------------------------------


@pytest.fixture
def recording_spec(
    mock_state: MockState, mock_block: MockBlock, mock_store: MockStore
) -> RecordingSpec:
    """
    This is the core fixture for our tests.
    It yields an initialized RecordingSpec proxy.
    """
    # 1. Create the real (mocked) spec
    mock_spec = MockSpec()

    # 2. Define the initial fixtures, just like conftest.py would
    initial_fixtures = {
        "state": mock_state,
        "genesis_block": mock_block,
        "store": mock_store,
    }

    # 3. Create the proxy and yield it
    proxy = RecordingSpec(mock_spec, initial_fixtures)
    yield proxy

    # --- Cleanup ---
    # Restore the real maps after the test
    CLASS_NAME_MAP.clear()
    CLASS_NAME_MAP.update(REAL_CLASS_MAP)
    NON_SSZ_FIXTURES.clear()
    NON_SSZ_FIXTURES.update(REAL_NON_SSZ_FIXTURES)


# --- The Tests ---


def test_init_serializes_fixtures(recording_spec: RecordingSpec):
    """
    Tests that the __init__ method correctly serializes
    the initial fixtures.
    """
    proxy = recording_spec  # Get the initialized proxy

    # Check SSZ objects
    assert "$context.states.initial" in proxy._self_name_to_obj_map
    assert "$context.blocks.genesis_block" in proxy._self_name_to_obj_map

    # Check non-SSZ fixtures
    assert "store" in proxy._self_context_fixture_names

    # Check artifacts to be written
    assert "state_v0.ssz" in proxy._self_artifacts_to_write
    assert "blocks_genesis_block.ssz" in proxy._self_artifacts_to_write


@patch("yaml.dump")
@patch("builtins.open", new_callable=mock_open)
def test_state_change_is_recorded(
    m_open: Any, m_yaml_dump: Any, recording_spec: RecordingSpec, mock_state: MockState
):
    """
    Tests the most important branch: a function call that
    mutates the state.
    """
    proxy = recording_spec

    # 1. Call the function
    proxy.tick(state=mock_state, slot=1)

    # 2. Check internal trace steps
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "tick"
    assert step["params"]["state"] == "$context.states.initial"
    assert step["result"] == "$context.states.v0"  # The *new* state name

    # 3. Check that a new artifact was marked for saving
    assert "state_v0.ssz" in proxy._self_artifacts_to_write  # From init
    assert "state_v1.ssz" in proxy._self_artifacts_to_write  # From tick

    # 4. Check that 'save_trace' writes the correct files
    proxy.save_trace("mock_trace.yaml")

    # 4a. Check SSZ files
    m_open.assert_any_call(os.path.join(os.getcwd(), "state_v1.ssz"), "wb")

    # 4b. Check YAML file
    m_open.assert_any_call("mock_trace.yaml", "w")

    # 4c. Check YAML content
    final_data = m_yaml_dump.call_args[0][0]  # Get the data passed to yaml.dump
    trace_model = TraceModel(**final_data)  # Validate with Pydantic

    assert trace_model.context.objects.states["v1"] == "state_v1.ssz"
    assert trace_model.trace[0].result == "$context.states.v0"


@patch("yaml.dump")
@patch("builtins.open", new_callable=mock_open)
def test_no_state_change_is_recorded(
    m_open: Any,
    m_yaml_dump: Any,
    recording_spec: RecordingSpec,
    mock_state: MockState,
    mock_block: MockBlock,
):
    """
    Tests that a no-op call is recorded correctly
    (i.e., it reuses the old state name as the result).
    """
    proxy = recording_spec

    # 1. Call the function
    proxy.process_block(state=mock_state, block=mock_block)

    # 2. Check internal trace steps
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "process_block"
    assert step["params"]["state"] == "$context.states.initial"
    assert step["params"]["block"] == "$context.blocks.genesis_block"
    assert step["result"] == "$context.states.initial"  # The *old* state name

    # 3. Check artifacts
    # No new state artifact should be added
    assert len(proxy._self_artifacts_to_write) == 2  # Only state_v0 and block_genesis
    assert "state_v1.ssz" not in proxy._self_artifacts_to_write

    # 4. Check YAML content
    proxy.save_trace("mock_trace.yaml")
    final_data = m_yaml_dump.call_args[0][0]
    trace_model = TraceModel(**final_data)

    assert "v1" not in trace_model.context.objects.states
    assert trace_model.trace[0].result == "$context.states.initial"


def test_new_ssz_object_is_recorded(recording_spec: RecordingSpec):
    """
    Tests that a function call that *returns* a new SSZ
    object is recorded correctly.
    """
    proxy = recording_spec

    # 1. Call the function
    new_block = proxy.BeaconBlock(body=None)

    # 2. Check internal trace steps
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "BeaconBlock"
    assert step["result"] == "$context.blocks.b0"  # The new block name

    # 3. Check artifacts
    assert "blocks_b0.ssz" in proxy._self_artifacts_to_write
    assert proxy._self_artifacts_to_write["blocks_b0.ssz"] is new_block


# --- FIXED: Added test for Attestation ---
@patch("yaml.dump")
@patch("builtins.open", new_callable=mock_open)
def test_new_ssz_object_attestation(m_open: Any, m_yaml_dump: Any, recording_spec: RecordingSpec):
    """
    Tests that a new object of a *different* type (Attestation)
    is also recorded correctly.
    """
    proxy = recording_spec

    # 1. Call the function
    new_att = proxy.Attestation(data=None)

    # 2. Check internal trace steps
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "Attestation"
    assert step["result"] == "$context.attestations.b0"  # The new attestation name

    # 3. Check artifacts
    assert "attestations_b0.ssz" in proxy._self_artifacts_to_write
    assert proxy._self_artifacts_to_write["attestations_b0.ssz"] is new_att

    # 4. Check YAML content
    proxy.save_trace("mock_trace.yaml")
    final_data = m_yaml_dump.call_args[0][0]
    trace_model = TraceModel(**final_data)

    # This assertion verifies our Pydantic model bugfix
    assert trace_model.context.objects.attestations["b0"] == "attestations_b0.ssz"
    assert trace_model.trace[0].result == "$context.attestations.b0"


# ----------------------------------------


def test_literal_args_and_return_are_recorded(recording_spec: RecordingSpec):
    """
    Tests that simple literals (int, str, bool, bytes)
    are serialized directly into the YAML.
    """
    proxy = recording_spec

    # 1. Call function with literals
    proxy.is_valid_merkle_branch(
        leaf=b"\x00" * 32, branch=[b"\x01" * 32], depth=3, index=2, root=b"\x02" * 32
    )

    # 2. Call function with simple return
    epoch = proxy.get_current_epoch(state=proxy._self_name_to_obj_map["$context.states.initial"])

    # 3. Check trace steps
    assert len(proxy._self_trace_steps) == 2

    # Step 1: is_valid_merkle_branch
    step1 = proxy._self_trace_steps[0]
    assert step1["op"] == "is_valid_merkle_branch"
    assert step1["params"]["depth"] == 3
    assert step1["params"]["index"] == 2
    assert step1["params"]["leaf"] == b"\x00" * 32
    assert step1["result"] == True  # The boolean result

    # Step 2: get_current_epoch
    step2 = proxy._self_trace_steps[1]
    assert step2["op"] == "get_current_epoch"
    assert step2["result"] == 0  # The integer result
    assert epoch == 0


def test_unserializable_args_are_handled(recording_spec: RecordingSpec, mock_store: MockStore):
    """
    Tests that non-SSZ, non-fixture arguments (like 'store')
    are handled gracefully.
    """
    proxy = recording_spec

    # 1. Call function with 'store'
    head = proxy.get_head(store=mock_store)

    # 2. Check trace steps
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "get_head"

    # 3. Check that 'store' was serialized to its placeholder
    assert step["params"]["store"] == "<unserializable MockStore>"

    # 4. Check that the 'bytes' return value was recorded
    assert step["result"] == b"\xdd" * 32
    assert head == b"\xdd" * 32
