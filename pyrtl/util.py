"""AHA fork: a small RTL utility library on the ready-valid Decoupled interface.

The components everyone reaches for when composing flow-controlled designs (Chisel's
``chisel3.util``), built as real PyRTL logic over :class:`~pyrtl.bundle.Decoupled`:

  * :func:`queue` — a synchronous FIFO: an enqueue channel in, a dequeue channel out, with
    backpressure (``enq.ready`` = not full, ``deq.valid`` = not empty). The workhorse for
    rate-matching / pipelining / the data side of a clock-domain crossing.
  * :func:`arbiter` — a fixed-priority N->1 merge: the lowest-index valid input wins, its payload
    is routed out, and only the granted input sees ``ready``.

Each is a function that builds the logic into the current working block and returns the produced
channel(s); the caller wires the remaining direction (e.g. ``deq.ready`` from the consumer).
"""
from __future__ import annotations

from typing import List

from .bundle import Decoupled, DecoupledWires


def queue(
    enq: DecoupledWires,
    depth: int,
    *,
    name: str = "q",
    clear=None,
    allow_full_replacement: bool = True,
) -> DecoupledWires:
    """A synchronous FIFO of ``depth`` entries. ``enq`` is the enqueue channel (the producer
    drives ``enq.bits``/``enq.valid``; this drives ``enq.ready``). Returns the dequeue channel
    (this drives ``deq.bits``/``deq.valid``; the consumer drives ``deq.ready``). Circular buffer
    over a MemBlock with head/tail/count; async read of the head, synchronous write of the tail."""
    import pyrtl

    if depth < 1:
        raise ValueError("queue depth must be >= 1")
    width = len(enq.bits)
    aw = max((depth - 1).bit_length(), 1)
    cw = depth.bit_length() + 1                       # count holds 0..depth

    deq = Decoupled(width).wires(f"{name}_deq_")
    mem = pyrtl.MemBlock(bitwidth=width, addrwidth=aw, name=f"{name}_mem")
    head = pyrtl.Register(aw, f"{name}_head")
    tail = pyrtl.Register(aw, f"{name}_tail")
    count = pyrtl.Register(cw, f"{name}_count")

    full = count == depth
    empty = count == 0
    deq.valid <<= ~empty
    deq.bits <<= mem[head]                            # async read of the oldest entry
    # A simultaneous dequeue can normally free the head slot at the same edge.
    # Some compositions deliberately disable this bypass to cut a ready/valid
    # combinational cycle at an arbitration boundary.
    if allow_full_replacement:
        enq.ready <<= ~full | (deq.ready & ~empty)
    else:
        enq.ready <<= ~full

    do_enq = enq.valid & enq.ready
    do_deq = deq.ready & ~empty
    mem[tail] <<= pyrtl.MemBlock.EnabledWrite(enq.bits, enable=do_enq)

    def _adv(ptr):                                    # wrap depth-1 -> 0
        return pyrtl.select(ptr == (depth - 1), pyrtl.Const(0, aw), (ptr + 1)[0:aw])

    next_tail = pyrtl.select(do_enq, _adv(tail), tail)
    next_head = pyrtl.select(do_deq, _adv(head), head)
    next_count = (
        count + do_enq.zero_extended(cw) - do_deq.zero_extended(cw)
    )[0:cw]
    if clear is None:
        tail.next <<= next_tail
        head.next <<= next_head
        count.next <<= next_count
    else:
        tail.next <<= pyrtl.select(clear, pyrtl.Const(0, aw), next_tail)
        head.next <<= pyrtl.select(clear, pyrtl.Const(0, aw), next_head)
        count.next <<= pyrtl.select(clear, pyrtl.Const(0, cw), next_count)
    return deq


def _fixed_grants(valids):
    """One-hot fixed-priority grant over a valid vector: grant[i] = v[i] & no lower-index valid."""
    import pyrtl

    grants = []
    higher = pyrtl.Const(0, 1)
    for v in valids:
        grants.append(v & ~higher)
        higher = higher | v
    return grants


def _or(bits):
    acc = bits[0]
    for b in bits[1:]:
        acc = acc | b
    return acc


def _drive_output(out: DecoupledWires, inputs, grants) -> None:
    """Wire a merged output from a one-hot grant: bits = the granted payload, each input's ready
    asserted only when granted and the downstream is ready."""
    import pyrtl

    sel = pyrtl.Const(0, len(out.bits))
    for g, ch in zip(grants, inputs):
        sel = sel | (g.sign_extended(len(out.bits)) & ch.bits)
    out.bits <<= sel
    for g, ch in zip(grants, inputs):
        ch.ready <<= g & out.ready


def arbiter(inputs: List[DecoupledWires], *, name: str = "arb") -> DecoupledWires:
    """A fixed-priority N->1 arbiter: the lowest-index valid input is granted. ``inputs`` are
    producer channels (they drive bits/valid; this drives each ``ready``). Returns the merged
    output channel (this drives bits/valid; the consumer drives ``out.ready``)."""
    import pyrtl

    if not inputs:
        raise ValueError("arbiter needs at least one input")
    out = Decoupled(len(inputs[0].bits)).wires(f"{name}_out_")
    valids = [ch.valid for ch in inputs]
    out.valid <<= _or(valids)
    _drive_output(out, inputs, _fixed_grants(valids))
    return out


def round_robin_arbiter(inputs: List[DecoupledWires], *, name: str = "rr") -> DecoupledWires:
    """A FAIR (round-robin) N->1 arbiter: after granting input g, priority rotates past g, so no
    input can be starved under sustained contention. Same interface as :func:`arbiter`."""
    import pyrtl

    if not inputs:
        raise ValueError("arbiter needs at least one input")
    n = len(inputs)
    out = Decoupled(len(inputs[0].bits)).wires(f"{name}_out_")
    valids = [ch.valid for ch in inputs]

    mask = pyrtl.Register(n, f"{name}_mask")          # thermometer of already-served indices
    cand_valids = [valids[i] & ~mask[i] for i in range(n)]
    g_cand = _fixed_grants(cand_valids)               # prefer unmasked (rotated) candidates
    g_all = _fixed_grants(valids)                     # fall back to all (wrap the round)
    any_cand = _or(cand_valids)
    grants = [pyrtl.select(any_cand, g_cand[i], g_all[i]) for i in range(n)]

    out.valid <<= _or(valids)
    _drive_output(out, inputs, grants)

    fire = out.valid & out.ready
    gh = pyrtl.concat(*reversed(grants))              # n-bit one-hot of the granted index
    thermo = gh | (gh - 1)                            # bits 0..g set -> mask <= g next round
    mask.next <<= pyrtl.select(fire, thermo, mask)
    return out


__all__ = ["queue", "arbiter", "round_robin_arbiter"]
