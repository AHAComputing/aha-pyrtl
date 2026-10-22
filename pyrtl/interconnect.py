"""AHA fork: address-map interconnect generation (an SoC crossbar from a declared address map).

Diplomacy's valuable essence is: declare each slave's address range, and have the interconnect
auto-build the decode + crossbar — no hand-written address decoding, no manual master/slave wiring.
This does that on the fork's own ready-valid `Decoupled` bus (no TileLink protocol to copy), and
adds the thing diplomacy doesn't: the generated decoder is FORMALLY verifiable (the AHA test
proves, via the netlist->Z3 layer, that for ALL addresses exactly one slave is selected and it is
the right one).

  * `AddressMap` — slaves as (name, base, size) ranges; `address_map_violations` flags the #1
    interconnect bug (overlapping ranges) and zero-size regions.
  * `build_decoder(addr, amap)` — a combinational one-hot address decoder (sel[i] = addr in range i).
  * `route(master, addr, amap)` — a 1->M demux: a master request fans out to the addressed slave.
  * `crossbar(masters, addrs, amap)` — an N->M request crossbar: each master is routed by address,
    each slave arbitrates its contending masters (composed from `route` + `arbiter`).
  * `crossbar_full(masters, addrs, slave_resps, amap)` — a full bidirectional crossbar: the request
    is tagged with the master's source id, the slave echoes it, and the response path is just
    another `crossbar` routed by id (responses return to the originating master, out-of-order safe).

Composition-first: built from `Decoupled` + `arbiter`; the bidirectional path reuses `crossbar`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .bundle import Decoupled, DecoupledWires
from .util import arbiter


@dataclass(frozen=True)
class AddressMap:
    """Slaves as (name, base, size) byte ranges over an ``addrwidth``-bit address."""

    slaves: Tuple[Tuple[str, int, int], ...]
    addrwidth: int = 0

    def __post_init__(self):
        if not self.slaves:
            raise ValueError("address map needs at least one slave")
        if not self.addrwidth:
            top = max(b + s for _n, b, s in self.slaves)
            object.__setattr__(self, "addrwidth", max((top - 1).bit_length(), 1))

    @property
    def n(self) -> int:
        return len(self.slaves)


@dataclass(frozen=True)
class MapViolation:
    kind: str           # "overlap" | "zero_size" | "out_of_range"
    detail: str


def address_map_violations(amap: AddressMap) -> List[MapViolation]:
    """Flag overlapping ranges (the #1 interconnect bug), zero-size regions, and ranges that
    exceed the address width."""
    out: List[MapViolation] = []
    span = 1 << amap.addrwidth
    rs = list(amap.slaves)
    for i, (n, b, s) in enumerate(rs):
        if s <= 0:
            out.append(MapViolation("zero_size", f"{n}: size {s}"))
        if b < 0 or b + s > span:
            out.append(MapViolation("out_of_range", f"{n}: [{b},{b + s}) exceeds {span}"))
        for nj, bj, sj in rs[i + 1:]:
            if b < bj + sj and bj < b + s:               # half-open ranges intersect
                out.append(MapViolation("overlap", f"{n} & {nj}"))
    return out


def map_clean(amap: AddressMap) -> bool:
    return not address_map_violations(amap)


def build_decoder(addr, amap: AddressMap):
    """A combinational one-hot decoder: returns (sels, hit) where ``sels[i]`` is high iff ``addr``
    is in slave i's range, and ``hit`` is high iff any slave matches."""
    import pyrtl

    aw1 = amap.addrwidth + 1                              # +1 so base+size == 2^aw compares cleanly
    a = addr.zero_extended(aw1)
    sels = []
    for _name, base, size in amap.slaves:
        in_range = (~(a < pyrtl.Const(base, aw1))) & (a < pyrtl.Const(base + size, aw1))
        sels.append(in_range)
    hit = sels[0]
    for s in sels[1:]:
        hit = hit | s
    return sels, hit


def route(master: DecoupledWires, addr, amap: AddressMap, *, name: str = "rt") -> List[DecoupledWires]:
    """1->M demux: the master request fans out to the slave selected by ``addr``. Returns the M
    slave channels (master.bits forwarded; valid gated by the per-slave select; master.ready is the
    addressed slave's ready)."""
    import pyrtl

    sels, _hit = build_decoder(addr, amap)
    width = len(master.bits)
    branches: List[DecoupledWires] = []
    for s in range(amap.n):
        br = Decoupled(width).wires(f"{name}_s{s}_")
        br.bits <<= master.bits
        br.valid <<= master.valid & sels[s]
        branches.append(br)
    ready = sels[0] & branches[0].ready
    for s in range(1, amap.n):
        ready = ready | (sels[s] & branches[s].ready)
    master.ready <<= ready                                # ready of the addressed slave (one-hot)
    return branches


def crossbar(masters: List[DecoupledWires], addrs: List, amap: AddressMap,
             *, name: str = "xbar") -> List[DecoupledWires]:
    """N->M request crossbar: each master is routed by its address, and each slave arbitrates the
    masters contending for it. Returns the M slave channels. Composed from :func:`route` (demux)
    and :func:`arbiter` (per-slave merge). See :func:`crossbar_full` for the bidirectional version
    (response routing back to the originating master)."""
    if len(masters) != len(addrs):
        raise ValueError("one address wire per master")
    per_master = [route(m, a, amap, name=f"{name}_m{i}")
                  for i, (m, a) in enumerate(zip(masters, addrs))]
    slaves: List[DecoupledWires] = []
    for s in range(amap.n):
        contenders = [per_master[i][s] for i in range(len(masters))]
        slaves.append(arbiter(contenders, name=f"{name}_s{s}"))
    return slaves


def crossbar_full(masters: List[DecoupledWires], addrs: List, slave_resps: List[DecoupledWires],
                  amap: AddressMap, *, resp_width: int, name: str = "xbar"):
    """A full bidirectional N<->M crossbar. The request path routes each master to the addressed
    slave (by address) and tags the request with the master's source id; the slave is expected to
    ECHO that id in its response; the RESPONSE path is then just another crossbar routed by the id
    (so responses return to the originating master, out-of-order and multi-outstanding safe — no
    source FIFOs). Pure composition of :func:`crossbar` (used twice) + id tag/strip.

    Returns ``(slave_reqs, master_resps)``:
      * ``slave_reqs[s].bits`` = {id (high), request payload (low)} — the slave reads the payload
        and must echo ``id`` in the high bits of its response.
      * ``slave_resps[s].bits`` = {id (high), response payload (low ``resp_width`` bits)} — driven
        by the slave (this drives its ready).
      * ``master_resps[m].bits`` = the response payload (id stripped) — driven here.
    """
    import pyrtl

    n = len(masters)
    if n != len(addrs):
        raise ValueError("one address per master")
    if len(slave_resps) != amap.n:
        raise ValueError("one response channel per slave")
    idw = max((n - 1).bit_length(), 1)
    req_w = len(masters[0].bits)

    # request: tag master m's bits with its (static) source id = m, then route by address
    tagged = []
    for m in range(n):
        t = Decoupled(idw + req_w).wires(f"{name}_tag{m}_")
        t.bits <<= pyrtl.concat(pyrtl.Const(m, idw), masters[m].bits)   # id high, payload low
        t.valid <<= masters[m].valid
        masters[m].ready <<= t.ready
        tagged.append(t)
    slave_reqs = crossbar(tagged, addrs, amap, name=f"{name}_req")

    # response: route each slave response back to master[id] (id == the echoed source) via a
    # crossbar over an identity id-map (master i occupies id range [i, i+1))
    id_amap = AddressMap(tuple((f"m{i}", i, 1) for i in range(n)), addrwidth=idw)
    resp_ids = [sr.bits[resp_width:resp_width + idw] for sr in slave_resps]
    routed = crossbar(slave_resps, resp_ids, id_amap, name=f"{name}_resp")

    master_resps = []
    for m in range(n):
        mr = Decoupled(resp_width).wires(f"{name}_mresp{m}_")
        mr.bits <<= routed[m].bits[0:resp_width]                        # strip the id
        mr.valid <<= routed[m].valid
        routed[m].ready <<= mr.ready
        master_resps.append(mr)
    return slave_reqs, master_resps


__all__ = [
    "AddressMap", "MapViolation", "address_map_violations", "map_clean",
    "build_decoder", "route", "crossbar", "crossbar_full",
]
