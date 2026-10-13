# AHA vendored fork of PyRTL

This is a **minimal fork of PyRTL 0.12** (see `UPSTREAM_METADATA.txt` for the upstream
package metadata + license — PyRTL is BSD-3-Clause).  The source under `pyrtl/` is a
verbatim copy of upstream **except** for one surgical change:

## The only change: thread-local global state

Upstream PyRTL keeps the "current circuit" and the conditional-assignment context in
**process-global** state, so two threads building / simulating circuits at once corrupt
each other.  This fork makes that state **thread-local**, so concurrent measurement /
synthesis is safe (each thread gets its own working block + conditional state).  The
main thread is seeded from the import-time singleton, so **sequential single-thread
behaviour is byte-for-byte unchanged**.

Every change is marked with an `AHA fork:` comment. The thread-safety diff is confined to:

* `pyrtl/core.py` — the working block (`_singleton_block`) is now read/written through a
  thread-local (`_aha_get_block` / `_aha_set_block`); `working_block`,
  `reset_working_block`, and `set_working_block` use it.
* `pyrtl/conditional.py` — the conditional-assignment state (`_depth`,
  `_conditions_list_stack`, `_predicate_map`, `_conflicts_map`) moved into a thread-local
  `_AhaCondState` accessed via `_st()`.

### Consequence: thread-safe simulation

With the thread-local working block, both simulators are safe to run concurrently:
`FastSimulation` is pure-Python over a thread-local block, and `CompiledSimulation`
additionally gives each instance its own `tempfile.mkdtemp()` + `gcc` + `.so` (no
process-global state). So the fast C-backed simulator can drive parallel measurement /
DSE (verified: concurrent `CompiledSimulation` == sequential). A compile cache to skip
`gcc` for identical netlists is a deferred follow-on — it needs a deterministic C codegen,
which needs a stable `Block.__iter__` (a core change tracked separately).

## Second change: modular (hierarchical) Verilog emission

Upstream emits the whole block as one flat `module toplevel`. The fork adds modular output:

* `pyrtl/importexport.py` — `output_to_verilog` / `_VerilogOutput` take a `module_name`
  argument (default `"toplevel"`, so existing flat emit is unchanged).
* `pyrtl/modular.py` — a NEW additive module (`emit_module` / `emit_hierarchy` /
  `TopModule` / `Instance`): emit each sub-circuit Block as its own named module (one file
  each) plus a structural top that instantiates them. PyRTL has no instance primitive, so
  the top's structure is described by the caller.
* `pyrtl/modular.py` `InstanceArray` (+ `TopModule.array_wires` / `instance_arrays`) —
  synthesis-quality emission of REPLICATED instances as a Verilog `generate`-`for` loop
  rather than N unrolled instantiations (the form for a regular R×C PE mesh / N-lane
  datapath): **O(1) emitted text for N instances**. Connections are raw net expressions in
  terms of the loop `genvar` (e.g. `xin[i*8 +: 8]` for a packed bus, or `row[i]` for an
  `array_wires` unpacked array), keeping the caller-describes-the-structure contract; `clk`
  is auto-wired unless overridden. Proven: yosys-elaborates (`generate`/part-select are
  Verilog-2001), and the top stays one instantiation line at N=4 and N=64.

## Third change: build-time module hierarchy

* `pyrtl/hierarchy.py` — a NEW additive module (`module` / `Module` / `Design` / `Net`):
  elaborate a leaf sub-circuit ONCE and `instantiate` it N times as black-boxes wired
  through named nets, instead of inlining it N times (upstream's only option). The
  persistent design stays small (one leaf block + N light instance records) — O(1)
  leaf-elaboration, modular emit for free (`Design.emit_verilog` reuses `modular.py`), and
  `Design.flatten()` inlines for correct simulation (proven byte-equal to a flat build).
  Single clock domain; memories inside a leaf are a follow-on (raise a clear error).

## Fourth change: deterministic, per-block name indexers

Upstream numbers auto-generated temp/const/mem names from PROCESS-GLOBAL counters, so a
design built after another gets shifted names, and parallel builds interleave
non-deterministically — both break diffable, reproducible (sign-off-grade) RTL.

* `pyrtl/core.py` — `_block_name_indexer(block, attr, prefix)` lazily stashes a
  `_NameIndexer` on the (thread-local) working block.
* `pyrtl/wire.py` — `next_tempvar_name` and `Const` naming use the per-block indexer.
* `pyrtl/memory.py` — MemBlock ids use the per-block indexer.

So each design numbers from 0 → the same design always emits the same names, regardless
of build order or parallelism (proven: identical RTL across 24 concurrent builds and
across builds with unrelated designs in between). The old global indexers remain but are
unused.

## Fifth change: deterministic topographic iteration (`Block.__iter__`)

Deterministic *names* (the fourth change) are only half of reproducible RTL — the *order*
nets are emitted in is the other half. Upstream's `Block.__iter__` walks the netlist by
popping from a `set` of wires and iterating `set`-built adjacency lists, and both
`WireVector.__hash__` and `LogicNet.__hash__` are **id-derived** (`id(self)` /
`hash(tuple(self))`). So the topographic order is id-hash order: stable only by coincidence
within a single run, and it shifts under different build order, interleaved/parallel
construction, or any allocation that moves object ids — reordering the emitted Verilog and
the compiled-simulation C source.

* `pyrtl/core.py` — `Block.__iter__` is now a deterministic Kahn's topological sort. Among
  the nets that are simultaneously ready (all args already produced) it emits the one with
  the smallest **canonical key** (`_net_sort_key`: the net's op, op_param, and the *names*
  of its dests/args — no object ids). Register outputs are pre-cleared exactly as upstream
  (breaking their own cycle), and the loop-detection behaviour is unchanged (any net never
  reaching in-degree 0 → the same `find_and_print_loop` error). The **set of nets yielded
  and the topological validity are identical**; only the order is now canonical.

Result: the same design iterates — and therefore emits — the same way regardless of run,
build order, or thread (proven: byte-identical Verilog under id perturbation and across 24
concurrent builds; the net *sequence* itself, keyed by name, is invariant). This is the
enabler for the CompiledSimulation **compile cache** (the sixth change): with deterministic
netlist iteration the generated C source is reproducible, so a content-addressed cache can
skip `gcc` for an unchanged netlist.

## Sixth change: a content-addressed CompiledSimulation compile cache

Building a `CompiledSimulation` generates a C file and compiles it with `gcc` into a `.so`
— the dominant cost, paid every time even for a netlist compiled moments ago. Because the
C codegen is now deterministic, an identical netlist yields byte-identical C, so the `.so`
can be content-addressed and the compile skipped on a hit.

* `pyrtl/compilesim.py` —
  * `_create_code` now emits **deterministic** C: every set-iteration that fed the codegen
    (wire declarations — which drive the `_uid()`-derived C variable names — plus memories,
    inputs, outputs, and the register/memory-write nets) is sorted by a stable key (wire
    name, or `_net_sort_key` for nets). This is required not just for cache *hits* but for
    their *correctness*: on a hit the instance recomputes its input/output marshalling maps,
    which must match the cached `.so`'s actual buffer layout.
  * `_create_dll` content-addresses the C source together with a **build signature** (cache
    format, platform, machine, CPU instruction-set fingerprint — because `-march=native`
    tunes to the build CPU — `gcc` version, and the exact flags), and reuses a cached `.so`
    when one exists, skipping `gcc`. Misses compile and then atomically install (`os.replace`)
    a copy into the cache for others; racing builders write byte-identical files, so
    last-writer-wins is safe.
  * **Isolation preserved:** every instance still `dlopen`s its OWN unique-inode copy of the
    `.so` (on a hit we *copy* the cached file into the instance's temp dir, then load that).
    The generated C keeps register/memory/wire state in file-scope globals, and `dlopen`
    keys on (device, inode), so loading the shared cache file *directly* would hand
    co-existing instances the same globals and corrupt them. The cache replaces `gcc`, not
    the per-instance isolation.

On by default; `PYRTL_COMPILESIM_NO_CACHE=1` disables it and `PYRTL_COMPILESIM_CACHE_DIR`
relocates it (default `<XDG cache>/pyrtl/compilesim`). A load failure or any cache I/O error
falls back to a fresh compile. Proven: an identical design skips `gcc` on rebuild in the
same process AND cross-process (different object ids → same C → same key → hit); a different
design never false-hits; 24 threads building one design concurrently all produce correct,
isolated results. The cache is unbounded (content-addressed; grows with distinct designs) —
LRU eviction is a possible follow-on.

## Seventh change: activity-based dynamic power estimation

Upstream ships an area estimator (`analysis.area_estimation`) and a timing estimator
(`analysis.TimingAnalysis`) but **no power model** — the missing PPA axis.

* `pyrtl/power.py` — a NEW additive module (clean upstream-merge): `estimate_dynamic_power`
  / `simulate_and_estimate_power` count the bit transitions each net makes over a simulation
  trace and weight them by a first-order capacitance model (per-bit wire load + per-fanout
  gate load) plus a clock-tree term from the register count. Exposed from `pyrtl/__init__.py`
  alongside the other estimators (`PowerParams`, `PowerEstimate`, `NetPower`, `count_toggles`).

The point is that this is **workload-aware**: the activity comes from a real simulation, so
different stimulus produces different power — the signal an architecture-exploration loop
wants and that a static gate-count estimate cannot give. It is deliberately **honest** about
its limits: the switching *activity* (toggle counts) is exact for the supplied stimulus, but
the capacitance/voltage/frequency constants are first-order models, so **absolute watts are
an estimate and relative comparisons are the trustworthy output**. Glitch power (intra-cycle
hazards) and memory-array energy are out of scope. Proven: a high-activity workload draws
multiples of the data-switching power of constant inputs on the same datapath; clock power is
workload-independent and scales with register count; power is linear in frequency and
quadratic in voltage; the toggle count is exact; results are deterministic.

## Eighth change: realistic multi-port / banked SRAM modeling

Upstream `MemBlock` allows multiple read/write ports but models them as IDEAL — no area or
timing cost per port, and no bank conflicts. Real SRAM PPA is dominated by port count and
organization.

* `pyrtl/sram.py` — a NEW additive module (exposed from `pyrtl/__init__.py`):
  * `model_sram(SramConfig)` — area / access-energy / access-time / peak-bandwidth for a
    monolithic multiport array OR a banked single-port array. Area reuses the published SRAM
    scaling of `analysis.area_estimation` (`bits**0.9 · ports**0.7 · tech**2.07`); it captures
    the real tradeoff — a true N-port array is expensive (`ports**0.7`, conflict-free), while
    `N` single-port banks cost far less for the same peak bandwidth, run faster and at lower
    energy (fewer rows per bank), but are bank-conflict-limited.
  * `bank_conflicts` / `served_accesses` — EXACT bank-conflict counts for an access pattern
    (low-order interleave, configurable ports-per-bank).
  * `banked_sram_equivalent` — builds a real low-order-interleaved bank array (decode + write
    routing + read mux) and proves by cosim that it produces the SAME read stream as a flat
    `MemBlock` — the banking is real RTL, not just a cost model.

Honest: the area scaling is the published model and the per-bank/port multipliers + access
energy/time constants are first-order (relative comparisons trustworthy, absolute values are
estimates); the conflict counts and the functional equivalence are EXACT. Proven: area is
monotone in bits/ports, a true 2r2w port costs more than 1r1w, 4 banks cost ~69% of a true
4-port at scale (banking win) and access ~2× faster, and the banked RTL == a flat memory.

## Ninth change: hard-IP / macro (black-box) instantiation

PyRTL is a flat-netlist generator, and even the fork's structural `Instance` requires a PyRTL
`Block` behind every instance — so there was NO way to instantiate a cell whose implementation
we don't own: foundry IP (SRAM compilers, PLLs, PHYs, IO cells), FPGA vendor primitives
(Xilinx RAMB36E2 / DSP48E2 / BUFG, Intel altsyncram), or a pre-hardened AHA eFPGA region.

* `pyrtl/hardip.py` — a NEW additive module. `HardMacro` declares a black-box cell by its
  INTERFACE only (module name, parameters, typed `MacroPort`s) — not an implementation. From
  that, the fork emits a correct, optionally-parameterised Verilog instantiation
  (`MODULE #(.P(v), ...) inst (.port(net), ...)`; ints bare, modes quoted, sized literals raw)
  plus a `(* blackbox *)` stub so the design elaborates self-contained (a real flow reads the
  vendor library instead). A starter library ships interfaces for `foundry_sram_1rw`,
  `xilinx_ramb36e2`, `xilinx_dsp48e2`, `xilinx_bufg`, and `aha_efpga_region`.
* `pyrtl/modular.py` — `TopModule` gains `macros` (individual `MacroInstance`s) and
  `macro_arrays` (replicated macros emitted as a `generate`-for loop, reusing the eighth
  change); `emit_hierarchy` writes a `<top>__blackboxes.v` with one stub per distinct macro.

Proven: parameterised instantiations + stubs are emitted, a top instantiating a foundry SRAM
macro yosys-ELABORATES (and the stub is what carries the port directions — without it yosys
reports the top output undriven).

A `HardMacro` may also carry a `sim_model` — a PyRTL builder `fn(inputs) -> outputs` giving the
macro a BEHAVIOUR — so the same declaration is both simulatable AND synthesizable:

* `instantiate_model(macro, inputs)` splices the model's logic into the current working block
  (clock pins, marked `is_clock`, are implicit — PyRTL forbids `Input("clk")`), so a design
  containing the macro becomes SIMULATABLE; the black-box instantiation is emitted separately
  for synthesis (the model is never emitted).
* `simulate_macro` / `build_model_block` run the model standalone (works for stateful macros
  like memories — no flatten); `macro_model_equivalent` cosim-checks model == a Python spec.
* The library `foundry_sram_1rw` (MemBlock-backed, async-read/sync-write) and `xilinx_dsp48e2`
  (functional P=A*B+C) ship models; `aha_efpga_region` stays opaque (no model) and clearly
  errors on simulate.

Proven: the SRAM model matches a reference memory and the DSP model computes A*B+C (cosim); a
design splicing the SRAM model reads back what it wrote; emission stays the black box (no model
internals leak). HONEST: a model is a behavioural (not necessarily cycle-accurate) stand-in —
e.g. the DSP model omits the AREG/BREG pipeline; and without a model a macro is emit-only
(opaque to simulation). DEFER: `hierarchy.Design.instantiate_macro` (gated by the existing
flatten-memory limitation for memory macros); liberty/.lib timing import.

## Tenth change: multi-clock SoC composition + clock-domain-crossing (CDC) checking

PyRTL has one implicit clock, but that is a PER-MODULE property: each module is single-clock
internally, and a real multi-clock SoC is built by wiring DIFFERENT clocks to different instances
at the top. This adds that composition layer without touching the core.

* `pyrtl/multiclock.py` — a NEW additive module. A `MultiClockSoC` declares clock domains, nodes
  assigned to domains, and CDC crossings. `cdc_violations` is the structural CDC checker (the #1
  SoC bug class): it flags every signal that crosses a clock-domain boundary without a registered
  synchronizer, and every multi-bit signal taken through a plain two-flop synchronizer (a
  bit-coherency hazard — use an async FIFO / gray code). `emit_soc` produces a multi-clock top
  (one clk input per domain, each node wired to its domain's clock), a real two-flop synchronizer
  module, a REAL gray-code async-FIFO module for multi-bit crossings (`async_fifo_verilog`; no
  longer a black box), and black-box stubs for the nodes and for handshake crossings.
* The async FIFO's CDC safety rests on a GRAY-CODE pointer (consecutive values differ in exactly
  one bit, so a pointer synchronized through a two-flop synchronizer is always the old or new value,
  never a corrupted intermediate). That encode/decode is combinational single-clock logic, so it is
  built (`gray_encode`/`gray_decode`) and formally PROVEN: `gray_decode(gray_encode(x)) == x` for
  all x, and adjacent values' gray codes are Hamming-distance-1 (the property the synchronizer
  relies on). The two-clock FIFO assembly (registered full/empty to avoid a combinational loop;
  flow control winc/wfull/rinc/rempty exposed at the SoC boundary) is emitted as templated RTL —
  PyRTL has one clock per block, so a dual-clock module cannot be simulated in a single PyRTL block.

The node internals are out of scope here — this layer is about composition / clocking / CDC, the
ability to assemble and CDC-check large multi-clock SoCs, not the leaf logic. Proven: a properly
synchronized 2-domain SoC has no violations; an unsynchronized cross-domain read and a multi-bit
two-flop crossing are both flagged; a multi-bit async-FIFO crossing is clean; the async-FIFO
crossing emits the real gray-code FIFO (not a stub); the gray encode/decode are proven inverse +
Hamming-1; and the emitted multi-clock RTL (two-flop and real async-FIFO variants, plus the FIFO
standalone) yosys-elaborates with `check -assert` (no combinational loops or undriven nets).

## Eleventh change: SystemVerilog emission (with Verilog-2001 as the lowered form)

Upstream emits Verilog-2001 (`wire`/`reg` + plain `always`). The fork adds a SystemVerilog
dialect — modern, intent-carrying, what commercial flows expect — while keeping Verilog-2001 as
the exact lowered form (SV and our Verilog differ only in keywords; combinational logic is
`assign` in both).

* `pyrtl/importexport.py` — `output_to_verilog(..., systemverilog=False)` and the convenience
  `output_to_systemverilog`: in SV mode every signal is declared `logic` (vs `wire`/`reg`) and
  sequential logic uses `always_ff @(posedge clk[ or posedge rst])` (vs plain `always`), which
  lets synthesis tools check flop-vs-comb intent. Verilog-2001 (the default) is unchanged.
* `pyrtl/modular.py` — `emit_module` / `emit_top` / `emit_hierarchy` take `systemverilog=` so the
  modular / hierarchical / generate-loop paths emit consistent SV (structural-top wires become
  `logic` too).

Proven: the Verilog default is byte-unchanged (`reg`/`wire`/`always`); SV mode emits
`logic`/`always_ff` (incl. async-reset and memory arrays) and no bare `reg`/`wire`/`always`; both
dialects yosys-elaborate (SV via `read_verilog -sv`). DEFER: SV in the structural `multiclock` /
`hardip` stubs (currently `wire`, which is still valid SV); packed structs / enums / interfaces
(tie to the typed-Bundle gap).

## Twelfth change: typed aggregates (Bundle / Vec) + a ready-valid Decoupled interface

PyRTL is bit-vector only, so structured signals are hand-packed with concat/slice — error-prone
at SoC scale and the biggest ergonomic gap vs Chisel. This adds a typed layer that LOWERS to
exactly that concat/slice, no core change.

* `pyrtl/bundle.py` — a NEW additive module (exposed from `pyrtl/__init__`):
  * `Bundle` — a named struct of fields (each a width or a nested Bundle/Vec). `pack` concats the
    fields into one WireVector (field[0] = MSB); `unpack` slices a wire back. They are exact
    inverses, so a Bundle is a typed VIEW over a bus — for typed module ports and memory words.
  * `Vec` — an array of N identical elements (a width or a Bundle); same pack/unpack.
  * `Decoupled` — the ready-valid handshake (the inter-block flow-control interface): `bits` +
    `valid` flow forward, `ready` flows back. `connect` wires producer→consumer in the right
    directions; `fire` is `valid & ready`; the payload may itself be a Bundle.

These are the Chisel `Bundle` / `Vec` / `DecoupledIO` concepts lowered onto PyRTL. Proven:
pack/unpack round-trip (incl. nested Bundles and Vec-of-Bundles) via simulation; the handshake
fires only on valid&ready with bits forward / ready back; a Bundle payload round-trips through a
Decoupled; and a Bundle-based typed transform on a module port yosys-elaborates (as SystemVerilog).
DEFER: a util library built on Decoupled (Queue/Arbiter/FIFO); SV packed-struct emission of a
Bundle (currently lowers to a flat bus).

## Thirteenth change: a util library on the Decoupled interface (Queue / Arbiter)

The flow-controlled building blocks everyone reaches for (Chisel's `chisel3.util`), built as real
PyRTL logic over the ready-valid `Decoupled` (twelfth change):

* `pyrtl/util.py` — exposed from `pyrtl/__init__`:
  * `queue(enq, depth)` — a synchronous FIFO (circular buffer over a MemBlock + head/tail/count):
    an enqueue channel in, a dequeue channel out, with backpressure (`enq.ready` = not full,
    `deq.valid` = not empty). The workhorse for rate-matching / pipelining / the data side of a
    clock-domain crossing.
  * `arbiter(inputs)` — a fixed-priority N->1 merge: the lowest-index valid input wins, its payload
    is routed out, and only the granted input sees `ready` (gated by the downstream `ready`).
  * `round_robin_arbiter(inputs)` — a FAIR N->1 merge: after granting input g, priority rotates
    past g (a thermometer-mask register), so no input is starved under sustained contention.

Proven: FIFO ordering + full/empty backpressure across a push/pop sequence (incl. depth-1); the
fixed arbiter grants by priority and respects downstream `ready`; the round-robin arbiter rotates
fairly under contention, skips non-valid inputs, and holds its pointer when the downstream is not
ready; and a two-queue→arbiter slice (a small flow-controlled SoC) yosys-elaborates as
SystemVerilog. (The bidirectional crossbar with response-path routing and the gray-code async-FIFO
for the multi-clock CDC crossing now both exist — see below / the multi-clock change.)

## Fourteenth change: address-map interconnect generation (SoC crossbar)

Diplomacy's valuable essence is: declare each slave's address range, and auto-build the decode +
crossbar (no hand-written address decoding, no manual master/slave wiring). This does that on the
fork's own ready-valid `Decoupled` bus (no TileLink protocol copied), and adds what diplomacy
doesn't — the generated decoder is FORMALLY verifiable.

* `pyrtl/interconnect.py` — exposed from `pyrtl/__init__`:
  * `AddressMap` + `address_map_violations` — declare slaves as (name, base, size); flag the #1
    interconnect bug (overlapping ranges), zero-size, and out-of-range regions.
  * `build_decoder(addr, amap)` — a combinational one-hot address decoder.
  * `route(master, addr, amap)` — a 1->M demux (master request fans out to the addressed slave).
  * `crossbar(masters, addrs, amap)` — an N->M request crossbar, composed from `route` (demux) +
    `arbiter` (per-slave merge).
  * `crossbar_full(masters, addrs, slave_resps, amap, resp_width=)` — a full BIDIRECTIONAL N<->M
    crossbar. The request is tagged with the master's (static) source id; the slave echoes that id
    in its response; the RESPONSE path is then just another `crossbar` routed by id, so responses
    return to the originating master with no source FIFOs and no in-order constraint (out-of-order
    and multi-outstanding safe). Pure composition — `crossbar` used twice (by address, then by id).

Proven: the address-map check catches overlap / out-of-range; the decoder is one-hot + correct in
simulation; **the generated decoder is proven one-hot AND correct for EVERY address** via the
netlist->Z3 layer (the AHA test) — generation *and* proof, the improvement over plain diplomacy;
route/crossbar route by address with arbitration; the bidirectional crossbar routes each slave's
response back to the originating master by id (distinct slaves answer distinct masters; under
contention the winner's response returns and the loser stalls); and both a 2x3 request crossbar and
a 2x2 bidirectional crossbar yosys-elaborate as SystemVerilog. DEFER: round-robin per-slave
arbitration in the crossbar (the fair `round_robin_arbiter` exists; wiring it in as the default);
burst/length negotiation.

## How it is activated

`third_party/pyrtl` is placed ahead of any pip-installed `pyrtl` on `sys.path` by
`_pyrtl_fork.py` (called from the repo `conftest.py` for tests and `aha/__init__.py` for
the app).  No code changes are needed at any `import pyrtl` site.

## Not yet thread-safe

The z3-based proof path (`prove_mesh_routing`, used by `certify_gene`) uses z3's global
default context, which is a **separate** thread-safety concern — concurrent *proving*
still needs per-thread z3 contexts. Concurrent *measurement* (no z3) is safe with this
fork.

## Upgrading upstream PyRTL

Re-copy the upstream package over `pyrtl/`, then re-apply the in-place `AHA fork:` diffs —
grep for `AHA fork`; they live in `core.py` (thread-local working block, per-block name
indexers, deterministic `Block.__iter__`), `conditional.py` (thread-local conditional
state), and `wire.py` / `memory.py` (per-block indexers). The additive modules
(`modular.py`, `hierarchy.py`) and the `module_name` parameter in `importexport.py` are
new files / a backward-compatible signature and carry over as-is. The changes are small and
self-contained by design.
