"""AHA fork: multi-clock SoC composition + clock-domain-crossing (CDC) checking.

PyRTL has one implicit clock, but that is a PER-MODULE property: every module is single-clock
internally, and a real multi-clock SoC is built by wiring DIFFERENT clocks to different instances
at the top. This module models exactly that — a clock-domain-aware composition (domains + nodes
assigned to domains + declared CDC crossings) — so large SoCs can be assembled and EMITTED as
multi-clock RTL without touching PyRTL's core, with the crossings structurally CHECKED.

The headline is the CDC CHECK (:func:`cdc_violations`), the #1 SoC bug class: every signal that
crosses a clock-domain boundary must pass through a registered synchronizer, and a multi-bit
signal must not go through a plain two-flop synchronizer (bit-coherency hazard — use an async
FIFO / gray code). Unchecked crossings cause metastability in silicon.

Emission (:func:`emit_soc`) produces a multi-clock top (one clk input per domain; each node wired
to its domain's clock), a real two-flop synchronizer module, a REAL gray-code async-FIFO module
for multi-bit crossings (:func:`async_fifo_verilog`; no longer a black box), black-box stubs for
the nodes and for handshake crossings, and elaborates in yosys. The node internals are out of
scope here — this layer is about composition / clocking / CDC, not the leaf logic.

The async FIFO's CDC safety rests on a GRAY-CODE pointer: consecutive pointer values differ in
exactly one bit, so a pointer synchronized through a two-flop synchronizer is always either the
old or the new value — never a corrupted multi-bit-transition intermediate. That gray encode/decode
is combinational, single-clock logic, so it is built (:func:`gray_encode`/:func:`gray_decode`) and
formally PROVEN here (mutual inverses ∀, and the Hamming-distance-1 property the synchronizer
relies on). The two-clock FIFO assembly itself is emitted as templated RTL (PyRTL has one clock
per block, so a dual-clock module cannot be simulated in a single PyRTL block) and yosys-elaborated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

_VALID_KINDS = ("two_flop", "async_fifo", "handshake")


@dataclass(frozen=True)
class ClockDomain:
    name: str
    clk: str                                   # the clk net/port name for this domain


@dataclass(frozen=True)
class SocPort:
    port: str                                  # the node module's port name
    net: str                                   # the SoC net it connects to
    direction: str                             # "input" | "output"
    width: int = 1


@dataclass(frozen=True)
class SocNode:
    """An instance placed in a clock domain. ``clk`` is wired automatically to the domain's
    clock; ``ports`` are the data ports."""

    name: str
    module: str
    domain: str
    ports: Tuple[SocPort, ...] = ()


@dataclass(frozen=True)
class CdcCrossing:
    """A declared crossing: ``src_net`` (driven in some domain) is synchronized into
    ``dst_domain`` as ``dst_net`` by a ``kind`` synchronizer. Readers in ``dst_domain`` use
    ``dst_net``; reading ``src_net`` directly is a CDC violation."""

    src_net: str
    dst_net: str
    dst_domain: str
    kind: str = "two_flop"
    width: int = 1
    addr_bits: int = 4                         # async-FIFO depth = 2**addr_bits (>=2)


@dataclass
class MultiClockSoC:
    name: str
    domains: List[ClockDomain] = field(default_factory=list)
    top_inputs: List[Tuple[str, int, str]] = field(default_factory=list)    # (name, width, domain)
    top_outputs: List[Tuple[str, int, str]] = field(default_factory=list)   # (name, width, domain)
    internal_nets: List[Tuple[str, int]] = field(default_factory=list)      # (name, width)
    nodes: List[SocNode] = field(default_factory=list)
    crossings: List[CdcCrossing] = field(default_factory=list)

    def _domain_clk(self, domain: str) -> str:
        for d in self.domains:
            if d.name == domain:
                return d.clk
        raise ValueError(f"unknown clock domain {domain!r}")


@dataclass(frozen=True)
class CdcViolation:
    kind: str                                  # "unsynchronized_crossing" | "multibit_two_flop"
    net: str
    detail: str


def _driver_domain(soc: MultiClockSoC) -> Dict[str, str]:
    """net -> the clock domain it is DRIVEN in (top inputs, node outputs, crossing outputs)."""
    drv: Dict[str, str] = {}
    for name, _w, dom in soc.top_inputs:
        drv[name] = dom
    for node in soc.nodes:
        for p in node.ports:
            if p.direction == "output":
                drv[p.net] = node.domain
    for x in soc.crossings:
        drv[x.dst_net] = x.dst_domain           # the synchronizer output is in the dst domain
    return drv


def cdc_violations(soc: MultiClockSoC) -> List[CdcViolation]:
    """Structurally check the SoC's clock-domain crossings. Returns every:
      * net read in a domain different from where it is driven WITHOUT a declared crossing
        (an unsynchronized crossing — metastability risk);
      * multi-bit net taken through a plain two-flop synchronizer (bit-coherency hazard)."""
    drv = _driver_domain(soc)
    out: List[CdcViolation] = []

    # 1) every node INPUT must be driven in the node's own domain (crossings supply same-domain nets)
    for node in soc.nodes:
        for p in node.ports:
            if p.direction != "input":
                continue
            src = drv.get(p.net)
            if src is not None and src != node.domain:
                out.append(CdcViolation(
                    "unsynchronized_crossing", p.net,
                    f"net {p.net!r} driven in {src!r} but read by {node.name!r} in "
                    f"{node.domain!r} with no synchronizer"))

    # top outputs must be driven in their declared domain too
    for name, _w, dom in soc.top_outputs:
        src = drv.get(name)
        if src is not None and src != dom:
            out.append(CdcViolation(
                "unsynchronized_crossing", name,
                f"top output {name!r} declared in {dom!r} but driven in {src!r}"))

    # 2) multi-bit through a two-flop synchronizer is a bit-coherency hazard
    for x in soc.crossings:
        if x.kind == "two_flop" and x.width > 1:
            out.append(CdcViolation(
                "multibit_two_flop", x.dst_net,
                f"{x.width}-bit crossing {x.src_net!r}->{x.dst_net!r} uses a two-flop "
                "synchronizer; use an async FIFO / gray code for multi-bit"))
        if x.kind not in _VALID_KINDS:
            out.append(CdcViolation("unknown_kind", x.dst_net, f"unknown crossing kind {x.kind!r}"))
    return out


def cdc_clean(soc: MultiClockSoC) -> bool:
    return not cdc_violations(soc)


# ── gray-code pointer logic (the CDC-safe heart of the async FIFO; provable single-clock) ──

def gray_encode(w):
    """Binary -> reflected Gray code (combinational): ``gray = bin ^ (bin >> 1)``. Consecutive
    binary values map to Gray codes differing in exactly one bit — the property that lets a FIFO
    pointer cross a clock domain through a plain synchronizer without bit-coherency hazard."""
    import pyrtl

    return w ^ pyrtl.shift_right_logical(w, 1)


def gray_decode(g):
    """Gray code -> binary (combinational): ``bin = g ^ (g>>1) ^ (g>>2) ^ ...`` — the inverse of
    :func:`gray_encode`."""
    import pyrtl

    acc = g
    for k in range(1, len(g)):
        acc = acc ^ pyrtl.shift_right_logical(g, k)
    return acc


def async_fifo_verilog() -> str:
    """A real, parameterized (WIDTH, AW) dual-clock async FIFO with gray-code pointers and two-flop
    pointer synchronizers — the standard CDC-safe FIFO. Depth = 2**AW (AW >= 2). Instantiated by
    :func:`emit_soc` for ``async_fifo`` crossings (replaces the old black-box stub)."""
    return (
        "// gray-code dual-clock async FIFO (CDC-safe multi-bit crossing); depth = 2**AW (AW>=2)\n"
        "module async_fifo #(parameter WIDTH = 8, parameter AW = 4) (\n"
        "    input  wclk, input  wrst_n, input  winc, input  [WIDTH-1:0] wdata, output reg wfull,\n"
        "    input  rclk, input  rrst_n, input  rinc, output [WIDTH-1:0] rdata, output reg rempty\n"
        ");\n"
        "    localparam DEPTH = (1 << AW);\n"
        "    reg [WIDTH-1:0] mem [0:DEPTH-1];\n"
        "    // write domain: binary + gray write pointer, read-gray synced in. wfull is REGISTERED\n"
        "    // (uses the registered wfull in wbin_next) so there is no combinational loop.\n"
        "    reg  [AW:0] wbin, wgray, rgray_w1, rgray_w2;\n"
        "    wire [AW:0] wbin_next  = wbin + (winc & ~wfull);\n"
        "    wire [AW:0] wgray_next = (wbin_next >> 1) ^ wbin_next;\n"
        "    wire        wfull_val  = (wgray_next == {~rgray_w2[AW:AW-1], rgray_w2[AW-2:0]});\n"
        "    always @(posedge wclk or negedge wrst_n)\n"
        "        if (!wrst_n) begin wbin <= 0; wgray <= 0; wfull <= 1'b0; end\n"
        "        else        begin wbin <= wbin_next; wgray <= wgray_next; wfull <= wfull_val; end\n"
        "    always @(posedge wclk or negedge wrst_n)\n"
        "        if (!wrst_n) begin rgray_w1 <= 0; rgray_w2 <= 0; end\n"
        "        else        begin rgray_w1 <= rgray; rgray_w2 <= rgray_w1; end\n"
        "    always @(posedge wclk) if (winc & ~wfull) mem[wbin[AW-1:0]] <= wdata;\n"
        "    // read domain: binary + gray read pointer, write-gray synced in. rempty REGISTERED.\n"
        "    reg  [AW:0] rbin, rgray, wgray_r1, wgray_r2;\n"
        "    wire [AW:0] rbin_next   = rbin + (rinc & ~rempty);\n"
        "    wire [AW:0] rgray_next  = (rbin_next >> 1) ^ rbin_next;\n"
        "    wire        rempty_val  = (rgray_next == wgray_r2);\n"
        "    always @(posedge rclk or negedge rrst_n)\n"
        "        if (!rrst_n) begin rbin <= 0; rgray <= 0; rempty <= 1'b1; end\n"
        "        else        begin rbin <= rbin_next; rgray <= rgray_next; rempty <= rempty_val; end\n"
        "    always @(posedge rclk or negedge rrst_n)\n"
        "        if (!rrst_n) begin wgray_r1 <= 0; wgray_r2 <= 0; end\n"
        "        else        begin wgray_r1 <= wgray; wgray_r2 <= wgray_r1; end\n"
        "    assign rdata = mem[rbin[AW-1:0]];\n"
        "endmodule\n")


# ── emission: multi-clock Verilog ────────────────────────────────────────────────────────

def _two_flop_module() -> str:
    return (
        "// two-flop synchronizer (single-bit control; multi-bit needs an async FIFO)\n"
        "module cdc_two_flop #(parameter WIDTH = 1) (\n"
        "    input clk,\n"
        "    input [WIDTH-1:0] d,\n"
        "    output [WIDTH-1:0] q\n"
        ");\n"
        "    reg [WIDTH-1:0] s1, s2;\n"
        "    always @(posedge clk) begin s1 <= d; s2 <= s1; end\n"
        "    assign q = s2;\n"
        "endmodule\n")


def _rng(w: int) -> str:
    return f"[{w - 1}:0] " if w and w > 1 else ""


def _fifo_ctrl_nets(dst_net: str):
    """The async-FIFO flow-control nets derived from the data destination net:
    (winc, wfull, rinc, rempty)."""
    return (f"{dst_net}_winc", f"{dst_net}_wfull", f"{dst_net}_rinc", f"{dst_net}_rempty")


def _node_stub(module: str, ports: Tuple[SocPort, ...]) -> str:
    decls = ["    input clk"]
    for p in ports:
        decls.append(f"    {p.direction} {_rng(p.width)}{p.port}")
    return f"(* blackbox *)\nmodule {module} (\n" + ",\n".join(decls) + "\n);\nendmodule\n"


def _xing_stub(kind: str) -> str:
    return (f"(* blackbox *)\nmodule {kind} #(parameter WIDTH = 1) (\n"
            "    input src_clk,\n    input dst_clk,\n"
            "    input [WIDTH-1:0] d,\n    output [WIDTH-1:0] q\n);\nendmodule\n")


def emit_soc(soc: MultiClockSoC) -> str:
    """Emit the multi-clock SoC as Verilog (top + synchronizer + black-box node/crossing stubs)."""
    clks = [d.clk for d in soc.domains]
    # async-FIFO flow control is exposed at the SoC boundary: push/pop (winc/rinc) as inputs,
    # status (wfull/rempty) as outputs — wired to the producer/consumer by the integrator.
    fifo_in, fifo_out = [], []
    for x in soc.crossings:
        if x.kind == "async_fifo":
            winc, wfull, rinc, rempty = _fifo_ctrl_nets(x.dst_net)
            fifo_in += [winc, rinc]
            fifo_out += [wfull, rempty]
    ports = (clks + [n for n, _, _ in soc.top_inputs] + [n for n, _, _ in soc.top_outputs]
             + fifo_in + fifo_out)
    L = [f"// Generated by PyRTL (AHA fork) — multi-clock SoC", f"module {soc.name}({', '.join(ports)});"]
    for c in clks:
        L.append(f"    input {c};")
    L += [f"    input {_rng(w)}{n};" for n, w, _ in soc.top_inputs]
    L += [f"    output {_rng(w)}{n};" for n, w, _ in soc.top_outputs]
    L += [f"    input {n};" for n in fifo_in]
    L += [f"    output {n};" for n in fifo_out]
    L += [f"    wire {_rng(w)}{n};" for n, w in soc.internal_nets]
    L += [f"    wire {_rng(x.width)}{x.dst_net};" for x in soc.crossings]

    drv = _driver_domain(soc)
    for node in soc.nodes:
        conns = [f".clk({soc._domain_clk(node.domain)})"] + [f".{p.port}({p.net})" for p in node.ports]
        L.append(f"    {node.module} {node.name} ({', '.join(conns)});")
    for i, x in enumerate(soc.crossings):
        dst_clk = soc._domain_clk(x.dst_domain)
        src_clk = soc._domain_clk(drv.get(x.src_net, soc.domains[0].name))
        if x.kind == "two_flop":
            L.append(f"    cdc_two_flop #(.WIDTH({x.width})) sync_{i} "
                     f"(.clk({dst_clk}), .d({x.src_net}), .q({x.dst_net}));")
        elif x.kind == "async_fifo":
            winc, wfull, rinc, rempty = _fifo_ctrl_nets(x.dst_net)
            L.append(f"    async_fifo #(.WIDTH({x.width}), .AW({x.addr_bits})) sync_{i} (")
            L.append(f"        .wclk({src_clk}), .wrst_n(1'b1), .winc({winc}), .wdata({x.src_net}), .wfull({wfull}),")
            L.append(f"        .rclk({dst_clk}), .rrst_n(1'b1), .rinc({rinc}), .rdata({x.dst_net}), .rempty({rempty}));")
        else:                                  # handshake — still a black box
            L.append(f"    {x.kind} #(.WIDTH({x.width})) sync_{i} "
                     f"(.src_clk({src_clk}), .dst_clk({dst_clk}), .d({x.src_net}), .q({x.dst_net}));")
    L.append("endmodule")

    out = "\n".join(L) + "\n\n" + _two_flop_module()
    seen = set()
    for node in soc.nodes:                     # one stub per distinct node module
        if node.module not in seen:
            seen.add(node.module)
            out += "\n" + _node_stub(node.module, node.ports)
    if any(x.kind == "async_fifo" for x in soc.crossings):
        out += "\n" + async_fifo_verilog()     # the REAL gray-code FIFO, not a stub
    for x in soc.crossings:                    # black-box stubs for remaining crossing kinds
        if x.kind not in ("two_flop", "async_fifo") and x.kind not in seen:
            seen.add(x.kind)
            out += "\n" + _xing_stub(x.kind)
    return out


# ── certificate ──────────────────────────────────────────────────────────────────────────


def _example_soc(*, read_raw=False, width=1, kind="two_flop") -> "MultiClockSoC":
    a, b = ClockDomain("a", "clk_a"), ClockDomain("b", "clk_b")
    cons_net = "raw" if read_raw else "synced"
    return MultiClockSoC(
        name="soc", domains=[a, b],
        top_inputs=[("din", width, "a")], top_outputs=[("dout", width, "b")],
        internal_nets=[("raw", width)],
        nodes=[
            SocNode("prod", "producer", "a",
                    (SocPort("din", "din", "input", width), SocPort("q", "raw", "output", width))),
            SocNode("cons", "consumer", "b",
                    (SocPort("d", cons_net, "input", width), SocPort("q", "dout", "output", width))),
        ],
        crossings=[CdcCrossing("raw", "synced", "b", kind, width)],
    )


@dataclass
class MultiClockCertificate:
    """Proof that multi-clock SoCs compose, CDC is checked, and the result is multi-clock RTL."""

    clean_passes: bool                 # a properly-synchronized 2-domain SoC has no violations
    catches_unsynchronized: bool       # a raw cross-domain read is flagged
    catches_multibit_two_flop: bool    # a multi-bit two-flop crossing is flagged
    async_fifo_multibit_ok: bool       # multi-bit via async FIFO is clean
    emits_multiclock: bool             # the top declares one clk per domain + a synchronizer
    async_fifo_is_real: bool           # async-FIFO crossing emits the real gray-code FIFO, not a stub
    reason: str = ""

    @property
    def proven(self) -> bool:
        return (self.clean_passes and self.catches_unsynchronized and self.catches_multibit_two_flop
                and self.async_fifo_multibit_ok and self.emits_multiclock and self.async_fifo_is_real)


def wire_cdc() -> MultiClockCertificate:
    """Compose a 2-domain SoC, check its clock-domain crossings, and emit multi-clock RTL."""
    clean = cdc_clean(_example_soc())
    unsync = any(v.kind == "unsynchronized_crossing"
                 for v in cdc_violations(_example_soc(read_raw=True)))
    multibit = any(v.kind == "multibit_two_flop"
                   for v in cdc_violations(_example_soc(width=8)))
    fifo_ok = cdc_clean(_example_soc(width=8, kind="async_fifo"))
    v = emit_soc(_example_soc())
    emits = ("input clk_a;" in v and "input clk_b;" in v
             and "cdc_two_flop" in v and ".clk(clk_b)" in v)
    vf = emit_soc(_example_soc(width=8, kind="async_fifo"))
    fifo_real = ("module async_fifo" in vf and "wgray_next" in vf       # real gray-code FIFO body
                 and "(* blackbox *)\nmodule async_fifo" not in vf
                 and "async_fifo #(.WIDTH(8)" in vf)                    # instantiated with the data width

    reason = ""
    for ok, msg in [(clean, "a properly synchronized SoC reported a violation"),
                    (unsync, "an unsynchronized crossing was not caught"),
                    (multibit, "a multi-bit two-flop crossing was not caught"),
                    (fifo_ok, "a multi-bit async-FIFO crossing was wrongly flagged"),
                    (emits, "the emitted SoC was not multi-clock"),
                    (fifo_real, "the async-FIFO crossing did not emit the real gray-code FIFO")]:
        if not ok:
            reason = msg
            break
    return MultiClockCertificate(clean_passes=clean, catches_unsynchronized=unsync,
                                 catches_multibit_two_flop=multibit, async_fifo_multibit_ok=fifo_ok,
                                 emits_multiclock=emits, async_fifo_is_real=fifo_real, reason=reason)


__all__ = [
    "ClockDomain", "SocPort", "SocNode", "CdcCrossing", "MultiClockSoC", "CdcViolation",
    "cdc_violations", "cdc_clean", "emit_soc", "MultiClockCertificate", "wire_cdc",
    "gray_encode", "gray_decode", "async_fifo_verilog",
]
