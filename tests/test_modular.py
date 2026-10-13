"""AHA-fork modular Verilog emission (third_party/pyrtl/pyrtl/modular.py): a design
emits as a HIERARCHY (one module per file + a structural top that instantiates them),
not one flat module. Backward-compat: the default flat emit is unchanged."""
from __future__ import annotations

import io
import shutil
import subprocess

import pyrtl
from pyrtl.modular import Instance, TopModule, emit_hierarchy, emit_module


def _adder_block():
    pyrtl.reset_working_block()
    a = pyrtl.Input(8, "a")
    b = pyrtl.Input(8, "b")
    s = pyrtl.Output(9, "s")
    s <<= a + b
    return pyrtl.working_block()


def _dual_add_top():
    return TopModule(
        "dual_add",
        inputs=[("a0", 8), ("b0", 8), ("a1", 8), ("b1", 8)],
        outputs=[("s0", 9), ("s1", 9)],
        instances=[Instance("adder", "u0", {"a": "a0", "b": "b0", "s": "s0"}),
                   Instance("adder", "u1", {"a": "a1", "b": "b1", "s": "s1"})],
    )


def test_default_flat_emit_unchanged():
    buf = io.StringIO()
    pyrtl.output_to_verilog(buf, add_reset=False, block=_adder_block())
    assert "module toplevel(" in buf.getvalue()   # backward-compat: default name preserved


def test_emit_module_uses_given_name():
    v = emit_module(_adder_block(), "adder", add_reset=False)
    assert "module adder(" in v and "endmodule" in v


def test_emit_hierarchy_separate_files_with_instances():
    files = emit_hierarchy({"adder": _adder_block()}, _dual_add_top())
    assert set(files) == {"adder.v", "dual_add.v"}              # NOT one file
    assert "module adder(" in files["adder.v"]
    top = files["dual_add.v"]
    assert "module dual_add(" in top
    assert "adder u0 (" in top and "adder u1 (" in top         # two instances of the leaf
    assert ".a(a0)" in top and ".s(s0)" in top                 # port -> net connections
    assert ".clk(clk)" in top                                  # clk threaded to instances


def test_one_file_mode_keeps_hierarchy_in_one_file():
    top = TopModule("t", inputs=[("a", 8), ("b", 8)], outputs=[("s", 9)],
                    instances=[Instance("adder", "u0", {"a": "a", "b": "b", "s": "s"})])
    files = emit_hierarchy({"adder": _adder_block()}, top, one_file=True)
    assert set(files) == {"t.v"}
    assert "module adder(" in files["t.v"] and "module t(" in files["t.v"]


def test_modular_verilog_elaborates_in_yosys(tmp_path):
    # the strongest check: the emitted hierarchy reads + elaborates (instances resolve)
    yosys = shutil.which("yosys")
    if not yosys:
        import pytest
        pytest.skip("yosys not available")
    d = str(tmp_path)
    emit_hierarchy({"adder": _adder_block()}, _dual_add_top(), out_dir=d)
    r = subprocess.run(
        [yosys, "-q", "-p", f"read_verilog {d}/adder.v {d}/dual_add.v; hierarchy -top dual_add; check"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
