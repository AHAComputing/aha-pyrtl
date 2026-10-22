"""AHA fork: build-time module hierarchy (structural instances).

Upstream PyRTL is a FLAT netlist — building a replicated design (an R×C mesh, SIMT
lanes, NoC routers, vector lanes) inlines the leaf N times: O(N) elaboration, O(N)
netlist, O(N) sim, and a single giant flat module on emit.

This adds a real *instance* layer ON TOP of PyRTL (without touching the flat ``Block``
core): elaborate a leaf :class:`Module` ONCE, then ``instantiate`` it N times as
black-boxes wired through named :class:`Net` s.  The persistent design is small (one
leaf block + N light instance records), so:

  * BUILD is O(1) leaf-elaboration + O(N) instance records (not O(N) elaboration);
  * EMIT is modular for free — :meth:`Design.emit_verilog` reuses :mod:`pyrtl.modular`
    (one ``module`` per distinct leaf + a structural top instantiating them);
  * SIMULATION stays correct via :meth:`Design.flatten`, which inlines each instance's
    leaf (I/O remapped to the shared nets, internals scoped per-instance) into one flat
    Block you can hand to any pyrtl Simulation.

Single clock domain; memories inside a leaf are not yet supported (a follow-on) and
raise a clear error.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

from .core import Block, LogicNet, set_working_block, working_block
from .wire import Input, Output, WireVector
from .transform import clone_wire

Port = Tuple[str, int]


@dataclass(frozen=True)
class Module:
    """A leaf sub-circuit, elaborated once.  Use :func:`module` to build one."""

    name: str
    block: Block
    inputs: Tuple[Port, ...]                 # (port_name, width)
    outputs: Tuple[Port, ...]
    port_wires: Dict[str, object]            # port_name -> the leaf's Input/Output WireVector

    @property
    def input_names(self) -> Tuple[str, ...]:
        return tuple(n for n, _ in self.inputs)

    @property
    def output_names(self) -> Tuple[str, ...]:
        return tuple(n for n, _ in self.outputs)


def module(name: str, build: Callable[[], None]) -> Module:
    """Elaborate ``build`` ONCE into a fresh leaf block and capture its port interface.

    ``build`` must declare the leaf's ports as named ``pyrtl.Input`` / ``pyrtl.Output``.
    """
    leaf = Block()
    with set_working_block(leaf, no_sanity_check=True):
        build()
    leaf.sanity_check()
    inputs = sorted(((w.name, w.bitwidth) for w in leaf.wirevector_subset(Input)))
    outputs = sorted(((w.name, w.bitwidth) for w in leaf.wirevector_subset(Output)))
    port_wires: Dict[str, object] = {}
    for w in leaf.wirevector_subset((Input, Output)):
        port_wires[w.name] = w
    return Module(name=name, block=leaf, inputs=tuple(inputs), outputs=tuple(outputs),
                  port_wires=port_wires)


class Net:
    """A named connection point in a :class:`Design` (one driver, any readers)."""

    __slots__ = ("name", "width", "kind")

    def __init__(self, name: str, width: int, kind: str) -> None:
        self.name = name
        self.width = width
        self.kind = kind   # "input" | "output" | "wire"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Net({self.name!r}, {self.width}, {self.kind})"


@dataclass
class _Instance:
    module: Module
    name: str
    connections: Dict[str, Net]   # leaf port name -> Net


class Design:
    """A structural design: top I/O + internal nets + module instances."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._inputs: List[Net] = []
        self._outputs: List[Net] = []
        self._wires: List[Net] = []
        self._instances: List[_Instance] = []
        self._names: set = set()

    def _fresh(self, name: str) -> None:
        if name in self._names:
            raise ValueError(f"duplicate net/instance name {name!r} in design {self.name!r}")
        self._names.add(name)

    def add_input(self, name: str, width: int) -> Net:
        self._fresh(name)
        n = Net(name, width, "input")
        self._inputs.append(n)
        return n

    def add_output(self, name: str, width: int) -> Net:
        self._fresh(name)
        n = Net(name, width, "output")
        self._outputs.append(n)
        return n

    def net(self, name: str, width: int) -> Net:
        self._fresh(name)
        n = Net(name, width, "wire")
        self._wires.append(n)
        return n

    def instantiate(self, mod: Module, inst_name: str, connections: Dict[str, Net]) -> None:
        """Instantiate ``mod`` as ``inst_name``, binding leaf port names to nets."""
        self._fresh(inst_name)
        ports = set(mod.input_names) | set(mod.output_names)
        missing = ports - set(connections)
        extra = set(connections) - ports
        if missing or extra:
            raise ValueError(f"instance {inst_name!r} of {mod.name!r}: missing ports {sorted(missing)}, "
                             f"unknown ports {sorted(extra)}")
        self._instances.append(_Instance(module=mod, name=inst_name, connections=dict(connections)))

    # ── simulation: flatten (inline instances) ──────────────────────────────────

    def flatten(self) -> Block:
        """Inline every instance into ONE flat Block (for simulation). I/O ports are
        remapped to the shared nets; each instance's internals are scoped by name."""
        flat = Block()
        with set_working_block(flat, no_sanity_check=True):
            net_wire: Dict[Net, object] = {}
            for n in self._inputs:
                net_wire[n] = Input(n.width, n.name)
            for n in self._outputs:
                net_wire[n] = Output(n.width, n.name)
            for n in self._wires:
                net_wire[n] = WireVector(n.width, n.name)

            for inst in self._instances:
                leaf = inst.module.block
                if any(net.op in "m@" for net in leaf.logic):
                    raise NotImplementedError(
                        f"instance {inst.name!r}: leaf {inst.module.name!r} has a memory — "
                        "memories inside instantiated modules are not yet supported")
                wire_map: Dict[object, object] = {}
                # bind leaf ports to the shared parent nets
                for port_name, n in inst.connections.items():
                    wire_map[inst.module.port_wires[port_name]] = net_wire[n]
                # clone every other leaf wire, scoped by instance name
                for w in leaf.wirevector_subset():
                    if w in wire_map:
                        continue
                    wire_map[w] = clone_wire(w, name=f"{inst.name}_{w.name}")
                # copy the leaf's logic with args/dests remapped
                for lnet in leaf.logic:
                    new_args = tuple(wire_map[a] for a in lnet.args)
                    new_dests = tuple(wire_map[d] for d in lnet.dests)
                    flat.add_net(LogicNet(lnet.op, lnet.op_param, args=new_args, dests=new_dests))
        flat.sanity_check()
        return flat

    # ── emission: modular Verilog (reuses pyrtl.modular) ─────────────────────────

    def emit_verilog(self, **kwargs) -> Dict[str, str]:
        """Emit modular Verilog: one module per distinct leaf + a structural top."""
        from .modular import Instance, TopModule, emit_hierarchy

        submodules: Dict[str, Block] = {}
        for inst in self._instances:
            submodules.setdefault(inst.module.name, inst.module.block)
        top = TopModule(
            name=self.name,
            inputs=[(n.name, n.width) for n in self._inputs],
            outputs=[(n.name, n.width) for n in self._outputs],
            wires=[(n.name, n.width) for n in self._wires],
            instances=[Instance(inst.module.name, inst.name,
                                 {port: net.name for port, net in inst.connections.items()})
                       for inst in self._instances],
        )
        return emit_hierarchy(submodules, top, **kwargs)

    @property
    def n_instances(self) -> int:
        return len(self._instances)

    @property
    def n_distinct_modules(self) -> int:
        return len({inst.module.name for inst in self._instances})
