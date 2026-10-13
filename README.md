# AHA PyRTL

The AHAComputing fork of [PyRTL](https://github.com/UCSBarchlab/PyRTL), based on
upstream 0.12. It provides thread-local construction, deterministic naming and
RTL emission, reusable module hierarchy, typed ready/valid interfaces,
interconnects, CDC/reset composition, SRAM modeling, activity power estimates,
and declared hard-IP interfaces.

Install with `python -m pip install .`; the Python module remains `pyrtl`.
Run the standalone regression suite with `python -m pytest tests`.

AHA product generators, physical collateral and admission evidence live in
their own repositories. The first snapshot pins committed source; pending
changes in the original checkout are outside this release.

See `NOTICE.md`, `UPSTREAM_METADATA.txt` and `LICENSE.md` for upstream provenance
and the BSD-3-Clause terms. `AHA.source.json` binds the selected source revision
and copied toolkit regression tests. A toolkit test pass does not certify any IP.
