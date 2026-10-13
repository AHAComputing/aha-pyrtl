"""AHA fork: activity-based dynamic power estimation (pyrtl/power.py).

PyRTL had area + timing estimators but no power model. This adds a toggle-activity dynamic
power estimate from a simulation trace. The defining property — and what these tests lock —
is that it is WORKLOAD-AWARE: power tracks the actual switching a workload induces, which a
static gate-count estimate cannot. The estimate is honest about its limits (MEASURED
activity x MODELED capacitance), so the tests assert *relationships* (more switching -> more
power, linear in f, quadratic in V, deterministic) rather than absolute watts. No gcc needed
(pure FastSimulation)."""
from __future__ import annotations

import pytest

import pyrtl
from pyrtl.power import PowerParams, estimate_dynamic_power, simulate_and_estimate_power


def _mac():
    """A 16x16 multiply-accumulate: a high-toggle datapath with a register."""
    pyrtl.reset_working_block()
    a = pyrtl.Input(16, "a")
    b = pyrtl.Input(16, "b")
    o = pyrtl.Output(16, "o")
    r = pyrtl.Register(16, "r")
    r.next <<= (a * b)[0:16] ^ r
    o <<= r
    return pyrtl.working_block()


def _combinational():
    pyrtl.reset_working_block()
    a = pyrtl.Input(8, "a")
    b = pyrtl.Input(8, "b")
    o = pyrtl.Output(8, "o")
    o <<= (a & b) | (a ^ b)
    return pyrtl.working_block()


def test_power_is_workload_aware():
    """Changing inputs switch the datapath far more than constant inputs -> more data
    power. This is the property a static estimate cannot provide."""
    _mac()
    low = simulate_and_estimate_power([{"a": 5, "b": 5}] * 20)  # held constant
    _mac()
    high = simulate_and_estimate_power(
        [{"a": (i * 2654435761) & 0xFFFF, "b": (i * 40503) & 0xFFFF} for i in range(1, 21)]
    )
    assert high.total_toggles > low.total_toggles
    assert high.data_switching_power_w > low.data_switching_power_w


def test_clock_power_is_workload_independent_and_scales_with_registers():
    _mac()
    w1 = simulate_and_estimate_power([{"a": 1, "b": 1}, {"a": 9, "b": 9}, {"a": 2, "b": 3}])
    _mac()
    w2 = simulate_and_estimate_power([{"a": 7, "b": 7}, {"a": 0, "b": 0}, {"a": 5, "b": 6}])
    # clock power depends only on register count, not on the stimulus
    assert w1.clock_power_w == w2.clock_power_w
    assert w1.register_bits == 16
    p = PowerParams()
    expected = w1.register_bits * (p.cap_clock_ff * 1e-15) * p.voltage**2 * p.frequency_hz
    assert w1.clock_power_w == pytest.approx(expected)


def test_estimate_is_deterministic():
    stim = [{"a": 3, "b": 7}, {"a": 9, "b": 2}, {"a": 1, "b": 5}]
    _mac()
    e1 = simulate_and_estimate_power(stim)
    # perturb object ids so the wire-set iteration order shifts between the two estimates;
    # the energy sum must still be BIT-identical (we accumulate in sorted-name order, since
    # float addition is non-associative). Without that, watts vary by ULPs across runs.
    _keepalive = pyrtl.Block()
    with pyrtl.set_working_block(_keepalive, no_sanity_check=True):
        w = pyrtl.Input(16, "jx")
        for i in range(1500):
            w = (w + i) & 0xFFFF
    _mac()
    e2 = simulate_and_estimate_power(stim)
    assert e1.dynamic_power_w == e2.dynamic_power_w  # bit-identical, not just close
    assert e1.data_switching_power_w == e2.data_switching_power_w
    assert e1.total_toggles == e2.total_toggles


def test_count_toggles_is_exact():
    """An input driven 0,0xFF,0,0xFF,0 flips all 8 of its bits on each of the 4 transitions
    -> exactly 32 toggles on that wire. Activity is exact even though watts are modeled."""
    _combinational()
    from pyrtl.power import count_toggles
    from pyrtl.simulation import FastSimulation, SimulationTrace

    tracer = SimulationTrace(wires_to_track="all")
    sim = FastSimulation(tracer=tracer)
    for v in [0x00, 0xFF, 0x00, 0xFF, 0x00]:
        sim.step({"a": v, "b": 0x00})
    toggles, cycles = count_toggles(tracer)
    assert cycles == 4
    assert toggles["a"] == 8 * 4  # 8 bits flipped on each of 4 transitions


def test_constant_inputs_give_zero_data_power():
    """Purely combinational logic with unchanging inputs settles and never switches."""
    _combinational()
    est = simulate_and_estimate_power([{"a": 0xAB, "b": 0xCD}] * 4)
    assert est.total_toggles == 0
    assert est.data_switching_power_w == 0.0
    assert est.clock_power_w == 0.0  # no registers


def test_power_scales_linearly_in_freq_and_quadratically_in_voltage():
    _mac()
    # capture one trace, then re-estimate with different params (no re-simulation)
    from pyrtl.simulation import FastSimulation, SimulationTrace

    tracer = SimulationTrace(wires_to_track="all")
    sim = FastSimulation(tracer=tracer)
    for i in range(1, 11):
        sim.step({"a": i * 111, "b": i * 7})

    base = estimate_dynamic_power(tracer, params=PowerParams(voltage=1.0, frequency_hz=1e9))
    double_f = estimate_dynamic_power(tracer, params=PowerParams(voltage=1.0, frequency_hz=2e9))
    double_v = estimate_dynamic_power(tracer, params=PowerParams(voltage=2.0, frequency_hz=1e9))

    assert double_f.dynamic_power_w == pytest.approx(2.0 * base.dynamic_power_w)
    assert double_v.dynamic_power_w == pytest.approx(4.0 * base.dynamic_power_w)


def test_too_few_cycles_raises():
    _mac()
    with pytest.raises(pyrtl.PyrtlError):
        simulate_and_estimate_power([{"a": 1, "b": 1}])  # need >= 2 cycles


def test_dominant_nets_sorted_by_power():
    _mac()
    est = simulate_and_estimate_power(
        [{"a": (i * 31) & 0xFFFF, "b": (i * 97) & 0xFFFF} for i in range(1, 16)]
    )
    powers = [n.power_w for n in est.dominant_nets]
    assert powers == sorted(powers, reverse=True)
    assert all(n.toggles > 0 for n in est.dominant_nets)
