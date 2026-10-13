"""AHA fork: hard-IP / macro instantiation (black-box cells).

PyRTL is a flat-netlist generator: every block is RTL it elaborates itself, and the fork's
structural :class:`~pyrtl.modular.Instance` still requires a PyRTL :class:`Block` behind each
instance. Real designs also instantiate cells that have NO RTL we own — they are BLACK BOXES
whose implementation the synthesis / place-and-route tool (or a vendor library) provides:

  * foundry IP — SRAM-compiler macros, PLLs, PHYs, IO cells;
  * FPGA vendor primitives — Xilinx UNISIM (RAMB36E2, DSP48E2, BUFG, IBUF/OBUF), Intel/Altera
    (altsyncram, lpm_mult);
  * pre-hardened AHA IPs — e.g. an eFPGA region dropped into an SoC.

A :class:`HardMacro` declares such a cell by its INTERFACE only — module name, parameters,
and typed ports — not its implementation. The fork can then emit a correct (optionally
parameterised) Verilog instantiation plus a ``(* blackbox *)`` stub so the surrounding design
still elaborates (in a real flow the vendor library supplies the stub via ``read_verilog
-lib``; emitting our own from the declared interface keeps the design self-checking).

Composes with :mod:`pyrtl.modular`: a :class:`TopModule` carries ``macros`` (individual
black-box instances) and ``macro_arrays`` (replicated macros emitted as a ``generate``-for
loop, like :class:`~pyrtl.modular.InstanceArray`).

HONEST: a black-box macro has no behaviour the fork can SIMULATE — a design containing one
cannot be run through FastSimulation as-is. Attaching a PyRTL behavioural model for
simulation / proof (synthesize the black box, simulate the model) is a follow-on; this slice
is the instantiation + emission path (the gap that blocked using real macros at all).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# a Verilog sized/based literal like 36'h0, 256'hDEAD, 1'b0 — emitted RAW (not quoted)
_VERILOG_LITERAL = re.compile(r"^-?\d+'[bodhBODH]", re.ASCII)


@dataclass(frozen=True)
class MacroPort:
    """A black-box port: name, direction (``input`` / ``output`` / ``inout``), bit width.

    ``is_clock`` marks a clock pin: it is still emitted on the black-box instantiation, but a
    behavioural model SKIPS it (PyRTL's clock is implicit, and ``Input("clk")`` is forbidden)."""

    name: str
    direction: str
    width: int = 1
    is_clock: bool = False

    def __post_init__(self):
        if self.direction not in ("input", "output", "inout"):
            raise ValueError(f"port {self.name!r}: bad direction {self.direction!r}")


@dataclass(frozen=True)
class HardMacro:
    """A black-box hard IP / macro, defined by its INTERFACE (not a PyRTL Block).

    ``sim_model`` (optional) is a PyRTL builder ``fn(inputs) -> outputs`` that gives the macro
    a BEHAVIOUR for simulation / proof: ``inputs`` maps each non-clock input-port name to a
    driven ``WireVector``, and it returns a dict mapping each output-port name to a produced
    ``WireVector``. With a model the macro is simulatable (splice the model) AND synthesizable
    (emit the black box); without one it is emit-only (opaque to simulation)."""

    module: str                                   # the cell's Verilog module name
    ports: Tuple[MacroPort, ...]
    parameters: Dict[str, Any] = field(default_factory=dict)  # default param values
    vendor: str = ""                              # "xilinx" | "intel" | "foundry" | "aha" | ...
    sim_model: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None

    @property
    def port_names(self) -> Tuple[str, ...]:
        return tuple(p.name for p in self.ports)

    @property
    def model_input_names(self) -> Tuple[str, ...]:
        """Input ports a model is driven with (every input except clocks)."""
        return tuple(p.name for p in self.ports if p.direction == "input" and not p.is_clock)

    @property
    def output_names(self) -> Tuple[str, ...]:
        return tuple(p.name for p in self.ports if p.direction == "output")


@dataclass(frozen=True)
class MacroInstance:
    """One black-box instantiation: a macro, an instance name, port->net connections, and
    optional per-instance parameter overrides (merged over the macro's defaults)."""

    macro: HardMacro
    name: str
    connections: Dict[str, str]                   # port name -> net expression
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroArray:
    """A REPLICATED black-box instantiation, emitted as a ``generate``-for loop (like
    :class:`~pyrtl.modular.InstanceArray`). ``connections`` are net expressions in the
    ``genvar`` (e.g. ``din[i*36 +: 36]``)."""

    macro: HardMacro
    name: str                                     # generate-block label + instance base
    count: int
    connections: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)
    genvar: str = "i"


def _param_to_verilog(value: Any) -> str:
    """Render a Verilog parameter value: ints as-is, bools as 1/0, Verilog sized literals
    (``36'h0``) raw, every other string quoted (e.g. a mode like ``"READ_FIRST"``)."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value if _VERILOG_LITERAL.match(value) else f'"{value}"'
    raise TypeError(f"unsupported parameter value type: {value!r}")


def _params_clause(macro: HardMacro, override: Dict[str, Any]) -> str:
    """``#(.NAME(val), ...)`` for the macro's defaults merged with overrides (empty if none)."""
    merged = {**macro.parameters, **(override or {})}
    unknown = set(override or {}) - set(macro.parameters)
    # overrides MAY introduce params not in the declared defaults (vendor cells have many);
    # keep them, but emit declared ones first for stable, diffable output.
    keys = list(macro.parameters) + [k for k in (override or {}) if k in unknown]
    if not merged:
        return ""
    body = ", ".join(f".{k}({_param_to_verilog(merged[k])})" for k in keys)
    return f" #({body})"


def _check_connections(macro: HardMacro, connections: Dict[str, str], where: str) -> None:
    unknown = set(connections) - set(macro.port_names)
    if unknown:
        raise ValueError(f"{where}: unknown ports {sorted(unknown)} on macro "
                         f"{macro.module!r} (ports: {sorted(macro.port_names)})")


def emit_blackbox_stub(macro: HardMacro) -> str:
    """A ``(* blackbox *) module ...; endmodule`` stub so the design elaborates with the
    macro unresolved (yosys treats it as a black box; the vendor library supplies the real
    one downstream)."""
    lines = ["(* blackbox *)"]
    if macro.parameters:
        lines.append(f"module {macro.module} #(")
        params = [
            f"    parameter {name} = {_param_to_verilog(value)}"
            for name, value in macro.parameters.items()
        ]
        lines.append(",\n".join(params))
        lines.append(") (")
    else:
        lines.append(f"module {macro.module} (")
    decls = []
    for p in macro.ports:
        rng = f"[{p.width - 1}:0] " if p.width and p.width > 1 else ""
        decls.append(f"    {p.direction} {rng}{p.name}")
    lines.append(",\n".join(decls))
    lines.append(");")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def render_instance(inst: MacroInstance) -> str:
    """One instantiation line: ``MODULE #(params) name (.port(net), ...);``."""
    _check_connections(inst.macro, inst.connections, f"instance {inst.name!r}")
    params = _params_clause(inst.macro, inst.params)
    conns = ", ".join(f".{port}({net})" for port, net in inst.connections.items())
    return f"    {inst.macro.module}{params} {inst.name} ({conns});"


def render_array(arr: MacroArray) -> List[str]:
    """A replicated macro as a ``generate``-for loop (returns the lines; caller declares the
    genvar). Like :func:`pyrtl.modular.emit_top`'s instance-array handling."""
    _check_connections(arr.macro, arr.connections, f"macro array {arr.name!r}")
    params = _params_clause(arr.macro, arr.params)
    conns = ", ".join(f".{port}({expr})" for port, expr in arr.connections.items())
    return [
        f"    generate for ({arr.genvar} = 0; {arr.genvar} < {arr.count}; "
        f"{arr.genvar} = {arr.genvar} + 1) begin : {arr.name}",
        f"        {arr.macro.module}{params} {arr.name}_inst ({conns});",
        "    end endgenerate",
    ]


def collect_blackbox_stubs(macros: List[HardMacro]) -> str:
    """One stub per DISTINCT macro module (dedup by name), in stable order."""
    seen: Dict[str, HardMacro] = {}
    for m in macros:
        seen.setdefault(m.module, m)
    return "\n".join(emit_blackbox_stub(seen[name]) for name in sorted(seen))


# ── behavioural model: simulate / splice / prove a macro (when it has a sim_model) ───────


def _require_model(macro: HardMacro) -> None:
    if macro.sim_model is None:
        raise ValueError(
            f"macro {macro.module!r} has no sim_model — it is a black box (emit-only); "
            "attach a sim_model to simulate or prove it")


def _model_memory_name(module: str) -> str:
    """Return a deterministic, block-local name for behavioral macro storage.

    A single :class:`HardMacro` object can be modeled many times in one flat
    PyRTL block.  Reusing ``<module>_arr`` for every model aliases the emitted
    Verilog identifiers even though the PyRTL memories have distinct IDs.
    Preserve the legacy name for the first instance and suffix later replicas.
    The counter lives on the working block, so separate and concurrent builds
    remain isolated and deterministic.
    """
    import pyrtl

    block = pyrtl.working_block()
    attribute = "_hardip_model_memory_counts"
    counts = getattr(block, attribute, None)
    if counts is None:
        counts = {}
        setattr(block, attribute, counts)
    replica = counts.get(module, 0)
    counts[module] = replica + 1
    if replica == 0:
        return f"{module}_arr"
    return f"{module}_model{replica}_arr"


def instantiate_model(macro: HardMacro, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Splice the macro's behavioural model into the CURRENT working block: ``inputs`` maps
    each non-clock input-port name to a driven ``WireVector``; returns each output-port name
    to a produced ``WireVector``. This is how a design containing the macro becomes
    SIMULATABLE — wire in the model here, and emit the black-box instantiation (via
    :func:`render_instance`) separately for synthesis. Clocks are implicit (PyRTL's clock)."""
    _require_model(macro)
    need = set(macro.model_input_names)
    got = set(inputs)
    if need - got or got - need:
        raise ValueError(f"macro {macro.module!r} model inputs: missing {sorted(need - got)}, "
                         f"unexpected {sorted(got - need)}")
    outs = macro.sim_model(inputs)
    missing_out = set(macro.output_names) - set(outs)
    if missing_out:
        raise ValueError(f"macro {macro.module!r} sim_model did not produce outputs "
                         f"{sorted(missing_out)}")
    return outs


def build_model_block(macro: HardMacro):
    """Build the macro's behavioural model as a standalone PyRTL ``Block`` (input ports as
    ``Input``, output ports as ``Output``) — simulatable and provable on its own."""
    import pyrtl

    _require_model(macro)
    blk = pyrtl.Block()
    with pyrtl.set_working_block(blk, no_sanity_check=True):
        inputs = {name: pyrtl.Input(_width(macro, name), name) for name in macro.model_input_names}
        outs = instantiate_model(macro, inputs)
        for name in macro.output_names:
            o = pyrtl.Output(_width(macro, name), name)
            o <<= outs[name]
    blk.sanity_check()
    return blk


def _width(macro: HardMacro, port_name: str) -> int:
    for p in macro.ports:
        if p.name == port_name:
            return p.width
    raise KeyError(port_name)


def simulate_macro(macro: HardMacro, stimulus: List[Dict[str, int]]) -> List[Dict[str, int]]:
    """Run the macro's behavioural model over ``stimulus`` (per-cycle ``{input_port: value}``)
    and return the per-cycle ``{output_port: value}`` stream. Characterises / unit-tests the
    model (works for stateful macros like memories too — it is a standalone block, no flatten)."""
    import pyrtl

    blk = build_model_block(macro)
    tracer = pyrtl.SimulationTrace(block=blk)
    sim = pyrtl.FastSimulation(tracer=tracer, block=blk)
    out: List[Dict[str, int]] = []
    for s in stimulus:
        sim.step({k: s[k] for k in macro.model_input_names})
        out.append({name: sim.inspect(name) for name in macro.output_names})
    return out


def macro_model_equivalent(macro: HardMacro, reference: Callable[[List[Dict[str, int]]], List[Dict[str, int]]],
                           stimulus: List[Dict[str, int]]) -> bool:
    """Cosim: the macro's PyRTL model produces the same per-cycle output stream as a Python
    ``reference(stimulus)`` (the spec). The honest "model == spec" equivalence — synthesize
    the black box, but trust its simulated behaviour because it matches the reference."""
    return simulate_macro(macro, stimulus) == reference(stimulus)


# ── a starter library of common primitives (interface only) ──────────────────────────────


def foundry_sram_1rw(module: str, depth: int, width: int, *, vendor: str = "foundry") -> HardMacro:
    """A generic single-read/write-port SRAM-compiler macro (clk/ce/we/addr/din/dout), with a
    behavioural model (synchronous write on CE&WE, asynchronous read) so it is simulatable."""
    aw = max((depth - 1).bit_length(), 1)

    def _model(inputs):
        import pyrtl

        # The generic interface's Q port is an asynchronous read; callers add an explicit
        # output register when targeting a synchronous compiler macro.  Keep the behavioral
        # model aligned with that documented contract and allow a decoded/muxed 1RW address.
        mem = pyrtl.MemBlock(
            bitwidth=width, addrwidth=aw, name=_model_memory_name(module), asynchronous=True)
        mem[inputs["A"]] <<= pyrtl.MemBlock.EnabledWrite(
            inputs["D"], enable=inputs["WE"] & inputs["CE"])
        return {"Q": mem[inputs["A"]]}

    return HardMacro(
        module=module,
        vendor=vendor,
        parameters={"DEPTH": depth, "WIDTH": width},
        sim_model=_model,
        ports=(
            MacroPort("CLK", "input", is_clock=True),
            MacroPort("CE", "input"),
            MacroPort("WE", "input"),
            MacroPort("A", "input", aw),
            MacroPort("D", "input", width),
            MacroPort("Q", "output", width),
        ),
    )


def foundry_sram_1rw_byte_masked(
    module: str,
    depth: int,
    width: int,
    *,
    vendor: str = "foundry",
) -> HardMacro:
    """Single-port SRAM-compiler macro with one write enable per byte.

    ``WEM[byte]`` updates byte lane ``byte`` on ``CE & WE``.  The storage
    remains an explicit foundry boundary; the byte-array model only supplies
    bit-exact simulation/proof behavior for the declared macro contract.
    """
    if width < 8 or width % 8:
        raise ValueError("byte-masked SRAM width must be a positive byte multiple")
    aw = max((depth - 1).bit_length(), 1)
    byte_count = width // 8

    def _model(inputs):
        import pyrtl

        mem = pyrtl.MemBlock(
            bitwidth=width,
            addrwidth=aw,
            name=_model_memory_name(module),
            asynchronous=True,
            max_read_ports=1,
            max_write_ports=1,
        )
        old = mem[inputs["A"]]
        lanes = []
        for byte in range(byte_count):
            lanes.append(
                pyrtl.select(
                    inputs["WEM"][byte],
                    truecase=inputs["D"][byte * 8:(byte + 1) * 8],
                    falsecase=old[byte * 8:(byte + 1) * 8],
                )
            )
        merged = pyrtl.concat(*reversed(lanes))
        mem[inputs["A"]] <<= pyrtl.MemBlock.EnabledWrite(
            merged,
            enable=inputs["CE"] & inputs["WE"],
        )
        return {"Q": old}

    return HardMacro(
        module=module,
        vendor=vendor,
        parameters={"DEPTH": depth, "WIDTH": width, "MASK_WIDTH": byte_count},
        sim_model=_model,
        ports=(
            MacroPort("CLK", "input", is_clock=True),
            MacroPort("CE", "input"),
            MacroPort("WE", "input"),
            MacroPort("WEM", "input", byte_count),
            MacroPort("A", "input", aw),
            MacroPort("D", "input", width),
            MacroPort("Q", "output", width),
        ),
    )


def foundry_sram_1rw_byte_masked_registered_read(
    module: str,
    depth: int,
    width: int,
    *,
    vendor: str = "foundry",
) -> HardMacro:
    """Byte-masked 1RW SRAM with a one-cycle, clock-enabled read output.

    On ``CE``, ``Q`` captures the pre-write contents of ``A`` at the rising
    edge.  A simultaneous masked write commits at that edge, so read-during-
    write is explicitly old-data.  When ``CE`` is low, ``Q`` holds.
    """
    if width < 8 or width % 8:
        raise ValueError("byte-masked SRAM width must be a positive byte multiple")
    aw = max((depth - 1).bit_length(), 1)
    byte_count = width // 8

    def _model(inputs):
        import pyrtl

        mem = pyrtl.MemBlock(
            bitwidth=width,
            addrwidth=aw,
            name=_model_memory_name(module),
            asynchronous=True,
            max_read_ports=1,
            max_write_ports=1,
        )
        old = mem[inputs["A"]]
        q = pyrtl.Register(width, f"{module}_registered_q", reset_value=0)
        q.next <<= pyrtl.select(inputs["CE"], truecase=old, falsecase=q)
        lanes = []
        for byte in range(byte_count):
            lanes.append(
                pyrtl.select(
                    inputs["WEM"][byte],
                    truecase=inputs["D"][byte * 8:(byte + 1) * 8],
                    falsecase=old[byte * 8:(byte + 1) * 8],
                )
            )
        merged = pyrtl.concat(*reversed(lanes))
        mem[inputs["A"]] <<= pyrtl.MemBlock.EnabledWrite(
            merged,
            enable=inputs["CE"] & inputs["WE"],
        )
        return {"Q": q}

    return HardMacro(
        module=module,
        vendor=vendor,
        parameters={"DEPTH": depth, "WIDTH": width, "MASK_WIDTH": byte_count},
        sim_model=_model,
        ports=(
            MacroPort("CLK", "input", is_clock=True),
            MacroPort("CE", "input"),
            MacroPort("WE", "input"),
            MacroPort("WEM", "input", byte_count),
            MacroPort("A", "input", aw),
            MacroPort("D", "input", width),
            MacroPort("Q", "output", width),
        ),
    )


def foundry_sram_1r1w(
    module: str,
    depth: int,
    width: int,
    *,
    vendor: str = "foundry",
) -> HardMacro:
    """A one-read/one-write SRAM macro with independent addresses.

    The behavioral contract is asynchronous read plus synchronous write.  It
    deliberately models one physical write port; callers that need multiple
    architectural writes must bank/arbitrate before the macro boundary.
    """
    aw = max((depth - 1).bit_length(), 1)

    def _model(inputs):
        import pyrtl

        mem = pyrtl.MemBlock(
            bitwidth=width,
            addrwidth=aw,
            name=_model_memory_name(module),
            asynchronous=True,
            max_read_ports=1,
            max_write_ports=1,
        )
        mem[inputs["WA"]] <<= pyrtl.MemBlock.EnabledWrite(
            inputs["D"],
            enable=inputs["WE"] & inputs["CE"],
        )
        return {"Q": mem[inputs["RA"]]}

    return HardMacro(
        module=module,
        vendor=vendor,
        parameters={"DEPTH": depth, "WIDTH": width},
        sim_model=_model,
        ports=(
            MacroPort("CLK", "input", is_clock=True),
            MacroPort("CE", "input"),
            MacroPort("WE", "input"),
            MacroPort("RA", "input", aw),
            MacroPort("WA", "input", aw),
            MacroPort("D", "input", width),
            MacroPort("Q", "output", width),
        ),
    )


def xilinx_ramb36e2() -> HardMacro:
    """A (simplified) Xilinx UltraScale block-RAM primitive interface."""
    return HardMacro(
        module="RAMB36E2",
        vendor="xilinx",
        parameters={"READ_WIDTH_A": 36, "WRITE_WIDTH_A": 36, "DOA_REG": 0},
        ports=(
            MacroPort("CLKARDCLK", "input", is_clock=True),
            MacroPort("ENARDEN", "input"),
            MacroPort("WEA", "input", 4),
            MacroPort("ADDRARDADDR", "input", 15),
            MacroPort("DINADIN", "input", 32),
            MacroPort("DOUTADOUT", "output", 32),
        ),
    )


def xilinx_dsp48e2() -> HardMacro:
    """A (simplified) Xilinx DSP48E2 multiply-add primitive interface, with a FUNCTIONAL model
    P = (A*B + C) truncated to 48b (the pipeline registers AREG/BREG are not modelled — this
    is a functional, not cycle-accurate, model; honest)."""

    def _model(inputs):
        return {"P": (inputs["A"] * inputs["B"] + inputs["C"])[0:48]}

    return HardMacro(
        module="DSP48E2",
        vendor="xilinx",
        parameters={"USE_MULT": "MULTIPLY", "AREG": 1, "BREG": 1},
        sim_model=_model,
        ports=(
            MacroPort("CLK", "input", is_clock=True),
            MacroPort("A", "input", 30),
            MacroPort("B", "input", 18),
            MacroPort("C", "input", 48),
            MacroPort("P", "output", 48),
        ),
    )


def xilinx_bufg() -> HardMacro:
    """A global clock buffer."""
    return HardMacro(module="BUFG", vendor="xilinx",
                     ports=(MacroPort("I", "input"), MacroPort("O", "output")))


def aha_efpga_region(module: str = "aha_efpga_region", *, n_io: int = 32,
                     config_bits: int = 1024) -> HardMacro:
    """A pre-hardened AHA eFPGA region: a configurable-fabric black box dropped into an SoC
    (loaded over a config port, exposes a bidirectional IO bus)."""
    return HardMacro(
        module=module,
        vendor="aha",
        parameters={"N_IO": n_io, "CONFIG_BITS": config_bits},
        ports=(
            MacroPort("clk", "input", is_clock=True),
            MacroPort("rst", "input"),
            MacroPort("cfg_en", "input"),
            MacroPort("cfg_data", "input"),
            MacroPort("io_in", "input", n_io),
            MacroPort("io_out", "output", n_io),
        ),
    )


# ── certificate ──────────────────────────────────────────────────────────────────────────


@dataclass
class MacroModelCertificate:
    """Proof that a macro's behavioural model lets a design with hard IP be simulated, while
    the synthesized form stays the black box."""

    sram_model_matches_spec: bool   # the SRAM model == a reference memory (cosim)
    dsp_model_matches_spec: bool    # the DSP model computes A*B+C
    design_with_macro_simulates: bool  # a design splicing the model reads back what it wrote
    emit_is_blackbox_not_model: bool   # synthesis emits the black box; the model is never emitted
    deterministic: bool
    reason: str = ""

    @property
    def proven(self) -> bool:
        return (self.sram_model_matches_spec and self.dsp_model_matches_spec
                and self.design_with_macro_simulates and self.emit_is_blackbox_not_model
                and self.deterministic)


def wire_macro_model() -> MacroModelCertificate:
    """MEAS/hardip: a hard macro with a behavioural model is SIMULATABLE (and provable against
    a spec) yet still emits as a synthesizable black box."""
    import pyrtl

    sram = foundry_sram_1rw("sram16x8", 16, 8)
    dsp = xilinx_dsp48e2()

    # SRAM model == a reference memory (async read of current contents, sync write)
    stim = [{"CE": 1, "WE": 1, "A": a, "D": (a * 13 + 1) & 0xFF} for a in range(16)]
    stim += [{"CE": 1, "WE": 0, "A": a, "D": 0} for a in range(16)]

    def sram_ref(s_list):
        state, out = {}, []
        for s in s_list:
            out.append({"Q": state.get(s["A"], 0)})       # async read of current contents
            if s["WE"] & s["CE"]:
                state[s["A"]] = s["D"]                     # sync write (visible next cycle)
        return out

    sram_ok = macro_model_equivalent(sram, sram_ref, stim)

    dsp_stim = [{"A": (i * 101) & 0x3FFFFFFF, "B": (i * 7) & 0x3FFFF, "C": (i * 5) & 0xFFFF}
                for i in range(1, 12)]
    dsp_ref = [{"P": (s["A"] * s["B"] + s["C"]) & ((1 << 48) - 1)} for s in dsp_stim]
    dsp_ok = simulate_macro(dsp, dsp_stim) == dsp_ref

    # a design splicing the SRAM model reads back what it wrote
    pyrtl.reset_working_block()
    a = pyrtl.Input(4, "a")
    din = pyrtl.Input(8, "din")
    we = pyrtl.Input(1, "we")
    o = pyrtl.Output(8, "o")
    outs = instantiate_model(sram, {"CE": pyrtl.Const(1, 1), "WE": we, "A": a, "D": din})
    o <<= outs["Q"]
    sim = pyrtl.Simulation()
    sim.step({"a": 9, "din": 0x5A, "we": 1})
    sim.step({"a": 9, "din": 0, "we": 0})
    design_ok = sim.inspect("o") == 0x5A

    # synthesis emits the black box; the model's internals (a MemBlock) are never in the emit
    inst_v = render_instance(MacroInstance(sram, "u_sram",
                             {"CLK": "clk", "CE": "1'b1", "WE": "we", "A": "a", "D": "d", "Q": "q"}))
    emit_blackbox = ("sram16x8" in inst_v and "_arr" not in inst_v and "MemBlock" not in inst_v)

    deterministic = simulate_macro(sram, stim) == simulate_macro(sram, stim)

    reason = ""
    for ok, msg in [
        (sram_ok, "SRAM model did not match the reference memory"),
        (dsp_ok, "DSP model did not compute A*B+C"),
        (design_ok, "a design with the spliced macro model did not read back its write"),
        (emit_blackbox, "emission leaked the model instead of the black box"),
        (deterministic, "macro model simulation was not deterministic"),
    ]:
        if not ok:
            reason = msg
            break

    return MacroModelCertificate(
        sram_model_matches_spec=sram_ok, dsp_model_matches_spec=dsp_ok,
        design_with_macro_simulates=design_ok, emit_is_blackbox_not_model=emit_blackbox,
        deterministic=deterministic, reason=reason)


__all__ = [
    "MacroPort", "HardMacro", "MacroInstance", "MacroArray",
    "emit_blackbox_stub", "render_instance", "render_array", "collect_blackbox_stubs",
    "instantiate_model", "build_model_block", "simulate_macro", "macro_model_equivalent",
    "MacroModelCertificate", "wire_macro_model",
    "foundry_sram_1rw", "foundry_sram_1rw_byte_masked",
    "foundry_sram_1rw_byte_masked_registered_read", "foundry_sram_1r1w",
    "xilinx_ramb36e2", "xilinx_dsp48e2", "xilinx_bufg", "aha_efpga_region",
]
