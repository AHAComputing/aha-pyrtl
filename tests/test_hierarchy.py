"""AHA-fork build-time module hierarchy (third_party/pyrtl/pyrtl/hierarchy.py): a leaf
Module is elaborated ONCE and instantiated N times; flatten() inlines for correct
simulation (== a hand-built flat reference); emit_verilog() produces modular RTL."""
from __future__ import annotations

import shutil
import subprocess

import pytest

import pyrtl
from pyrtl import hierarchy as H


def _stage_builder(counter: dict):
    def build():
        counter["n"] = counter.get("n", 0) + 1
        d = pyrtl.Input(8, "d")
        q = pyrtl.Output(8, "q")
        r = pyrtl.Register(8, "r")
        r.next <<= d
        q <<= r            # q = d delayed one cycle
    return build


def _pipeline(stage: H.Module, n: int) -> H.Design:
    d = H.Design(f"pipe{n}")
    nets = [d.add_input("din", 8)]
    nets += [d.net(f"n{i}", 8) for i in range(1, n)]
    nets.append(d.add_output("dout", 8))
    for i in range(n):
        d.instantiate(stage, f"u{i}", {"d": nets[i], "q": nets[i + 1]})
    return d


def _flat_shift_reference(n: int) -> pyrtl.Block:
    pyrtl.reset_working_block()
    din = pyrtl.Input(8, "din")
    dout = pyrtl.Output(8, "dout")
    prev = din
    for i in range(n):
        r = pyrtl.Register(8, f"r{i}")
        r.next <<= prev
        prev = r
    dout <<= prev
    return pyrtl.working_block()


def _run(block: pyrtl.Block, stimulus):
    tr = pyrtl.SimulationTrace(block=block)
    sim = pyrtl.FastSimulation(tracer=tr, block=block)
    for inputs in stimulus:
        sim.step(inputs)
    return list(tr.trace["dout"])


def test_module_elaborated_once_then_reused():
    counter: dict = {}
    stage = H.module("stage", _stage_builder(counter))
    design = _pipeline(stage, 4)
    assert counter["n"] == 1                 # leaf built ONCE, not per-instance
    assert design.n_instances == 4 and design.n_distinct_modules == 1


def test_flatten_pipeline_matches_flat_reference():
    n = 5
    stage = H.module("stage", _stage_builder({}))
    stim = [{"din": 0xA5}] + [{"din": 0} for _ in range(n + 2)]

    hier = _run(_pipeline(stage, n).flatten(), stim)
    ref = _run(_flat_shift_reference(n), stim)
    assert hier == ref                       # hierarchical-flatten sim == flat reference
    assert hier[n] == 0xA5 and hier[0] == 0   # the value lands after n cycles


def test_combinational_instances_flatten_and_sim():
    def build_add():
        a = pyrtl.Input(8, "a")
        b = pyrtl.Input(8, "b")
        s = pyrtl.Output(9, "s")
        s <<= a + b
    adder = H.module("adder", build_add)
    d = H.Design("dual_add")
    a0, b0 = d.add_input("a0", 8), d.add_input("b0", 8)
    a1, b1 = d.add_input("a1", 8), d.add_input("b1", 8)
    s0, s1 = d.add_output("s0", 9), d.add_output("s1", 9)
    d.instantiate(adder, "u0", {"a": a0, "b": b0, "s": s0})
    d.instantiate(adder, "u1", {"a": a1, "b": b1, "s": s1})

    flat = d.flatten()
    tr = pyrtl.SimulationTrace(block=flat)
    sim = pyrtl.FastSimulation(tracer=tr, block=flat)
    sim.step({"a0": 3, "b0": 4, "a1": 10, "b1": 20})
    assert sim.inspect("s0") == 7 and sim.inspect("s1") == 30


def test_emit_is_modular_and_elaborates(tmp_path):
    stage = H.module("stage", _stage_builder({}))
    files = _pipeline(stage, 4).emit_verilog()
    assert set(files) == {"stage.v", "pipe4.v"}
    top = files["pipe4.v"]
    assert top.count("stage ") >= 4            # four instances of the one leaf module
    assert "module stage(" in files["stage.v"]

    yosys = shutil.which("yosys")
    if not yosys:
        pytest.skip("yosys not available")
    d = str(tmp_path)
    _pipeline(stage, 4).emit_verilog(out_dir=d)
    r = subprocess.run([yosys, "-q", "-p",
                        f"read_verilog {d}/stage.v {d}/pipe4.v; hierarchy -top pipe4; check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_instantiate_validates_ports():
    stage = H.module("stage", _stage_builder({}))
    d = H.Design("bad")
    x = d.add_input("x", 8)
    with pytest.raises(ValueError):
        d.instantiate(stage, "u0", {"d": x})   # missing port "q"
