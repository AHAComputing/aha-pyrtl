"""AHA-fork deterministic naming: PyRTL's temp/const/mem name counters are now PER-BLOCK
(thread-local working block), so the same design emits the SAME RTL regardless of what
was built before it or of parallel interleaving — reproducible, diffable commercial RTL."""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor

import pyrtl


def _build_design():
    """A small design with several auto-named temp + const wires."""
    a = pyrtl.Input(8, "a")
    b = pyrtl.Input(8, "b")
    o = pyrtl.Output(8, "o")
    o <<= ((a & b) | (a ^ b)) + (a + 3)   # temps + a const


def _emit_in_fresh_block() -> str:
    blk = pyrtl.Block()
    with pyrtl.set_working_block(blk, no_sanity_check=True):
        _build_design()
        buf = io.StringIO()
        pyrtl.output_to_verilog(buf, add_reset=False, block=blk)
        return buf.getvalue()


def test_same_design_same_rtl_regardless_of_build_history():
    v1 = _emit_in_fresh_block()
    # build an UNRELATED design in between — pre-fix this shifted the global temp counter
    other = pyrtl.Block()
    with pyrtl.set_working_block(other, no_sanity_check=True):
        x = pyrtl.Input(16, "x")
        y = pyrtl.Output(16, "y")
        y <<= (x * x) + (x | x) - (x + 7)
    v2 = _emit_in_fresh_block()
    assert v1 == v2                       # byte-identical -> names don't accumulate across designs
    # (per-design restart-at-0 is checked at the wire-set level in the test below)


def test_parallel_builds_produce_identical_rtl():
    seq = _emit_in_fresh_block()
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: _emit_in_fresh_block(), range(24)))
    assert all(r == seq for r in results)  # concurrent builds all identical (per-block, no interleave)


def test_per_block_indexers_are_independent():
    b1, b2 = pyrtl.Block(), pyrtl.Block()
    with pyrtl.set_working_block(b1, no_sanity_check=True):
        _build_design()
    with pyrtl.set_working_block(b2, no_sanity_check=True):
        _build_design()
    # both blocks numbered their temps independently from 0
    names1 = {w.name for w in b1.wirevector_set}
    names2 = {w.name for w in b2.wirevector_set}
    assert "tmp0" in names1 and "tmp0" in names2
    assert names1 == names2                # identical naming, independent counters
