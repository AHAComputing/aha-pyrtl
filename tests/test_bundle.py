"""AHA-fork typed aggregates (Bundle / Vec) + ready-valid Decoupled interface, lowered to
PyRTL concat/slice. Tests lock pack/unpack round-trips (incl. nesting), the handshake, and that
the lowering is synthesizable (yosys)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

import pyrtl
from pyrtl import Bundle, Decoupled, Vec


def _sim_step(inputs):
    sim = pyrtl.Simulation()
    sim.step(inputs)
    return sim


def test_bundle_width_and_field_order():
    pkt = Bundle([("op", 4), ("addr", 8), ("data", 16)])
    assert pkt.width == 28
    assert pkt.field_names == ("op", "addr", "data")


def test_bundle_pack_unpack_roundtrip():
    pyrtl.reset_working_block()
    pkt = Bundle([("op", 4), ("addr", 8), ("data", 16)])
    a, b, d = pyrtl.Input(4, "a"), pyrtl.Input(8, "b"), pyrtl.Input(16, "d")
    f = pkt.unpack(pkt.pack({"op": a, "addr": b, "data": d}))
    for name, w, src in [("op", 4, "a"), ("addr", 8, "b"), ("data", 16, "d")]:
        o = pyrtl.Output(w, "out_" + name)
        o <<= f[name]
    sim = _sim_step({"a": 0x5, "b": 0xAB, "d": 0xBEEF})
    assert sim.inspect("out_op") == 0x5
    assert sim.inspect("out_addr") == 0xAB
    assert sim.inspect("out_data") == 0xBEEF


def test_nested_bundle_roundtrip():
    pyrtl.reset_working_block()
    pt = Bundle([("x", 8), ("y", 8)])
    frame = Bundle([("tag", 4), ("p", pt)])           # nested bundle field
    assert frame.width == 20
    t, x, y = pyrtl.Input(4, "t"), pyrtl.Input(8, "x"), pyrtl.Input(8, "y")
    packed = frame.pack({"tag": t, "p": {"x": x, "y": y}})
    f = frame.unpack(packed)
    ot, ox, oy = pyrtl.Output(4, "ot"), pyrtl.Output(8, "ox"), pyrtl.Output(8, "oy")
    ot <<= f["tag"]; ox <<= f["p"]["x"]; oy <<= f["p"]["y"]
    sim = _sim_step({"t": 0xC, "x": 0x11, "y": 0x22})
    assert (sim.inspect("ot"), sim.inspect("ox"), sim.inspect("oy")) == (0xC, 0x11, 0x22)


def test_vec_roundtrip():
    pyrtl.reset_working_block()
    lanes = Vec(4, 8)
    assert lanes.width == 32
    ins = [pyrtl.Input(8, f"i{k}") for k in range(4)]
    back = lanes.unpack(lanes.pack(ins))
    for k in range(4):
        o = pyrtl.Output(8, f"o{k}")
        o <<= back[k]
    sim = _sim_step({f"i{k}": 10 + k for k in range(4)})
    assert [sim.inspect(f"o{k}") for k in range(4)] == [10, 11, 12, 13]


def test_vec_of_bundles_roundtrip():
    pyrtl.reset_working_block()
    pe = Bundle([("a", 8), ("b", 8)])
    arr = Vec(2, pe)
    assert arr.width == 32
    a0, b0, a1, b1 = (pyrtl.Input(8, n) for n in ("a0", "b0", "a1", "b1"))
    packed = arr.pack([{"a": a0, "b": b0}, {"a": a1, "b": b1}])
    back = arr.unpack(packed)
    oa0 = pyrtl.Output(8, "oa0"); ob1 = pyrtl.Output(8, "ob1")
    oa0 <<= back[0]["a"]; ob1 <<= back[1]["b"]
    sim = _sim_step({"a0": 1, "b0": 2, "a1": 3, "b1": 4})
    assert sim.inspect("oa0") == 1 and sim.inspect("ob1") == 4


def test_decoupled_handshake_and_directions():
    pyrtl.reset_working_block()
    dc = Decoupled(8)
    prod, cons = dc.wires("p_"), dc.wires("c_")
    v, r, bts = pyrtl.Input(1, "v"), pyrtl.Input(1, "r"), pyrtl.Input(8, "bts")
    prod.valid <<= v; prod.bits <<= bts; cons.ready <<= r
    Decoupled.connect(prod, cons)              # bits/valid forward, ready back
    fire, cb, pr = pyrtl.Output(1, "fire"), pyrtl.Output(8, "cb"), pyrtl.Output(1, "pr")
    fire <<= Decoupled.fire(cons); cb <<= cons.bits; pr <<= prod.ready
    sim = pyrtl.Simulation()
    sim.step({"v": 1, "r": 0, "bts": 0x42})    # valid, not ready -> no fire
    assert sim.inspect("fire") == 0 and sim.inspect("cb") == 0x42 and sim.inspect("pr") == 0
    sim.step({"v": 1, "r": 1, "bts": 0x42})    # valid & ready -> fire
    assert sim.inspect("fire") == 1 and sim.inspect("pr") == 1


def test_decoupled_with_bundle_payload():
    pyrtl.reset_working_block()
    pkt = Bundle([("op", 4), ("addr", 12)])
    dc = Decoupled(pkt)
    assert dc.width == 16
    op, addr, v = pyrtl.Input(4, "op"), pyrtl.Input(12, "addr"), pyrtl.Input(1, "v")
    chan = dc.wires("ch_")
    chan.bits <<= pkt.pack({"op": op, "addr": addr})
    chan.valid <<= v
    chan.ready <<= pyrtl.Const(1, 1)          # tie off the back-channel for this one-sided test
    fields = dc.unpack_bits(chan)              # bits interpreted as the Bundle payload
    oo, oa = pyrtl.Output(4, "oo"), pyrtl.Output(12, "oa")
    oo <<= fields["op"]; oa <<= fields["addr"]
    sim = _sim_step({"op": 0x9, "addr": 0xABC, "v": 1})
    assert sim.inspect("oo") == 0x9 and sim.inspect("oa") == 0xABC


def test_exposed_from_pyrtl():
    for name in ("Bundle", "Vec", "Decoupled", "DecoupledWires"):
        assert hasattr(pyrtl, name)


def test_bundle_lowering_elaborates_in_yosys(tmp_path):
    yosys = shutil.which("yosys")
    if not yosys:
        pytest.skip("yosys not available")
    pyrtl.reset_working_block()
    pkt = Bundle([("op", 4), ("addr", 8), ("data", 4)])      # 16-bit word on one port
    busin = pyrtl.Input(16, "busin")
    busout = pyrtl.Output(16, "busout")
    f = pkt.unpack(busin)
    # swap op/data and zero addr, then re-pack -> a typed transform lowered to concat/slice
    busout <<= pkt.pack({"op": f["data"], "addr": pyrtl.Const(0, 8), "data": f["op"]})
    p = tmp_path / "bundle.v"
    with open(p, "w") as fh:
        pyrtl.output_to_systemverilog(fh, add_reset=False, block=pyrtl.working_block())
    r = subprocess.run([yosys, "-q", "-p", f"read_verilog -sv {p}; hierarchy -top toplevel; check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
