"""Functional lowering for an explicitly initialized, fixed-control profile.

No width reduction, retiming or memory substitution. Constant operations use
Simulation's own bit semantics. Register folding assumes the same cold values
as Simulation/default_register_value and initialized Verilog emission; it is
not equivalence over arbitrary starting register states or injected SEUs.
"""

from collections import deque
from types import SimpleNamespace

from .core import LogicNet, set_working_block
from .simulation import Simulation
from .wire import Const, Input, Output, Register


def lower_initialized_profile(block, *, default_register_value=0, max_rounds=32):
    """Fold fixed controls and remove unreachable logic, keeping all public I/O.

Memory reads remain unknown. All enabled memory writes are roots, including
unobserved writes. Every nonconstant state machine and production capacity is
retained. A round limit fails explicitly instead of returning an unfinished IR.
    """
    raw = len(block.logic)
    constants = {}
    for wire in block.wirevector_subset(Const):
        key = (len(wire), wire.val)
        old = constants.get(key)
        if old is None or wire.name < old.name:
            constants[key] = wire
    aliases = {}

    def resolve(wire):
        path = []
        while wire in aliases:
            path.append(wire)
            wire = aliases[wire]
        for old in path:
            aliases[old] = wire
        return wire

    def constant(width, value):
        value &= (1 << width) - 1
        key = width, value
        if key not in constants:
            name = f"__initialized_const_{width}_{value:x}"
            suffix = 0
            while name in block.wirevector_by_name:
                suffix += 1
                name = f"__initialized_const_{width}_{value:x}_{suffix}"
            constants[key] = Const(value, bitwidth=width, name=name, block=block)
        return constants[key]

    folded = 0
    with set_working_block(block, no_sanity_check=True):
        for round_index in range(max_rounds):
            changed = False
            logic = set()
            for net in block.iter_topological(canonical=False):
                args = tuple(resolve(wire) for wire in net.args)
                updated = LogicNet(net.op, net.op_param, args, net.dests)
                replacement = None
                if net.op == "@":
                    if isinstance(args[2], Const) and args[2].val == 0:
                        changed = True
                        folded += 1
                        continue
                elif net.op == "r":
                    register = net.dests[0]
                    initial = register.reset_value
                    if initial is None:
                        initial = default_register_value
                    if args[0] is register or (isinstance(args[0], Const) and args[0].val == initial):
                        replacement = constant(len(register), initial)
                elif net.op == "w" and len(args[0]) == len(net.dests[0]):
                    replacement = args[0]
                elif net.op == "x" and isinstance(args[0], Const):
                    replacement = args[1] if args[0].val == 0 else args[2]
                elif net.op == "x" and args[1] is args[2]:
                    replacement = args[1]
                elif net.op == "s" and net.op_param == tuple(range(len(args[0]))):
                    replacement = args[0]
                elif net.op in "&|^n" and any(isinstance(arg, Const) for arg in args) and not all(isinstance(arg, Const) for arg in args):
                    fixed, variable = args if isinstance(args[0], Const) else reversed(args)
                    mask = (1 << len(net.dests[0])) - 1
                    if fixed.val == 0:
                        if net.op in "&n":
                            replacement = constant(len(net.dests[0]), mask if net.op == "n" else 0)
                        else:
                            replacement = variable
                    elif fixed.val == mask:
                        if net.op == "|":
                            replacement = constant(len(net.dests[0]), mask)
                        elif net.op == "&":
                            replacement = variable
                elif net.op in "w~&|^n+-*<>=xcs" and all(isinstance(arg, Const) for arg in args):
                    # Reuse the authoritative primitive evaluator, including
                    # concat/slice ordering and destination-width masking.
                    evaluator = SimpleNamespace(value={arg: arg.val for arg in args},
                                                _sanitize=Simulation._sanitize)
                    Simulation._execute(evaluator, updated)
                    replacement = constant(len(net.dests[0]), evaluator.value[net.dests[0]])
                if replacement is not None and len(replacement) == len(net.dests[0]):
                    dest = net.dests[0]
                    if isinstance(dest, Output):
                        logic.add(LogicNet("w", None, (replacement,), (dest,)))
                    else:
                        aliases[dest] = replacement
                        changed = True
                        folded += 1
                else:
                    logic.add(updated)
            block.logic = logic
            if not changed:
                break
        else:
            raise RuntimeError("initialized-profile lowering did not reach a fixed point")

        # Backward reachability is linear, including register-next dependencies.
        producers = {wire: net for net in block.logic for wire in net.dests}
        roots = [net for net in block.logic if net.op == "@"
                 or any(isinstance(wire, Output) for wire in net.dests)]
        reachable = set(roots)
        pending = deque(roots)
        while pending:
            for wire in pending.popleft().args:
                producer = producers.get(wire)
                if producer is not None and producer not in reachable:
                    reachable.add(producer)
                    pending.append(producer)
        block.logic = reachable
        wires = {wire for net in reachable for wire in net.args + net.dests}
        wires.update(block.wirevector_subset((Input, Output)))
        block.wirevector_set = wires
        block.wirevector_by_name = {wire.name: wire for wire in wires}
        memories = {net.op_param[1] for net in reachable if net.op in "m@"}
        block.memblock_by_name = {mem.name: mem for mem in memories}
        for mem in memories:
            mem.readport_nets = [net for net in reachable if net.op == "m" and net.op_param[1] is mem]
            mem.writeport_nets = [net for net in reachable if net.op == "@" and net.op_param[1] is mem]
            mem.num_read_ports, mem.num_write_ports = len(mem.readport_nets), len(mem.writeport_nets)
        block.sanity_check()
    return {"raw_nets": raw, "lowered_nets": len(block.logic), "folded_nets": folded,
            "rounds": round_index + 1, "default_register_value": default_register_value}
