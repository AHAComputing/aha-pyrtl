"""AHA fork: realistic multi-port / banked SRAM modeling.

PyRTL's ``MemBlock`` allows multiple read/write ports but treats them as IDEAL — no
area/timing cost per port, no bank conflicts. Real SRAM PPA is dominated by port count and
organization: a true dual-port bitcell is ~2x a single-port cell, whereas BANKING emulates
bandwidth with cheap single-port banks at the cost of conflicts (two accesses to one bank in
a cycle contend). This module adds two things:

1. A parameterized PPA + conflict MODEL (:func:`model_sram`, :func:`bank_conflicts`): area,
   access energy, access time, peak bandwidth, and exact bank-conflict counts for an access
   pattern — so a design's memory is costed honestly, and the monolithic-multiport vs banked
   tradeoff is explicit.
2. A FUNCTIONAL banked-memory builder (:func:`banked_sram_equivalent`): a low-order
   interleaved bank array that is proven (cosim) to produce the same read stream as a flat
   ``MemBlock`` — so the banking is real RTL, not just a cost model.

HONEST: area reuses the published SRAM scaling of ``analysis.area_estimation``
(``bits**0.9 * ports**0.7 * tech**2.07``); the per-bank/port multipliers and the access
energy/time constants are FIRST-ORDER models, so trust RELATIVE comparisons and treat
absolute mm^2 / pJ / ns as estimates. Bank-conflict COUNTS and the functional equivalence
are EXACT.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List

# modeled access constants (first-order; relative comparisons are what matter)
_E_PER_BIT_SQRT_ROW_PJ = 1.0e-3   # access energy ~ width * sqrt(rows)
_T_PER_SQRT_ROW_NS = 1.0e-2       # access time  ~ sqrt(rows)
_T_BASE_NS = 0.05                 # fixed access overhead
_DECODE_OVERHEAD_FRAC = 0.02      # bank decode/read-mux area, as a fraction of bank area


def _mem_area_mm2(bits: float, ports: int, tech_nm: float, is_rom: bool) -> float:
    """The published SRAM area model used by analysis.area_estimation (mm^2)."""
    tech_um = tech_nm / 1000.0
    area = 0.001 * tech_um ** 2.07 * bits ** 0.9 * max(ports, 1) ** 0.7 + 0.0048
    return area / 10.0 if is_rom else area


@dataclass(frozen=True)
class SramConfig:
    """An SRAM to cost: ``2**addrwidth`` entries x ``width`` bits, with ports or banking."""

    addrwidth: int
    width: int
    read_ports: int = 1
    write_ports: int = 1
    banks: int = 1          # 1 = monolithic multiport; >1 = banked (single-port banks)
    tech_nm: float = 28.0
    is_rom: bool = False

    @property
    def depth(self) -> int:
        return 1 << self.addrwidth

    @property
    def bits(self) -> int:
        return self.depth * self.width


@dataclass(frozen=True)
class SramPPA:
    organization: str            # "monolithic" | "banked"
    area_mm2: float
    read_energy_pj: float        # energy of ONE read access (one bank, if banked)
    write_energy_pj: float
    access_time_ns: float
    peak_accesses_per_cycle: int  # bandwidth ceiling (ports for monolithic, banks for banked)
    conflict_free: bool          # monolithic multiport = conflict-free; banked = pattern-dependent

    def to_dict(self) -> Dict[str, Any]:
        return {
            "organization": self.organization,
            "area_mm2": self.area_mm2,
            "read_energy_pj": self.read_energy_pj,
            "write_energy_pj": self.write_energy_pj,
            "access_time_ns": self.access_time_ns,
            "peak_accesses_per_cycle": self.peak_accesses_per_cycle,
            "conflict_free": self.conflict_free,
        }


def model_sram(cfg: SramConfig) -> SramPPA:
    """Model the PPA of an SRAM configuration.

    Monolithic (``banks==1``): a true multiport array — area grows with the port count
    (``max(read,write)`` ports, matching analysis.area_estimation), bandwidth = read+write
    ports, conflict-free. Banked (``banks>1``): ``banks`` single-port banks each holding
    ``bits/banks`` — far less area for the same peak bandwidth, faster + lower energy per
    access (smaller banks = fewer rows), but bandwidth is bank-conflict-limited.
    """
    if cfg.banks <= 1:
        ports = max(cfg.read_ports, cfg.write_ports)
        area = _mem_area_mm2(cfg.bits, ports, cfg.tech_nm, cfg.is_rom)
        rows = cfg.depth
        peak = cfg.read_ports + cfg.write_ports
        organization, conflict_free = "monolithic", True
    else:
        per_bank_bits = cfg.bits / cfg.banks
        per_bank_area = _mem_area_mm2(per_bank_bits, 1, cfg.tech_nm, cfg.is_rom)
        area = cfg.banks * per_bank_area * (1.0 + _DECODE_OVERHEAD_FRAC)
        rows = cfg.depth / cfg.banks
        peak = cfg.banks
        organization, conflict_free = "banked", False

    sqrt_rows = math.sqrt(max(rows, 1.0))
    access_energy = _E_PER_BIT_SQRT_ROW_PJ * cfg.width * sqrt_rows
    access_time = _T_BASE_NS + _T_PER_SQRT_ROW_NS * sqrt_rows
    return SramPPA(
        organization=organization,
        area_mm2=area,
        read_energy_pj=access_energy,
        write_energy_pj=access_energy,
        access_time_ns=access_time,
        peak_accesses_per_cycle=peak,
        conflict_free=conflict_free,
    )


def bank_conflicts(addresses: List[int], banks: int, ports_per_bank: int = 1) -> int:
    """Number of accesses that CANNOT be served this cycle because their bank is over
    capacity. Low-order interleave (bank = ``addr % banks``); a bank with ``ports_per_bank``
    ports serves that many accesses, the rest conflict. EXACT for the given pattern."""
    if banks <= 1:
        return max(0, len(addresses) - ports_per_bank)
    hits = Counter(a % banks for a in addresses)
    return sum(max(0, c - ports_per_bank) for c in hits.values())


def served_accesses(addresses: List[int], banks: int, ports_per_bank: int = 1) -> int:
    """Accesses actually served this cycle (= total - conflicts)."""
    return len(addresses) - bank_conflicts(addresses, banks, ports_per_bank)


# ── functional banked memory: proven (cosim) == a flat MemBlock ────────────────────────


def _run_flat(addrwidth: int, width: int, stimulus: List[Dict[str, int]]) -> List[int]:
    import pyrtl

    blk = pyrtl.Block()
    with pyrtl.set_working_block(blk, no_sanity_check=True):
        waddr = pyrtl.Input(addrwidth, "waddr")
        wdata = pyrtl.Input(width, "wdata")
        we = pyrtl.Input(1, "we")
        raddr = pyrtl.Input(addrwidth, "raddr")
        rdata = pyrtl.Output(width, "rdata")
        mem = pyrtl.MemBlock(bitwidth=width, addrwidth=addrwidth, name="flat")
        mem[waddr] <<= pyrtl.MemBlock.EnabledWrite(wdata, enable=we)
        rdata <<= mem[raddr]
    return _simulate_rdata(blk, stimulus)


def _run_banked(addrwidth: int, width: int, banks: int,
                stimulus: List[Dict[str, int]]) -> List[int]:
    import pyrtl

    log2banks = banks.bit_length() - 1
    intra_aw = addrwidth - log2banks
    blk = pyrtl.Block()
    with pyrtl.set_working_block(blk, no_sanity_check=True):
        waddr = pyrtl.Input(addrwidth, "waddr")
        wdata = pyrtl.Input(width, "wdata")
        we = pyrtl.Input(1, "we")
        raddr = pyrtl.Input(addrwidth, "raddr")
        rdata = pyrtl.Output(width, "rdata")
        # low-order interleave: bank = addr[low log2banks], intra = addr[high bits]
        w_bank = waddr[0:log2banks]
        w_intra = waddr[log2banks:addrwidth]
        r_bank = raddr[0:log2banks]
        r_intra = raddr[log2banks:addrwidth]
        bank_reads = []
        for b in range(banks):
            mem = pyrtl.MemBlock(bitwidth=width, addrwidth=intra_aw, name=f"bank{b}")
            we_b = we & (w_bank == b)
            mem[w_intra] <<= pyrtl.MemBlock.EnabledWrite(wdata, enable=we_b)
            bank_reads.append(mem[r_intra])
        rdata <<= pyrtl.mux(r_bank, *bank_reads)
    return _simulate_rdata(blk, stimulus)


def _simulate_rdata(blk, stimulus: List[Dict[str, int]]) -> List[int]:
    import pyrtl

    tracer = pyrtl.SimulationTrace(block=blk)
    sim = pyrtl.FastSimulation(tracer=tracer, block=blk)
    out: List[int] = []
    for s in stimulus:
        sim.step({"waddr": s["waddr"], "wdata": s["wdata"],
                  "we": s["we"], "raddr": s["raddr"]})
        out.append(sim.inspect("rdata"))
    return out


def banked_sram_equivalent(addrwidth: int, width: int, banks: int,
                           stimulus: List[Dict[str, int]]) -> bool:
    """A low-order interleaved banked SRAM produces the SAME per-cycle read stream as a flat
    MemBlock of the same depth/width on ``stimulus`` (cosim functional equivalence). ``banks``
    must be a power of 2 with ``1 < banks < 2**addrwidth`` (so each bank has >= 2 entries)."""
    if banks & (banks - 1) != 0:
        raise ValueError("banks must be a power of 2")
    if not (1 < banks < (1 << addrwidth)):
        raise ValueError("need 1 < banks < 2**addrwidth")
    return _run_flat(addrwidth, width, stimulus) == _run_banked(addrwidth, width, banks, stimulus)


# ── certificate ────────────────────────────────────────────────────────────────────────


@dataclass
class SramCertificate:
    area_grows_with_size: bool       # area monotone in bits and ports
    multiport_costs_area: bool       # true 2-port area > 1-port area
    banking_saves_area: bool         # N banks < true N-port at scale (the banking win)
    banking_is_faster_lower_energy: bool  # smaller banks -> less access time + energy
    conflicts_exact: bool            # bank_conflicts matches hand counts
    banked_equiv_flat: bool          # banked RTL == flat MemBlock (cosim)
    evidence: Dict[str, Any] = None
    reason: str = ""

    @property
    def proven(self) -> bool:
        return (self.area_grows_with_size and self.multiport_costs_area
                and self.banking_saves_area and self.banking_is_faster_lower_energy
                and self.conflicts_exact and self.banked_equiv_flat)


def wire_sram() -> SramCertificate:
    """MEAS/SRAM: the multi-port/banked SRAM model is monotone + captures the real
    multiport-vs-banked area/bandwidth/conflict tradeoff, and the banked RTL is proven
    (cosim) equivalent to a flat memory."""
    tech = 28.0
    # a reasonably-sized SRAM (banking wins at scale; the base-cell overhead dominates tiny ones)
    big = SramConfig(addrwidth=12, width=32, tech_nm=tech)            # 4Ki x 32b
    mono1 = model_sram(big)
    mono2 = model_sram(SramConfig(addrwidth=12, width=32, read_ports=2, write_ports=2, tech_nm=tech))
    banked4 = model_sram(SramConfig(addrwidth=12, width=32, banks=4, tech_nm=tech))
    # a true 4-access monolithic memory vs a 4-bank memory (same peak bandwidth = 4)
    mono4 = model_sram(SramConfig(addrwidth=12, width=32, read_ports=4, tech_nm=tech))

    area_grows = (
        _mem_area_mm2(2 ** 12 * 32, 1, tech, False) > _mem_area_mm2(2 ** 10 * 32, 1, tech, False)
        and _mem_area_mm2(2 ** 12 * 32, 2, tech, False) > _mem_area_mm2(2 ** 12 * 32, 1, tech, False)
    )
    multiport_costs = mono2.area_mm2 > mono1.area_mm2
    banking_saves = banked4.area_mm2 < mono4.area_mm2 and banked4.peak_accesses_per_cycle == 4
    banking_faster = (banked4.access_time_ns < mono1.access_time_ns
                      and banked4.read_energy_pj < mono1.read_energy_pj)

    # conflicts: all to one bank (mod 4 == 0) -> K-1 conflicts; one per distinct bank -> 0
    conflicts_exact = (
        bank_conflicts([0, 4, 8, 12], banks=4) == 3      # all bank 0
        and bank_conflicts([0, 1, 2, 3], banks=4) == 0   # one per bank
        and bank_conflicts([0, 1, 4, 5], banks=4) == 2   # banks {0,0,1,1}
        and served_accesses([0, 1, 4, 5], banks=4) == 2
    )

    # functional: write each address, read it back interleaved; banked == flat
    stim = []
    for a in range(16):
        stim.append({"waddr": a, "wdata": (a * 7 + 1) & 0xFF, "we": 1, "raddr": (a + 3) % 16})
    for a in range(16):
        stim.append({"waddr": 0, "wdata": 0, "we": 0, "raddr": a})   # read-only sweep
    banked_equiv = banked_sram_equivalent(addrwidth=4, width=8, banks=4, stimulus=stim)

    reason = ""
    for ok, msg in [
        (area_grows, "area not monotone in bits/ports"),
        (multiport_costs, "a true 2r2w port did not cost more area than 1r1w"),
        (banking_saves, "banking did not save area vs a true multiport at scale"),
        (banking_faster, "smaller banks were not faster / lower-energy"),
        (conflicts_exact, "bank conflict counts wrong"),
        (banked_equiv, "banked RTL did not match a flat MemBlock (cosim)"),
    ]:
        if not ok:
            reason = msg
            break

    return SramCertificate(
        area_grows_with_size=area_grows,
        multiport_costs_area=multiport_costs,
        banking_saves_area=banking_saves,
        banking_is_faster_lower_energy=banking_faster,
        conflicts_exact=conflicts_exact,
        banked_equiv_flat=banked_equiv,
        evidence=dict(
            mono_1port_mm2=round(mono1.area_mm2, 4),
            mono_2r2w_mm2=round(mono2.area_mm2, 4),
            mono_4port_mm2=round(mono4.area_mm2, 4),
            banked4_mm2=round(banked4.area_mm2, 4),
            banked4_vs_mono4_area_pct=round(100 * banked4.area_mm2 / mono4.area_mm2, 1),
            mono_access_ns=round(mono1.access_time_ns, 4),
            banked4_access_ns=round(banked4.access_time_ns, 4),
        ),
        reason=reason,
    )


__all__ = [
    "SramConfig", "SramPPA", "model_sram", "bank_conflicts", "served_accesses",
    "banked_sram_equivalent", "SramCertificate", "wire_sram",
]
