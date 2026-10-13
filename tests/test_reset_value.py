"""AHA-fork synth-quality emission: per-register reset_value.

PyRTL registers carry a ``reset_value``; this locks that the emitted Verilog honours it —
in the synchronous AND asynchronous synthesizable reset blocks, and in the testbench's
register initialisation — rather than resetting everything to 0. Also covers a fixed latent
bug: an explicit ``register_value_map`` entry of 0 must win over the reset_value (the old
truthiness check treated an explicit 0 as "absent"). yosys-elaboration confirms the reset
RTL is valid.
"""
from __future__ import annotations

import io
import shutil
import subprocess

import pytest

import pyrtl
from pyrtl.importexport import _VerilogOutput


def _design():
    pyrtl.reset_working_block()
    a = pyrtl.Input(8, "a")
    o = pyrtl.Output(8, "o")
    r0 = pyrtl.Register(8, "r0")                    # default reset (0)
    r5 = pyrtl.Register(8, "r5", reset_value=5)
    r9 = pyrtl.Register(8, "r9", reset_value=9)
    r0.next <<= a
    r5.next <<= a + r0
    r9.next <<= r5
    o <<= r9
    return pyrtl.working_block()


def _reset_block(verilog: str) -> str:
    """The lines inside the `if (rst) begin ... end else` reset assignment block."""
    out, inblk = [], False
    for line in verilog.splitlines():
        if "if (rst) begin" in line:
            inblk = True
            continue
        if inblk and "end else" in line:
            break
        if inblk:
            out.append(line.strip())
    return "\n".join(out)


def test_synchronous_reset_uses_per_register_reset_value():
    buf = io.StringIO()
    pyrtl.output_to_verilog(buf, add_reset=True, block=_design())
    blk = _reset_block(buf.getvalue())
    assert "r0 <= 8'd0;" in blk
    assert "r5 <= 8'd5;" in blk   # NOT 0
    assert "r9 <= 8'd9;" in blk


def test_asynchronous_reset_uses_per_register_reset_value():
    buf = io.StringIO()
    pyrtl.output_to_verilog(buf, add_reset="asynchronous", block=_design())
    v = buf.getvalue()
    assert "always @(posedge clk or posedge rst)" in v   # async reset sensitivity
    blk = _reset_block(v)
    assert "r5 <= 8'd5;" in blk and "r9 <= 8'd9;" in blk


def test_testbench_init_uses_reset_value_by_default():
    blk = _design()
    vo = _VerilogOutput(blk, add_reset=True)
    buf = io.StringIO()
    vo.output_verilog_testbench(buf, vcd="")
    v = buf.getvalue()
    assert "block.r5 = 8'd5;" in v   # reset_value, no explicit map
    assert "block.r9 = 8'd9;" in v


def test_explicit_register_value_zero_overrides_reset_value():
    """The fixed bug: an explicit init of 0 must win over reset_value=5 (was overwritten)."""
    blk = _design()
    r5 = next(w for w in blk.wirevector_set if w.name == "r5")
    tr = pyrtl.SimulationTrace(block=blk)
    tr.register_value_map = {r5: 0}
    vo = _VerilogOutput(blk, add_reset=True)
    buf = io.StringIO()
    vo.output_verilog_testbench(buf, simulation_trace=tr, vcd="")
    v = buf.getvalue()
    assert "block.r5 = 8'd0;" in v   # explicit 0 honoured (not 8'd5)
    assert "block.r9 = 8'd9;" in v   # unmapped register still uses its reset_value


def test_explicit_nonzero_register_value_overrides_reset_value():
    blk = _design()
    r5 = next(w for w in blk.wirevector_set if w.name == "r5")
    tr = pyrtl.SimulationTrace(block=blk)
    tr.register_value_map = {r5: 7}
    vo = _VerilogOutput(blk, add_reset=True)
    buf = io.StringIO()
    vo.output_verilog_testbench(buf, simulation_trace=tr, vcd="")
    assert "block.r5 = 8'd7;" in buf.getvalue()


def test_reset_module_elaborates_in_yosys(tmp_path):
    yosys = shutil.which("yosys")
    if not yosys:
        pytest.skip("yosys not available")
    p = tmp_path / "reset_design.v"
    with open(p, "w") as f:
        pyrtl.output_to_verilog(f, add_reset=True, block=_design())
    r = subprocess.run(
        [yosys, "-q", "-p", f"read_verilog {p}; hierarchy -top toplevel; proc; check"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
