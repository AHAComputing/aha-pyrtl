"""AHA-fork realistic multi-port / banked SRAM modeling (pyrtl/sram.py).

PyRTL's MemBlock models ports as ideal. This adds a PPA + conflict model that captures the
real tradeoff (a true multiport bitcell is expensive; banking is cheap single-port banks with
conflicts) plus a functional banked-memory builder proven (cosim) equivalent to a flat
MemBlock. Tests lock the model's monotonicity + the tradeoff, exact conflict counts, and the
functional equivalence. No gcc (pure FastSimulation)."""
from __future__ import annotations

import pytest

import pyrtl
from pyrtl.sram import (
    SramConfig,
    bank_conflicts,
    banked_sram_equivalent,
    model_sram,
    served_accesses,
    wire_sram,
)


def test_wire_sram_certificate_proven():
    cert = wire_sram()
    assert cert.proven, cert.reason
    # the banking win is real: 4 banks cost less than a true 4-port for the same peak BW
    assert cert.evidence["banked4_vs_mono4_area_pct"] < 100.0


def test_area_monotone_in_bits_and_ports():
    base = model_sram(SramConfig(addrwidth=10, width=16))
    deeper = model_sram(SramConfig(addrwidth=12, width=16))
    wider = model_sram(SramConfig(addrwidth=10, width=64))
    assert deeper.area_mm2 > base.area_mm2
    assert wider.area_mm2 > base.area_mm2


def test_true_multiport_costs_more_area():
    one = model_sram(SramConfig(addrwidth=12, width=32, read_ports=1, write_ports=1))
    two = model_sram(SramConfig(addrwidth=12, width=32, read_ports=2, write_ports=2))
    assert two.area_mm2 > one.area_mm2


def test_banking_saves_area_and_is_faster_at_scale():
    mono4 = model_sram(SramConfig(addrwidth=12, width=32, read_ports=4))
    banked4 = model_sram(SramConfig(addrwidth=12, width=32, banks=4))
    assert banked4.peak_accesses_per_cycle == 4 and banked4.organization == "banked"
    assert banked4.area_mm2 < mono4.area_mm2            # banking saves area for same peak BW
    assert banked4.access_time_ns < mono4.access_time_ns  # smaller banks are faster
    assert banked4.read_energy_pj < mono4.read_energy_pj
    assert banked4.conflict_free is False and mono4.conflict_free is True


def test_bank_conflicts_exact():
    assert bank_conflicts([0, 4, 8, 12], banks=4) == 3   # all map to bank 0
    assert bank_conflicts([0, 1, 2, 3], banks=4) == 0    # one per bank
    assert bank_conflicts([0, 1, 4, 5], banks=4) == 2    # banks {0,0,1,1}
    assert bank_conflicts([], banks=4) == 0
    # multi-ported banks raise the per-bank capacity
    assert bank_conflicts([0, 4, 8], banks=4, ports_per_bank=2) == 1


def test_served_accesses():
    assert served_accesses([0, 1, 4, 5], banks=4) == 2   # 4 requested, 2 conflict
    assert served_accesses([0, 1, 2, 3], banks=4) == 4   # all served (distinct banks)


def test_banked_rtl_is_equivalent_to_flat_memory():
    """The strongest check: banked RTL produces the same read stream as a flat MemBlock."""
    stim = []
    for a in range(16):
        stim.append({"waddr": a, "wdata": (a * 11 + 3) & 0xFF, "we": 1, "raddr": (a + 5) % 16})
    for a in range(16):
        stim.append({"waddr": 0, "wdata": 0, "we": 0, "raddr": a})
    assert banked_sram_equivalent(addrwidth=4, width=8, banks=2, stimulus=stim)
    assert banked_sram_equivalent(addrwidth=4, width=8, banks=4, stimulus=stim)
    assert banked_sram_equivalent(addrwidth=4, width=8, banks=8, stimulus=stim)


def test_banked_builder_validates_args():
    stim = [{"waddr": 0, "wdata": 1, "we": 1, "raddr": 0}]
    with pytest.raises(ValueError):
        banked_sram_equivalent(addrwidth=4, width=8, banks=3, stimulus=stim)   # not power of 2
    with pytest.raises(ValueError):
        banked_sram_equivalent(addrwidth=4, width=8, banks=16, stimulus=stim)  # banks == depth


def test_sram_api_exposed_from_pyrtl():
    for name in ("SramConfig", "SramPPA", "model_sram", "bank_conflicts",
                 "served_accesses", "banked_sram_equivalent"):
        assert hasattr(pyrtl, name)
