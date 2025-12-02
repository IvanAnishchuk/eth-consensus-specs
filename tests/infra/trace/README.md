## Spec trace framework

This is an implementation of #4603 a new testing framework for the Ethereum
consensus spec tests, based on tracing spec method calls and recording them in a
structured trace file.

The basic idea for new tests is to use pytest more properly, to use fixture
parametrization for forks and presets, and to use a spec trace file to generate
vectors (the actual vector generation is another task in progress).

### Usage and example test

```python
from tests.infra.trace import spec_trace


@with_all_phases
@spec_state_test  # keep these like before
@spec_trace  # this is the thing that makes the magic happen
def test_linear_sanity_slots_222(
    spec, state
):  # spec and state can be positional but the name matters
    # just use spec methods, they are traced automagically, and state is dumped
    spec.process_slot(state)
```

this is for example purposes put into
`tests/core/pyspec/eth2spec/test/gloas/sanity/test_slots_2.py` and can be run
with something like

```
make reftests fork=gloas runner=sanity k=linear_sanity_slots_222 verbose=true
```

that produces a trace in
`../consensus-spec-tests/tests/minimal/gloas/sanity/slots_2/pyspec_tests/linear_sanity_slots_222/trace.yaml`

### Spec trace file example

```yaml
default_fork: gloas
trace:
- {op: load_state, state_root: 
    95d19311d30804985b06c40cc437bdfbb126209ad9ea8253ba33e0ff0af74c40}
- op: spec_call
  method: process_slot
  input: {state: 
      95d19311d30804985b06c40cc437bdfbb126209ad9ea8253ba33e0ff0af74c40.ssz_snappy}
- {op: assert_state, state_root: 
    41f562b491baaa9fdd981973c8aef64bb7c663c4b07f35141c16afc9e11184c1}
```

In this example, `process_slot` does not return anything but we can see the
initial state and the final state being dumped automatically and they are
different. In the other more complex example test (omitted here for brewety)
we can examine how complex inputs and outputs being dumped and how out-of-band
state mutations are being tracked with assert and load steps.

### Implementation details

wrapt is used to wrap spec methods and record their calls, parameters and
results. A decorator is used to set things up. Some simple pydantic models are
used for the trace file structure and some sanitation/formatting.

### TODO

This is still work in progress.

I tried my best to separate core logic from the boilerplate needed but it could
be improved upon.

Some cleanup and polishing is still required and logic in the test consumer to
detect and dump new type of output.

Typing could be improved and some data sanitization hacks I came up with are
probably non-optimal. Test coverage needs another look, etc.

### Credits

Thanks to Leo for the initial idea and guidance, and to all the reviewers who
helped refine this.

Thanks to Cristobal for the first prototype of this framework, it's not used
here but I reviewed 4724 and got some inspiration from that.

Thanks to IG organizers, mentors, sponsors and fellow builders for making this
possible!
