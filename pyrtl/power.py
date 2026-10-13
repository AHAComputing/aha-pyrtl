"""AHA fork: activity-based dynamic power estimation.

PyRTL ships an area estimator (:func:`~pyrtl.analysis.area_estimation`) and a timing
estimator (:class:`~pyrtl.analysis.TimingAnalysis`) but **no power model**. This module
adds the missing PPA axis: a *toggle-activity* dynamic power estimate. It counts the bit
transitions each net makes over a simulation trace and weights them by a first-order
capacitance model (per-bit wire load + per-fanout gate load), plus a clock-tree term from
the register count.

What makes this different from a static estimate is that it is **workload-aware**: the
activity comes from a real simulation, so different stimulus produces different power. That
is exactly the signal an architecture-exploration loop wants — "does workload X switch more
than workload Y on this datapath?" — which a purely structural (gate-count) estimate cannot
give.

HONESTY (read before trusting a number):

* The switching **activity** (toggle counts) is *exact* for the supplied stimulus.
* The **capacitance / voltage / frequency** constants are *first-order models*, not a
  characterized standard-cell library. So treat **absolute watts as an estimate** and
  **relative comparisons** (design A vs B, workload X vs Y, before/after an optimization)
  as the trustworthy output.
* **Glitch power is not modeled.** A cycle-accurate trace records one settled value per
  cycle, so intra-cycle hazards/glitches are invisible — this under-counts activity on deep
  combinational logic.
* **Memory-array power is not modeled** (only the switching of mem *read-data wires* is
  counted, like any other net). SRAM/ROM internal energy is out of scope here.

Use :func:`estimate_dynamic_power` on an already-populated trace, or
:func:`simulate_and_estimate_power` to run a FastSimulation over stimulus and estimate in
one call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyrtl.core import working_block
from pyrtl.pyrtlexceptions import PyrtlError
from pyrtl.wire import Const, Register

__all__ = [
    "PowerParams",
    "NetPower",
    "PowerEstimate",
    "StreamingToggleTrace",
    "count_toggles",
    "estimate_dynamic_power",
    "estimate_streaming_dynamic_power",
    "simulate_and_estimate_power",
]


@dataclass(frozen=True)
class PowerParams:
    """First-order constants for the dynamic-power model. Defaults are order-of-magnitude
    values for an older (~130nm-ish) node; override them to match a target process. They
    set the *absolute* scale only — relative comparisons are insensitive to the exact
    numbers as long as the same params are used on both sides."""

    voltage: float = 1.0  # supply voltage (V)
    frequency_hz: float = 1.0e9  # clock frequency (Hz)
    cap_wire_ff: float = 2.0  # per-bit baseline (wire + self) capacitance (fF)
    cap_fanout_ff: float = 1.0  # per-bit, per-fanout gate-input capacitance (fF)
    cap_clock_ff: float = 3.0  # per-register-bit clock-pin + local clock-tree cap (fF)


@dataclass(frozen=True)
class NetPower:
    """Per-net switching contribution (sorted into :attr:`PowerEstimate.dominant_nets`)."""

    name: str
    bitwidth: int
    fanout: int
    toggles: int  # total bit transitions over the trace
    activity: float  # toggles per cycle (averaged over the net's bits)
    power_w: float


@dataclass
class PowerEstimate:
    """Result of a dynamic-power estimate. ``dynamic_power_w`` is the headline number
    (data switching + clock). All powers are in watts, energy in joules."""

    dynamic_power_w: float  # data_switching_power_w + clock_power_w
    data_switching_power_w: float  # workload-dependent (toggle-driven)
    clock_power_w: float  # ~workload-independent (register count)
    switching_energy_j: float  # total data-switching energy over the trace
    cycles: int  # number of cycle-to-cycle transitions observed
    total_toggles: int  # total bit transitions across all nets
    register_bits: int
    params: PowerParams
    dominant_nets: list[NetPower] = field(default_factory=list)
    note: str = (
        "MEASURED switching activity x MODELED capacitance; absolute watts are an "
        "estimate, relative comparisons are the trustworthy output. No glitch or "
        "memory-array power."
    )


class StreamingToggleTrace:
    """Count whole-net toggles online without retaining per-cycle waveforms.

    ``FastSimulation`` normally materializes one Python integer per tracked wire per
    cycle in a :class:`SimulationTrace`.  That is useful for waveforms but needlessly
    makes power measurement memory scale as ``O(wires * cycles)``.  This tracer keeps
    only the previous value and accumulated toggle count for each wire, so memory is
    ``O(wires)`` while preserving the exact settled-cycle activity used by
    :func:`estimate_dynamic_power`.

    The object implements the tracer seam consumed by ``Simulation`` and
    ``FastSimulation``.  It intentionally does not support waveform rendering.
    """

    def __init__(self, wires_to_track="all", block=None):
        self.block = working_block(block)
        if wires_to_track == "all":
            wires = self.block.wirevector_set
        elif wires_to_track is None:
            wires = self.block.wirevector_set
        else:
            wires = wires_to_track
        self.wires_to_track = tuple(
            sorted(
                (wire for wire in wires if not isinstance(wire, Const)),
                key=lambda wire: wire.name,
            )
        )
        if not self.wires_to_track:
            raise PyrtlError("streaming toggle trace needs at least one non-constant wire")
        self._wires = {wire.name: wire for wire in self.wires_to_track}
        # FastSimulation uses the keys of ``trace`` to decide which internal values to
        # return from its generated function. Values are sentinels, not waveform lists.
        self.trace = dict.fromkeys(self._wires)
        self._previous: dict[str, int] = {}
        self._toggles = dict.fromkeys(self._wires, 0)
        self._samples = 0
        self.default_value = 0
        self.register_value_map = {}
        self.memory_value_map = {}

    def __len__(self):
        return self._samples

    @property
    def cycles(self) -> int:
        return max(0, self._samples - 1)

    @property
    def toggle_counts(self) -> dict[str, int]:
        return dict(self._toggles)

    def _set_initial_values(
        self, default_value, register_value_map, memory_value_map
    ):
        self.default_value = default_value
        self.register_value_map = register_value_map
        self.memory_value_map = memory_value_map

    def _record(self, value_for_name):
        if self._samples == 0:
            for name in self.trace:
                self._previous[name] = int(value_for_name(name))
        else:
            for name in self.trace:
                value = int(value_for_name(name))
                self._toggles[name] += (
                    value ^ self._previous[name]
                ).bit_count()
                self._previous[name] = value
        self._samples += 1

    def add_step(self, value_map):
        self._record(lambda name: value_map[self._wires[name]])

    def add_fast_step(self, fastsim):
        self._record(fastsim.context.__getitem__)


def _bit_toggles(values) -> int:
    """Total bit transitions across consecutive cycle samples of one wire."""
    total = 0
    prev = values[0]
    for cur in values[1:]:
        total += (cur ^ prev).bit_count()
        prev = cur
    return total


def count_toggles(trace) -> tuple[dict[str, int], int]:
    """Count per-wire bit transitions in a populated :class:`SimulationTrace`.

    :param trace: a ``SimulationTrace`` whose ``.trace`` has been filled by a simulation.
        Track all wires (``SimulationTrace(wires_to_track='all')``) to capture internal
        switching, which dominates power.
    :return: ``(toggles_by_name, cycles)`` where ``cycles`` is the number of
        cycle-to-cycle transitions observed (``len(trace) - 1``).
    """
    toggles: dict[str, int] = {}
    cycles = 0
    for name, values in trace.trace.items():
        if len(values) < 2:
            toggles[name] = 0
            continue
        cycles = len(values) - 1
        toggles[name] = _bit_toggles(values)
    return toggles, cycles


def _estimate_from_toggles(
    *,
    toggles: dict[str, int],
    cycles: int,
    wires_by_name,
    block,
    params: PowerParams,
    top_k: int,
) -> PowerEstimate:
    if cycles <= 0:
        raise PyrtlError(
            "trace has fewer than 2 cycles; run the simulation for at least 2 steps "
            "before estimating power"
        )

    # fanout per wire, computed once (number of gate-input connections it drives)
    _, dst_nets = block.net_connections()
    fanout_of = {
        w: sum(1 for net in nets for a in net.args if a is w)
        for w, nets in dst_nets.items()
    }

    v2 = params.voltage * params.voltage
    half = 0.5 * 1.0e-15  # fF -> F, and the 1/2 in 1/2 C V^2 per transition
    energy_j = 0.0
    nets: list[NetPower] = []
    total_toggles = 0
    # Accumulate in sorted-name order: float addition is non-associative, and toggles is
    # keyed off the (id-hash-ordered) wire set, so an unsorted sum would vary by ULPs
    # across runs even though the toggle COUNTS are identical.
    for name, tog in sorted(toggles.items()):
        total_toggles += tog
        if tog == 0:
            continue
        w = wires_by_name.get(name)
        if w is None or isinstance(w, Const):
            continue
        fo = fanout_of.get(w, 0)
        cap_bit_ff = params.cap_wire_ff + params.cap_fanout_ff * fo
        e = half * cap_bit_ff * v2 * tog
        energy_j += e
        bw = w.bitwidth or 1
        nets.append(
            NetPower(
                name=name,
                bitwidth=bw,
                fanout=fo,
                toggles=tog,
                activity=tog / cycles,
                power_w=e * params.frequency_hz / cycles,
            )
        )

    data_power_w = energy_j * params.frequency_hz / cycles
    register_bits = sum(
        (w.bitwidth or 1) for w in block.wirevector_subset(Register)
    )
    clock_power_w = (
        register_bits
        * (params.cap_clock_ff * 1.0e-15)
        * v2
        * params.frequency_hz
    )

    nets.sort(key=lambda n: n.power_w, reverse=True)
    return PowerEstimate(
        dynamic_power_w=data_power_w + clock_power_w,
        data_switching_power_w=data_power_w,
        clock_power_w=clock_power_w,
        switching_energy_j=energy_j,
        cycles=cycles,
        total_toggles=total_toggles,
        register_bits=register_bits,
        params=params,
        dominant_nets=nets[:top_k],
    )


def estimate_dynamic_power(
    trace,
    block=None,
    params: PowerParams | None = None,
    top_k: int = 10,
) -> PowerEstimate:
    """Estimate dynamic power from a populated trace.

    :param trace: a ``SimulationTrace`` (track ``'all'`` wires for a meaningful estimate).
    :param block: the :class:`Block` the trace was taken from (defaults to the trace's
        block, then the :ref:`working_block`).
    :param params: :class:`PowerParams`; defaults to first-order constants.
    :param top_k: how many highest-power nets to return in ``dominant_nets``.
    """
    params = params or PowerParams()
    block = block or getattr(trace, "block", None) or working_block()

    toggles, cycles = count_toggles(trace)
    return _estimate_from_toggles(
        toggles=toggles,
        cycles=cycles,
        wires_by_name=dict(getattr(trace, "_wires", {})),
        block=block,
        params=params,
        top_k=top_k,
    )


def estimate_streaming_dynamic_power(
    trace: StreamingToggleTrace,
    block=None,
    params: PowerParams | None = None,
    top_k: int = 10,
) -> PowerEstimate:
    """Estimate power from an exact bounded-memory streaming toggle trace."""

    if not isinstance(trace, StreamingToggleTrace):
        raise PyrtlError("expected a StreamingToggleTrace")
    params = params or PowerParams()
    block = block or trace.block or working_block()
    return _estimate_from_toggles(
        toggles=trace.toggle_counts,
        cycles=trace.cycles,
        wires_by_name=trace._wires,
        block=block,
        params=params,
        top_k=top_k,
    )


def simulate_and_estimate_power(
    stimulus,
    block=None,
    params: PowerParams | None = None,
    register_value_map=None,
    memory_value_map=None,
    top_k: int = 10,
) -> PowerEstimate:
    """Run a FastSimulation over ``stimulus`` (tracking all wires) and estimate power.

    :param stimulus: an iterable of per-cycle input dicts, e.g.
        ``[{"a": 1, "b": 2}, {"a": 3, "b": 4}, ...]`` (need at least 2 to see a transition).
    :param block: the :class:`Block` to simulate (defaults to the working block).
    """
    from pyrtl.simulation import FastSimulation, SimulationTrace

    block = block or working_block()
    stimulus = list(stimulus)
    if len(stimulus) < 2:
        raise PyrtlError("need at least 2 input cycles to observe any switching")

    tracer = SimulationTrace(wires_to_track="all", block=block)
    sim = FastSimulation(
        tracer=tracer,
        block=block,
        register_value_map=register_value_map,
        memory_value_map=memory_value_map,
    )
    for inputs in stimulus:
        sim.step(inputs)
    return estimate_dynamic_power(tracer, block=block, params=params, top_k=top_k)
