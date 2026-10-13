# Import order matters in this file.
# isort: skip_file

# error types thrown
from .pyrtlexceptions import PyrtlError, PyrtlInternalError

# core rtl constructs
from .core import (
    LogicNet,
    Block,
    PostSynthBlock,
    working_block,
    reset_working_block,
    set_working_block,
    temp_working_block,
    set_debug_mode,
)

# convenience classes for building hardware
from .wire import WireVector, Input, Output, Const, Register

from .gate_graph import GateGraph, Gate

# helper functions
from .helperfuncs import (
    input_list,
    output_list,
    register_list,
    wirevector_list,
    log2,
    truncate,
    match_bitpattern,
    bitpattern_to_val,
    chop,
    val_to_signed_integer,
    val_to_formatted_str,
    formatted_str_to_val,
    infer_val_and_bitwidth,
    probe,
    rtl_assert,
    check_rtl_assertions,
    find_loop,
    find_and_print_loop,
    wire_struct,
    wire_matrix,
    one_hot_to_binary,
    binary_to_one_hot,
)

from .corecircuits import (
    and_all_bits,
    or_all_bits,
    xor_all_bits,
    rtl_any,
    rtl_all,
    mux,
    select,
    concat,
    concat_list,
    parity,
    tree_reduce,
    as_wires,
    match_bitwidth,
    enum_mux,
    bitfield_update,
    bitfield_update_set,
    signed_add,
    signed_sub,
    signed_mult,
    signed_lt,
    signed_le,
    signed_gt,
    signed_ge,
    shift_left_arithmetic,
    shift_right_arithmetic,
    shift_left_logical,
    shift_right_logical,
)

# memory blocks
from .memory import MemBlock, RomBlock

# conditional updates
from .conditional import conditional_assignment, otherwise, currently_under_condition

# block simulation support
from .simulation import Simulation, FastSimulation, SimulationTrace, enum_name
from .compilesim import CompiledSimulation

# block visualization output formats
from .visualization import (
    output_to_trivialgraph,
    graphviz_detailed_namer,
    output_to_graphviz,
    output_to_svg,
    block_to_graphviz_string,
    block_to_svg,
    trace_to_html,
    net_graph,
)

# import from and export to file format routines
from .importexport import (
    input_from_verilog,
    output_to_verilog,
    output_to_systemverilog,   # AHA fork: SystemVerilog (logic + always_ff) emission
    output_verilog_testbench,
    input_from_blif,
    output_to_firrtl,
    input_from_iscas_bench,
)

# different transform passes
from .passes import (
    common_subexp_elimination,
    constant_propagation,
    synthesize,
    nand_synth,
    and_inverter_synth,
    optimize,
    one_bit_selects,
    two_way_concat,
    direct_connect_outputs,
    two_way_fanout,
)

from .transform import (
    net_transform,
    wire_transform,
    copy_block,
    clone_wire,
    replace_wires,
    replace_wire_fast,
)

# analysis and estimation functions
from .analysis import (
    area_estimation,
    TimingAnalysis,
    yosys_area_delay,
    paths,
    distance,
    fanout,
)

# AHA fork: activity-based dynamic power estimation (additive; not in upstream)
from .power import (
    PowerParams,
    PowerEstimate,
    NetPower,
    StreamingToggleTrace,
    count_toggles,
    estimate_dynamic_power,
    estimate_streaming_dynamic_power,
    simulate_and_estimate_power,
)

# AHA fork: typed aggregates (Bundle/Vec) + ready-valid Decoupled interface
from .bundle import (
    Bundle,
    Vec,
    Decoupled,
    DecoupledWires,
)

# AHA fork: util library on Decoupled (Queue / Arbiter)
from .util import queue, arbiter, round_robin_arbiter

# AHA fork: address-map interconnect generation (SoC crossbar)
from .interconnect import (
    AddressMap, MapViolation, address_map_violations, map_clean,
    build_decoder, route, crossbar, crossbar_selected,
    crossbar_full, crossbar_full_selected, crossbar_full_preselected,
)

# AHA fork: realistic multi-port / banked SRAM modeling (additive; not in upstream)
from .sram import (
    SramConfig,
    SramPPA,
    model_sram,
    bank_conflicts,
    served_accesses,
    banked_sram_equivalent,
)

__all__ = [
    # pyrtlexceptions
    "PyrtlError",
    "PyrtlInternalError",
    # core
    "LogicNet",
    "Block",
    "PostSynthBlock",
    "working_block",
    "reset_working_block",
    "set_working_block",
    "temp_working_block",
    "set_debug_mode",
    # wire
    "WireVector",
    "Input",
    "Output",
    "Const",
    "Register",
    # gate_graph
    "GateGraph",
    "Gate",
    # helperfuncs
    "input_list",
    "output_list",
    "register_list",
    "wirevector_list",
    "log2",
    "truncate",
    "match_bitpattern",
    "bitpattern_to_val",
    "chop",
    "val_to_signed_integer",
    "val_to_formatted_str",
    "formatted_str_to_val",
    "infer_val_and_bitwidth",
    "probe",
    "rtl_assert",
    "check_rtl_assertions",
    "find_loop",
    "find_and_print_loop",
    "wire_struct",
    "wire_matrix",
    "one_hot_to_binary",
    "binary_to_one_hot",
    # corecircuits
    "and_all_bits",
    "or_all_bits",
    "xor_all_bits",
    "rtl_any",
    "rtl_all",
    "mux",
    "select",
    "concat",
    "concat_list",
    "parity",
    "tree_reduce",
    "as_wires",
    "match_bitwidth",
    "enum_mux",
    "bitfield_update",
    "bitfield_update_set",
    "signed_add",
    "signed_sub",
    "signed_mult",
    "signed_lt",
    "signed_le",
    "signed_gt",
    "signed_ge",
    "shift_left_arithmetic",
    "shift_right_arithmetic",
    "shift_left_logical",
    "shift_right_logical",
    # memory
    "MemBlock",
    "RomBlock",
    # conditional
    "conditional_assignment",
    "otherwise",
    "currently_under_condition",
    # simulation
    "Simulation",
    "FastSimulation",
    "SimulationTrace",
    "enum_name",
    # compilesim
    "CompiledSimulation",
    # visualization
    "output_to_trivialgraph",
    "graphviz_detailed_namer",
    "output_to_graphviz",
    "output_to_svg",
    "block_to_graphviz_string",
    "block_to_svg",
    "trace_to_html",
    "net_graph",
    # importexport
    "input_from_verilog",
    "output_to_verilog",
    "output_to_systemverilog",
    "output_verilog_testbench",
    "input_from_blif",
    "output_to_firrtl",
    "input_from_iscas_bench",
    # passes
    "common_subexp_elimination",
    "constant_propagation",
    "synthesize",
    "nand_synth",
    "and_inverter_synth",
    "optimize",
    "one_bit_selects",
    "two_way_concat",
    "direct_connect_outputs",
    "two_way_fanout",
    # transform
    "net_transform",
    "wire_transform",
    "copy_block",
    "clone_wire",
    "replace_wires",
    "replace_wire_fast",
    # analysis
    "area_estimation",
    "TimingAnalysis",
    "yosys_area_delay",
    "paths",
    "distance",
    "fanout",
    # power (AHA fork)
    "PowerParams",
    "PowerEstimate",
    "NetPower",
    "StreamingToggleTrace",
    "count_toggles",
    "estimate_dynamic_power",
    "estimate_streaming_dynamic_power",
    "simulate_and_estimate_power",
    # typed aggregates + decoupled (AHA fork)
    "Bundle",
    "Vec",
    "Decoupled",
    "DecoupledWires",
    "queue",
    "arbiter",
    "round_robin_arbiter",
    "AddressMap",
    "MapViolation",
    "address_map_violations",
    "map_clean",
    "build_decoder",
    "route",
    "crossbar",
    "crossbar_selected",
    "crossbar_full",
    "crossbar_full_selected",
    "crossbar_full_preselected",
    # sram (AHA fork)
    "SramConfig",
    "SramPPA",
    "model_sram",
    "bank_conflicts",
    "served_accesses",
    "banked_sram_equivalent",
]
