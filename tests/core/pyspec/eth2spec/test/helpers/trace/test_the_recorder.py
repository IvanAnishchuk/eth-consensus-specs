"""
This file unit-tests the trace recorder itself using
pytest fixtures and unittest.mock.

This approach is faster and more isolated than using 'pytester'.
"""

import pytest
from remerkleable.complex import Container
from remerkleable.basic import uint64
from eth2spec.test.helpers.trace.traced_spec import RecordingSpec


# --- Mocks for eth2spec objects ---
# We rename these to match the expected class names in CLASS_NAME_MAP
# and inherit from Container so isinstance(x, Container) checks pass.

class BeaconState(Container):
    """Mocks a BeaconState"""
    slot: uint64

    def __new__(cls, root: bytes = b"\x00" * 32, slot: int = 0):
        # Intercept 'root' here so it doesn't go to Container.__new__
        return super().__new__(cls, slot=slot)

    def __init__(self, root: bytes = b"\x00" * 32, slot: int = 0):
        # We don't need to call super().__init__ because Container does setup in __new__
        # Store our mock root separately
        self._root = root

    def hash_tree_root(self) -> bytes:
        return self._root

    def copy(self):
        return BeaconState(self._root, int(self.slot))


class BeaconBlock(Container):
    """Mocks a BeaconBlock"""
    slot: uint64

    def __new__(cls, root: bytes = b"\x01" * 32, slot: int = 0):
        return super().__new__(cls, slot=slot)

    def __init__(self, root: bytes = b"\x01" * 32, slot: int = 0):
        self._root = root

    def hash_tree_root(self) -> bytes:
        return self._root


class Attestation(Container):
    """Mocks an Attestation"""
    data: uint64  # Dummy field

    def __new__(cls, root: bytes = b"\x02" * 32):
        return super().__new__(cls, data=0)

    def __init__(self, root: bytes = b"\x02" * 32):
        self._root = root

    def hash_tree_root(self) -> bytes:
        return self._root


class Slot(int):
    """Mocks a Slot (int subclass)"""
    pass


class MockSpec:
    """Mocks the 'spec' object"""

    def tick(self, state: BeaconState, slot: int) -> None:
        # Simulate a state change by modifying the root
        # In a real spec, this would be a complex state transition
        new_root_int = int.from_bytes(state._root, "big") + 1
        state._root = new_root_int.to_bytes(32, "big")

    def no_op(self, state: BeaconState) -> None:
        # Does not modify state
        pass

    def get_current_epoch(self, state: BeaconState) -> int:
        return 0

    def get_root(self, data: bytes) -> bytes:
        return data  # Echo back bytes

    def fail_op(self) -> None:
        raise AssertionError("Something went wrong")


# --- Fixtures ---

@pytest.fixture
def mock_spec():
    return MockSpec()


@pytest.fixture
def recording_spec(mock_spec):
    # Initial context with one state
    # Root is 101010...
    initial_state = BeaconState(root=b"\x10" * 32)
    context = {"state": initial_state}
    
    return RecordingSpec(mock_spec, context)


# --- Tests ---

def test_basic_function_call(recording_spec):
    """Tests basic function recording and result capture."""
    proxy = recording_spec
    
    # The initial state is registered with a hash-based name
    root_hex = b"\x10" * 32
    root_hex_str = root_hex.hex()
    
    state_name = None
    for name, obj in proxy._self_name_to_obj_map.items():
        if obj.hash_tree_root() == root_hex:
            state_name = name
            break
    assert state_name is not None
    
    result = proxy.get_current_epoch(proxy._self_name_to_obj_map[state_name])
    
    assert result == 0
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "get_current_epoch"
    assert step["result"] == 0
    assert "error" not in step or step["error"] is None


def test_argument_sanitization(recording_spec):
    """Tests that arguments are sanitized (bytes -> hex, subclasses -> primitives)."""
    proxy = recording_spec
    
    # 1. Bytes should be hex-encoded with 0x prefix
    data = b"\xca\xfe"
    proxy.get_root(data)
    
    step = proxy._self_trace_steps[0]
    assert step["params"]["data"] == "0xcafe"
    
    # 2. Int subclasses (Slot) should be raw ints
    slot = Slot(42)
    
    root_hex = b"\x10" * 32
    root_hex_str = root_hex.hex()
    state_name = f"$context.states.{root_hex_str}"
    state = proxy._self_name_to_obj_map[state_name]
    
    proxy.tick(state, slot)
    
    step = proxy._self_trace_steps[1]
    assert step["params"]["slot"] == 42
    assert type(step["params"]["slot"]) is int


def test_result_sanitization(recording_spec):
    """Tests that return values are sanitized."""
    proxy = recording_spec
    
    # get_root returns bytes, expecting 0x hex string in trace
    result = proxy.get_root(b"\xde\xad")
    
    step = proxy._self_trace_steps[0]
    assert step["result"] == "0xdead"


def test_exception_handling(recording_spec):
    """Tests that exceptions are captured in the trace."""
    proxy = recording_spec
    
    # Should re-raise the exception
    with pytest.raises(AssertionError, match="Something went wrong"):
        proxy.fail_op()
        
    assert len(proxy._self_trace_steps) == 1
    step = proxy._self_trace_steps[0]
    assert step["op"] == "fail_op"
    assert step["result"] is None
    assert step["error"]["type"] == "AssertionError"
    assert step["error"]["message"] == "Something went wrong"


def test_state_mutation_and_deduplication(recording_spec):
    """
    Tests that:
    1. State mutation triggers a 'load_state' op.
    2. The new state name uses the root hash.
    3. Non-mutating operations do NOT trigger 'load_state'.
    """
    proxy = recording_spec
    
    root_hex = b"\x10" * 32
    root_hex_str = root_hex.hex()
    state_name = f"$context.states.{root_hex_str}"
    state = proxy._self_name_to_obj_map[state_name]
    
    # 1. Call op that DOES change state
    proxy.tick(state, 1)
    
    # We expect 2 steps: the 'tick' op, and 'load_state'
    assert len(proxy._self_trace_steps) == 2
    
    tick_step = proxy._self_trace_steps[0]
    load_step = proxy._self_trace_steps[1]
    
    assert tick_step["op"] == "tick"
    assert load_step["op"] == "load_state"
    
    # Check naming convention: should be hash-based
    new_root = state.hash_tree_root().hex()
    assert new_root != root_hex_str
    assert load_step["result"] == f"$context.states.{new_root}"
    
    # 2. Call op that DOES NOT change state
    proxy.no_op(state)
    
    # Should only add the 'no_op' step, NO 'load_state'
    assert len(proxy._self_trace_steps) == 3
    assert proxy._self_trace_steps[2]["op"] == "no_op"


def test_manual_artifacts(recording_spec):
    """Tests spec.ssz, spec.meta, and spec.configure."""
    proxy = recording_spec
    
    root_hex = b"\x10" * 32
    root_hex_str = root_hex.hex()
    state_name = f"$context.states.{root_hex_str}"
    state = proxy._self_name_to_obj_map[state_name]
    
    # 1. spec.ssz
    proxy.ssz("custom_state.ssz", state)
    
    assert "custom_state.ssz" in proxy._self_artifacts_to_write
    step = proxy._self_trace_steps[0]
    assert step["op"] == "ssz"
    assert step["params"]["name"] == "custom_state.ssz"
    # Result is the HASH of the state
    assert step["result"] == state_name
    
    # 2. spec.meta
    proxy.meta("description", "test case")
    assert proxy._self_metadata["description"] == "test case"
    
    # 3. spec.configure
    proxy.configure({"PRESET_BASE": "minimal"})
    assert proxy._self_config_data["PRESET_BASE"] == "minimal"
