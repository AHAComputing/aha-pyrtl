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

Single clock domain. Memories inside a leaf are cloned per instance when flattening,
so architectural state remains isolated exactly as it is in modular Verilog.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Tuple

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


@dataclass(frozen=True)
class InlineInstance:
    """One hierarchy module cloned into an existing flat working block.

    ``internal_wires`` and ``memories`` are returned for build accounting,
    debug, and state-isolation checks. Ports remain the exact caller-supplied
    wires in ``connections``.
    """

    module: Module
    name: str
    connections: Mapping[str, WireVector]
    internal_wires: Tuple[WireVector, ...]
    memories: Tuple[object, ...]


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


def _validate_connections(
    mod: Module,
    instance_name: str,
    connections: Mapping[str, object],
) -> None:
    ports = set(mod.input_names) | set(mod.output_names)
    missing = ports - set(connections)
    extra = set(connections) - ports
    if missing or extra:
        raise ValueError(
            f"instance {instance_name!r} of {mod.name!r}: missing ports "
            f"{sorted(missing)}, unknown ports {sorted(extra)}"
        )
    widths = dict(mod.inputs + mod.outputs)
    for port_name, wire in connections.items():
        if not isinstance(wire, WireVector):
            raise TypeError(
                f"instance {instance_name!r} port {port_name!r} must connect "
                "to a WireVector"
            )
        if wire.bitwidth != widths[port_name]:
            raise ValueError(
                f"instance {instance_name!r} port {port_name!r} width "
                f"{wire.bitwidth} does not match module width {widths[port_name]}"
            )


def _clone_module_into_block(
    mod: Module,
    instance_name: str,
    connections: Mapping[str, WireVector],
    target: Block,
) -> InlineInstance:
    """Clone ``mod`` once into ``target`` using validated port bindings."""

    wire_map: Dict[object, object] = {
        mod.port_wires[port_name]: wire
        for port_name, wire in connections.items()
    }
    mem_map: Dict[object, object] = {}
    internal_wires: List[WireVector] = []
    with set_working_block(target, no_sanity_check=True):
        for wire in mod.block.wirevector_subset():
            if wire in wire_map:
                continue
            cloned = clone_wire(wire, name=f"{instance_name}_{wire.name}")
            wire_map[wire] = cloned
            internal_wires.append(cloned)
        for logic_net in mod.block.logic:
            new_args = tuple(wire_map[arg] for arg in logic_net.args)
            new_dests = tuple(wire_map[dest] for dest in logic_net.dests)
            new_param = logic_net.op_param
            cloned_mem = None
            if logic_net.op in "m@":
                _, old_mem = logic_net.op_param
                cloned_mem = mem_map.get(old_mem)
                if cloned_mem is None:
                    cloned_mem = old_mem._make_copy(target)
                    cloned_mem.name = f"{instance_name}_{old_mem.name}"
                    mem_map[old_mem] = cloned_mem
                new_param = (cloned_mem.id, cloned_mem)
            new_net = LogicNet(
                logic_net.op,
                new_param,
                args=new_args,
                dests=new_dests,
            )
            target.add_net(new_net)
            if cloned_mem is not None:
                if logic_net.op == "m":
                    cloned_mem.readport_nets.append(new_net)
                    cloned_mem.num_read_ports += 1
                else:
                    cloned_mem.writeport_nets.append(new_net)
                    cloned_mem.num_write_ports += 1
    return InlineInstance(
        module=mod,
        name=instance_name,
        connections=dict(connections),
        internal_wires=tuple(internal_wires),
        memories=tuple(mem_map.values()),
    )


def instantiate_inline(
    mod: Module,
    instance_name: str,
    connections: Mapping[str, WireVector],
    *,
    block: Block | None = None,
) -> InlineInstance:
    """Instantiate a pre-elaborated module into an existing flat block.

    This is the composition seam for a larger PyRTL builder that already owns
    surrounding control, FIFOs, or interconnect. The leaf is elaborated once
    by :func:`module`; each call clones only its netlist and isolated state
    into ``block`` (the current working block by default).

    Port names and widths are checked before mutation. Instance names must be
    unique within the target block. Call ``block.sanity_check()`` after all
    surrounding wiring is complete.
    """

    if not isinstance(mod, Module):
        raise TypeError("instantiate_inline requires a hierarchy.Module")
    if not instance_name:
        raise ValueError("inline instance name must be non-empty")
    target = block if block is not None else working_block()
    _validate_connections(mod, instance_name, connections)
    for port_name, wire in connections.items():
        if wire not in target.wirevector_set:
            raise ValueError(
                f"instance {instance_name!r} port {port_name!r} connects to "
                "a wire outside the target block"
            )
    used = getattr(target, "_hierarchy_inline_instances", None)
    if used is None:
        used = set()
        setattr(target, "_hierarchy_inline_instances", used)
    if instance_name in used:
        raise ValueError(
            f"duplicate inline instance name {instance_name!r} in target block"
        )
    used.add(instance_name)
    return _clone_module_into_block(mod, instance_name, connections, target)


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
        self._design_instance_scopes: set[str] = set()

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
        ports = set(mod.input_names) | set(mod.output_names)
        missing = ports - set(connections)
        extra = set(connections) - ports
        if missing or extra:
            raise ValueError(f"instance {inst_name!r} of {mod.name!r}: missing ports {sorted(missing)}, "
                             f"unknown ports {sorted(extra)}")
        widths = dict(mod.inputs + mod.outputs)
        for port_name, net in connections.items():
            if net.width != widths[port_name]:
                raise ValueError(
                    f"instance {inst_name!r} port {port_name!r} width "
                    f"{net.width} does not match module width {widths[port_name]}"
                )
        self._fresh(inst_name)
        self._instances.append(_Instance(module=mod, name=inst_name, connections=dict(connections)))

    def instantiate_design(
        self,
        child: "Design",
        inst_name: str,
        connections: Dict[str, Net],
    ) -> None:
        """Compose one structural design into another without flattening leaves.

        The child's top-level inputs and outputs bind to ``connections``. Its
        internal nets and leaf instances are imported under ``inst_name``.
        This expands one structural level while preserving reusable leaf
        modules in :meth:`emit_verilog`.
        """

        if not isinstance(child, Design):
            raise TypeError("instantiate_design requires a hierarchy.Design")
        if not inst_name:
            raise ValueError("design instance name must be non-empty")
        if inst_name in self._design_instance_scopes:
            raise ValueError(f"duplicate design instance name {inst_name!r}")

        child_ports = {
            net.name: net for net in (*child._inputs, *child._outputs)
        }
        missing = set(child_ports) - set(connections)
        extra = set(connections) - set(child_ports)
        if missing or extra:
            raise ValueError(
                f"design instance {inst_name!r} of {child.name!r}: missing "
                f"ports {sorted(missing)}, unknown ports {sorted(extra)}"
            )
        for port_name, net in connections.items():
            if not isinstance(net, Net):
                raise TypeError(
                    f"design instance {inst_name!r} port {port_name!r} "
                    "must connect to a hierarchy Net"
                )
            if net.width != child_ports[port_name].width:
                raise ValueError(
                    f"design instance {inst_name!r} port {port_name!r} width "
                    f"{net.width} does not match design width "
                    f"{child_ports[port_name].width}"
                )

        imported_names = [
            *(f"{inst_name}_{net.name}" for net in child._wires),
            *(
                f"{inst_name}_{instance.name}"
                for instance in child._instances
            ),
        ]
        collisions = set(imported_names) & self._names
        if collisions:
            raise ValueError(
                f"design instance {inst_name!r} collides with names "
                f"{sorted(collisions)}"
            )

        net_map = {
            child_ports[name]: net for name, net in connections.items()
        }
        for child_net in child._wires:
            net_map[child_net] = self.net(
                f"{inst_name}_{child_net.name}",
                child_net.width,
            )
        for child_instance in child._instances:
            self.instantiate(
                child_instance.module,
                f"{inst_name}_{child_instance.name}",
                {
                    port: net_map[child_net]
                    for port, child_net in child_instance.connections.items()
                },
            )
        self._design_instance_scopes.add(inst_name)

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
                _clone_module_into_block(
                    inst.module,
                    inst.name,
                    {
                        port_name: net_wire[net]
                        for port_name, net in inst.connections.items()
                    },
                    flat,
                )
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
