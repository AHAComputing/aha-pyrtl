"""AHA fork: modular (hierarchical) Verilog emission.

Upstream PyRTL emits the whole working block as ONE flat ``module toplevel`` in one
file — no hierarchy, no reuse, hard for synthesis tools and unreadable for big designs.
This emits a HIERARCHY instead: each sub-circuit :class:`~pyrtl.core.Block` becomes its
own named Verilog ``module`` (one per file), plus a structural TOP module that
INSTANTIATES them.

PyRTL has no instance primitive (a Block is a flat netlist), so the top's structure —
which instances exist and how their ports connect — is described by the caller (which
knows its own hierarchy, e.g. an R×C mesh of identical PEs) via :class:`TopModule` /
:class:`Instance`.  Leaf modules are emitted by the (now module-name-parameterised)
:func:`pyrtl.output_to_verilog`; the top is generated structurally from the spec.

Example — two adder instances::

    a = pyrtl.Input(8, "a"); b = pyrtl.Input(8, "b")
    s = pyrtl.Output(9, "s"); s <<= a + b
    leaf = pyrtl.working_block()
    top = TopModule("dual_add",
                    inputs=[("a0", 8), ("b0", 8), ("a1", 8), ("b1", 8)],
                    outputs=[("s0", 9), ("s1", 9)],
                    instances=[Instance("adder", "u0", {"a": "a0", "b": "b0", "s": "s0"}),
                               Instance("adder", "u1", {"a": "a1", "b": "b1", "s": "s1"})])
    files = emit_hierarchy({"adder": leaf}, top)   # {"adder.v": ..., "dual_add.v": ...}
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .core import Block
from .importexport import output_to_verilog

# (name, bitwidth)
Port = Tuple[str, int]


@dataclass(frozen=True)
class Instance:
    """A submodule instantiation in the top: ``module name (.port(wire), ...)``."""

    module: str                      # the submodule's module name
    name: str                        # this instance's name (unique within the top)
    connections: Dict[str, str]      # submodule port name -> top-level net expression (e.g. "x" or "t[7:0]")


@dataclass(frozen=True)
class InstanceArray:
    """A REPLICATED instance, emitted as a Verilog ``generate``-``for`` loop rather than
    ``count`` unrolled instantiations — the synthesis-quality form for regular arrays
    (an R×C mesh of identical PEs, an N-lane datapath). O(1) emitted text for N instances.

    ``connections`` are raw net EXPRESSIONS written in terms of the loop ``genvar`` — e.g.
    ``{"din": "xin[i*8 +: 8]"}`` for a packed bus, or ``{"d": "row[i]"}`` for an arrayed
    wire (declare it in ``TopModule.array_wires``). This keeps with modular.py's contract:
    the caller, who knows the array's regularity, describes the structure. ``clk`` is wired
    automatically unless overridden in ``connections``."""

    module: str                      # the submodule's module name
    name: str                        # generate-block label + per-iteration instance base
    count: int                       # number of replicas
    connections: Dict[str, str] = field(default_factory=dict)  # port -> expr using the genvar
    genvar: str = "i"


# (name, width, count) -> ``wire [width-1:0] name [0:count-1];``
ArrayWire = Tuple[str, int, int]


@dataclass
class TopModule:
    """A structural top module: I/O, internal wires, instances, continuous assigns."""

    name: str
    inputs: List[Port] = field(default_factory=list)
    outputs: List[Port] = field(default_factory=list)
    wires: List[Port] = field(default_factory=list)
    instances: List[Instance] = field(default_factory=list)
    assigns: List[Tuple[str, str]] = field(default_factory=list)  # (lhs, rhs) continuous assigns
    clk: str = "clk"
    array_wires: List[ArrayWire] = field(default_factory=list)        # arrayed internal nets
    instance_arrays: List[InstanceArray] = field(default_factory=list)  # generate-for replicas
    macros: List["object"] = field(default_factory=list)             # hardip.MacroInstance (black-box IP)
    macro_arrays: List["object"] = field(default_factory=list)       # hardip.MacroArray (replicated IP)


def emit_module(block: Block, module_name: str, *, add_reset: bool | str = False,
                systemverilog: bool = False) -> str:
    """Emit a single :class:`Block` as a named Verilog/SystemVerilog ``module`` (string)."""
    buf = io.StringIO()
    output_to_verilog(buf, add_reset=add_reset, block=block, module_name=module_name,
                      systemverilog=systemverilog)
    return buf.getvalue()


def _decl(kind: str, name: str, width: int) -> str:
    rng = f"[{width - 1}:0] " if width and width > 1 else ""
    return f"    {kind} {rng}{name};"


def emit_top(top: TopModule, *, systemverilog: bool = False) -> str:
    """Generate the structural top module Verilog/SystemVerilog from its spec."""
    wire_kw = "logic" if systemverilog else "wire"
    ports = [top.clk] + [n for n, _ in top.inputs] + [n for n, _ in top.outputs]
    lines = [f"// Generated by PyRTL (AHA fork) — structural top",
             f"module {top.name}({', '.join(ports)});",
             f"    input {top.clk};"]
    lines += [_decl("input", n, w) for n, w in top.inputs]
    lines += [_decl("output", n, w) for n, w in top.outputs]
    lines += [_decl(wire_kw, n, w) for n, w in top.wires]
    # AHA fork: arrayed internal nets for replicated instances (e.g. PE-to-PE mesh wiring)
    for name, width, count in top.array_wires:
        rng = f"[{width - 1}:0] " if width and width > 1 else ""
        lines.append(f"    {wire_kw} {rng}{name} [0:{count - 1}];")
    for inst in top.instances:
        conns = [f".{top.clk}({top.clk})"] + [f".{port}({net})" for port, net in inst.connections.items()]
        lines.append(f"    {inst.module} {inst.name} ({', '.join(conns)});")
    # AHA fork: replicated instances as generate-for loops (O(1) text for N instances)
    declared_genvars: set = set()
    for arr in top.instance_arrays:
        if arr.genvar not in declared_genvars:
            lines.append(f"    genvar {arr.genvar};")
            declared_genvars.add(arr.genvar)
        conns = []
        if top.clk not in arr.connections:           # auto-wire clk unless the caller overrode it
            conns.append(f".{top.clk}({top.clk})")
        conns += [f".{port}({expr})" for port, expr in arr.connections.items()]
        lines.append(
            f"    generate for ({arr.genvar} = 0; {arr.genvar} < {arr.count}; "
            f"{arr.genvar} = {arr.genvar} + 1) begin : {arr.name}")
        lines.append(f"        {arr.module} {arr.name}_inst ({', '.join(conns)});")
        lines.append("    end endgenerate")
    # AHA fork: black-box hard-IP / macro instantiations (foundry IP, FPGA primitives, AHA IP)
    if top.macros or top.macro_arrays:
        from .hardip import render_array, render_instance

        for mi in top.macros:
            lines.append(render_instance(mi))
        for ma in top.macro_arrays:
            if ma.genvar not in declared_genvars:
                lines.append(f"    genvar {ma.genvar};")
                declared_genvars.add(ma.genvar)
            lines += render_array(ma)
    for lhs, rhs in top.assigns:
        lines.append(f"    assign {lhs} = {rhs};")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def emit_hierarchy(submodules: Dict[str, Block], top: TopModule, *,
                   add_reset: bool | str = False, out_dir: Optional[str] = None,
                   one_file: bool = False, systemverilog: bool = False) -> Dict[str, str]:
    """Emit a modular design.

    Each entry of ``submodules`` (``{module_name: block}``) is emitted as ``<name>.v``,
    and ``top`` is emitted as ``<top.name>.v``.  Returns ``{filename: verilog}``.  If
    ``out_dir`` is given, the files are also written there.  If ``one_file`` is True, all
    modules are concatenated into a single ``<top.name>.v`` (handy for tools that want one
    file but you still want real module hierarchy inside it).
    """
    files: Dict[str, str] = {}
    ordered: List[str] = []
    for name, block in submodules.items():
        v = emit_module(block, name, add_reset=add_reset, systemverilog=systemverilog)
        files[f"{name}.v"] = v
        ordered.append(v)
    # AHA fork: emit (* blackbox *) stubs for any hard-IP macros the top instantiates, so the
    # hierarchy elaborates self-contained (a real flow reads the vendor lib instead).
    macros = [mi.macro for mi in getattr(top, "macros", [])]
    macros += [ma.macro for ma in getattr(top, "macro_arrays", [])]
    if macros:
        from .hardip import collect_blackbox_stubs

        stubs = collect_blackbox_stubs(macros)
        files[f"{top.name}__blackboxes.v"] = stubs
        ordered.insert(0, stubs)   # stubs first so the top's instances resolve
    top_v = emit_top(top, systemverilog=systemverilog)
    files[f"{top.name}.v"] = top_v
    ordered.append(top_v)

    if one_file:
        files = {f"{top.name}.v": "\n".join(ordered)}
    if out_dir is not None:
        os.makedirs(out_dir, exist_ok=True)
        for fname, text in files.items():
            with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(text)
    return files
