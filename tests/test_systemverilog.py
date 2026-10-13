"""AHA-fork SystemVerilog emission: `logic` for every signal + intent-carrying `always_ff` for
sequential logic. Verilog-2001 (the default) is the lowered form (`wire`/`reg` + plain `always`);
combinational logic is `assign` in both, so the two are the same structure with different
keywords. yosys elaborates both."""
from __future__ import annotations

import io
import shutil
import subprocess

import pytest

import pyrtl
from pyrtl.modular import Instance, TopModule, emit_hierarchy


def _seq_design():
    pyrtl.reset_working_block()
    a = pyrtl.Input(8, "a")
    o = pyrtl.Output(8, "o")
    r = pyrtl.Register(8, "r", reset_value=3)
    r.next <<= (a & r) | pyrtl.Const(1, 8)
    o <<= r
    return pyrtl.working_block()


def _decl_lines(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip().startswith(("reg", "wire", "logic", "always"))]


def test_verilog_default_is_unchanged():
    buf = io.StringIO()
    pyrtl.output_to_verilog(buf, add_reset=True, block=_seq_design())
    decls = _decl_lines(buf.getvalue())
    assert any(d.startswith("reg") for d in decls)
    assert any(d.startswith("always @(posedge") for d in decls)
    assert not any(d.startswith("logic") or d.startswith("always_ff") for d in decls)


def test_systemverilog_uses_logic_and_always_ff():
    buf = io.StringIO()
    pyrtl.output_to_systemverilog(buf, add_reset=True, block=_seq_design())
    decls = _decl_lines(buf.getvalue())
    assert any(d.startswith("logic") for d in decls)
    assert any(d.startswith("always_ff @(posedge") for d in decls)
    assert not any(d.startswith("reg") or d.startswith("wire") for d in decls)
    assert not any(d.startswith("always @(") for d in decls)   # plain always replaced


def test_async_reset_systemverilog():
    buf = io.StringIO()
    pyrtl.output_to_systemverilog(buf, add_reset="asynchronous", block=_seq_design())
    assert "always_ff @(posedge clk or posedge rst)" in buf.getvalue()


def test_memory_emits_logic_and_always_ff():
    pyrtl.reset_working_block()
    wa = pyrtl.Input(4, "wa"); wd = pyrtl.Input(8, "wd"); we = pyrtl.Input(1, "we"); ra = pyrtl.Input(4, "ra")
    o = pyrtl.Output(8, "o")
    m = pyrtl.MemBlock(bitwidth=8, addrwidth=4, name="m")
    m[wa] <<= pyrtl.MemBlock.EnabledWrite(wd, enable=we)
    o <<= m[ra]
    buf = io.StringIO()
    pyrtl.output_to_systemverilog(buf, add_reset=False, block=pyrtl.working_block())
    v = buf.getvalue()
    # the memory array is declared with `logic` (not `reg`), and the write is an always_ff
    assert any(ln.strip().startswith("logic") and "m[15:0]" in ln for ln in v.splitlines())
    assert "reg" not in _kw(v)                       # no `reg`/`wire` keyword declarations remain
    assert "always_ff @(posedge clk)" in v          # sync write


def test_modular_hierarchy_systemverilog():
    def adder():
        pyrtl.reset_working_block()
        a = pyrtl.Input(8, "a"); b = pyrtl.Input(8, "b"); s = pyrtl.Output(9, "s")
        s <<= a + b
        return pyrtl.working_block()
    top = TopModule("dual", inputs=[("a0", 8), ("b0", 8)], outputs=[("s0", 9)], wires=[("t", 9)],
                    instances=[Instance("adder", "u0", {"a": "a0", "b": "b0", "s": "s0"})])
    files = emit_hierarchy({"adder": adder()}, top, systemverilog=True)
    assert "logic" in files["adder.v"] and "wire" not in _kw(files["adder.v"])
    assert "logic [8:0] t;" in files["dual.v"]   # the structural top's internal wire is `logic`


def _kw(text):
    return "\n".join(ln for ln in text.splitlines() if ln.strip().startswith(("wire", "reg")))


def test_output_to_systemverilog_exposed():
    assert hasattr(pyrtl, "output_to_systemverilog")


def test_both_dialects_elaborate_in_yosys(tmp_path):
    yosys = shutil.which("yosys")
    if not yosys:
        pytest.skip("yosys not available")
    vf = tmp_path / "d.v"
    sf = tmp_path / "d.sv"
    with open(vf, "w") as f:
        pyrtl.output_to_verilog(f, add_reset=True, block=_seq_design())
    with open(sf, "w") as f:
        pyrtl.output_to_systemverilog(f, add_reset=True, block=_seq_design())
    rv = subprocess.run([yosys, "-q", "-p", f"read_verilog {vf}; hierarchy -top toplevel; check"],
                        capture_output=True, text=True)
    rs = subprocess.run([yosys, "-q", "-p", f"read_verilog -sv {sf}; hierarchy -top toplevel; check"],
                        capture_output=True, text=True)
    assert rv.returncode == 0, rv.stdout + rv.stderr
    assert rs.returncode == 0, rs.stdout + rs.stderr
