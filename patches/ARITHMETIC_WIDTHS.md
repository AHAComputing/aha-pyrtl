# Arithmetic emission repair — 2026-09-05

Status: **integrated into the default Verilog and SystemVerilog emitter**.

`pyrtl/importexport.py` now adds a sized intermediate wire for each arithmetic gate
(`+`, `-`, `*`). Output assignments already have their declared width. This retains
carry, borrow and full-product widths before Verilog self-sizes concatenation
operands or propagates an enclosing expression's width. It changes emission,
not the PyRTL netlist, and requires no SystemVerilog-only casts.

The user approved preserving and committing the existing memory-ordering changes
(sorting by stable memory name instead of allocation id) alongside this fix. Those
changes are retained, with an allocation-order independence regression. The earlier
`arithmetic_widths.patch` is archived candidate evidence, not a patch to apply again.

## Evidence

```sh
pytest -q -s -o addopts='' tests/pyrtl_emission/test_arithmetic_widths.py
```

Verilator 5.030: **12 tests passed; no expected failures**. Ten cases check default
emission in both Verilog and SystemVerilog. A negative control locally restores
the old arithmetic inlining predicate and checks that packed-word corruption
returns. One elaboration-only test checks memory allocation-order independence.

Independent integer oracles check tagged addition, subtraction modulo the declared
width, full multiplication, nested arithmetic, extended word packing and registered
results. Inputs are exhaustive at widths 1 and 4, and deterministic boundary/random
vectors at widths 9, 33 and 65: 719 vectors per dialect, six output values per vector. No PyRTL
simulator is used. This is bounded functional evidence, not formal equivalence or
physical sign-off.

Default Verilog compiled artifacts, identical to the earlier candidate, by width:

- 1: `64e050e509e63401fcbed109daa1d915bd3f35c88e53e9c1f11f718729178c7d`
- 4: `10e1a90fac736e7f13d57cbad4acccf62aeea18c21927fa87aeac80a16471c7f`
- 9: `6b0607b393f364fcff7c2f9a7c03d2636ccdc91c9f00209301c0bd53edd2a0fd`
- 33: `778a762ecdf720a034fd8ce547e7d8e88949bacd0c2ff0eca399ee9c1e9c7fb6`
- 65: `f568592d09250f4a06bd5d92eb8c8190fb92ede42510a93c44b0c415c5df2c3d`

Before integration, the candidate predicate, temporarily installed only in the test process,
also passed `test_local_pair_full_and_cross_tile_fault_reuse[small]`: all four
local 1/2/4-engine and cross-tile 16-engine recovery modes. Exact numeric results,
completion fences and cache reuse remain checked. This uses reduced widths and
two contexts but retains four tiles / sixteen engines; it is not a new production
configuration claim. The default emitter file remained untouched throughout.

## Canonical integration replay

The default-emitter integration is `18219fa8`. Seventeen adjacent modular,
dialect and reset emission/elaboration checks pass. Normal Verilator XNN recovery
passes for both reduced and production configurations (two tests, four gang modes
each), without overriding the emitter predicate. Together with the twelve focused
tests above, this is **31 tests passed, no expected failures**.

```sh
pytest -q -s -o addopts='' tests/xnn_manycore/test_hierarchical_context_recovery.py -k local_pair
```

The production artifact is
`63beb726c67c2f00ab068299f6b1147836240ee7d57931d5888d43c656c448eb`;
the reduced-parameter artifact is
`4b84754d5f6d918577aef70ab27da1320e9177ed818b60865d7c1986d3d545a0`.
Exact INT8 results and HBM reuse remain correct. Production failure/readmission
cycles remain 35/36 for local 1/2/4-engine gangs and 67/68 for the 16-engine
cross-tile gang. These are recovery-cycle checks, not sustained throughput claims.

The original 64-bit packed-word reproducer also passes all sixteen input values;
base 3 now returns `0x400000003`. Its repaired artifact is
`81dc57b2c50f8d38782a6e8beaa994a35d61b021bd8850d1b7639d5844debe93`.

Changed RTL hashes invalidate prior compiled-model identities; old dossiers must
not silently substitute newly emitted RTL. Broader emitter correctness and formal
composition remain separate obligations.
