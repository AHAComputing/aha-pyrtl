"""AHA fork: typed aggregates (Bundle / Vec) and a ready-valid Decoupled interface.

PyRTL is bit-vector only, so structured signals are hand-packed with concat/slice — error-prone
at SoC scale. This adds a typed layer that LOWERS to exactly that:

  * ``Bundle`` — a named struct of fields (each a width or a nested Bundle/Vec). ``pack`` concats
    the fields into one WireVector (field[0] = MSB); ``unpack`` slices a wire back into fields.
    ``pack``/``unpack`` are exact inverses, so a Bundle is just a typed VIEW over a bus — ideal
    for typed module ports and memory words.
  * ``Vec`` — an array of N identical elements (a width or a Bundle); same pack/unpack.
  * ``Decoupled`` — the standard ready-valid handshake (the inter-block flow-control interface):
    ``bits`` + ``valid`` flow forward, ``ready`` flows back. ``connect`` wires a producer to a
    consumer in the right directions; ``fire`` is ``valid & ready``.

These are the Chisel ``Bundle`` / ``Vec`` / ``DecoupledIO`` concepts, lowered onto PyRTL. No core
change — everything is concat/slice + ``<<=`` over ordinary WireVectors.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple, Union

# a field's layout: a bit width, or a nested Bundle/Vec
Layout = Union[int, "Bundle", "Vec"]


def _width_of(layout: Layout) -> int:
    return layout if isinstance(layout, int) else layout.width


class Bundle:
    """A typed struct of named fields. ``fields`` is an ordered list of (name, layout) or a dict
    (insertion-ordered). field[0] occupies the most-significant bits of the packed wire."""

    def __init__(self, fields: Union[List[Tuple[str, Layout]], Dict[str, Layout]]):
        items = list(fields.items()) if isinstance(fields, dict) else list(fields)
        if not items:
            raise ValueError("a Bundle needs at least one field")
        self.fields: List[Tuple[str, Layout]] = items
        self._layout: Dict[str, Layout] = dict(items)

    @property
    def width(self) -> int:
        return sum(_width_of(lay) for _n, lay in self.fields)

    @property
    def field_names(self) -> Tuple[str, ...]:
        return tuple(n for n, _ in self.fields)

    def pack(self, values: Dict[str, Any]):
        """Concat the fields (in declared order, field[0] = MSB) into one WireVector. Each value
        is a WireVector, or a dict/list for a nested Bundle/Vec field."""
        import pyrtl

        missing = set(self._layout) - set(values)
        if missing:
            raise ValueError(f"pack missing fields {sorted(missing)}")
        parts = [_pack_value(self._layout[n], values[n]) for n, _ in self.fields]
        return parts[0] if len(parts) == 1 else pyrtl.concat(*parts)

    def unpack(self, wire) -> Dict[str, Any]:
        """Slice ``wire`` back into fields (the inverse of :meth:`pack`). Nested fields unpack
        recursively into dicts/lists."""
        out: Dict[str, Any] = {}
        hi = self.width
        for name, lay in self.fields:
            w = _width_of(lay)
            lo = hi - w
            sl = wire[lo:hi]
            out[name] = sl if isinstance(lay, int) else lay.unpack(sl)
            hi = lo
        return out

    def wires(self, prefix: str = ""):
        """Fresh named WireVectors for each (scalar) field, for building. Nested fields recurse."""
        import pyrtl

        out: Dict[str, Any] = {}
        for name, lay in self.fields:
            if isinstance(lay, int):
                out[name] = pyrtl.WireVector(lay, f"{prefix}{name}")
            else:
                out[name] = lay.wires(f"{prefix}{name}_")
        return out


class Vec:
    """An array of ``n`` identical elements (a width or a Bundle/Vec). element[0] = MSB."""

    def __init__(self, n: int, elem: Layout):
        if n < 1:
            raise ValueError("Vec needs n >= 1")
        self.n = n
        self.elem = elem

    @property
    def width(self) -> int:
        return self.n * _width_of(self.elem)

    def pack(self, items: List[Any]):
        import pyrtl

        if len(items) != self.n:
            raise ValueError(f"Vec.pack expects {self.n} items, got {len(items)}")
        parts = [_pack_value(self.elem, it) for it in items]
        return parts[0] if len(parts) == 1 else pyrtl.concat(*parts)

    def unpack(self, wire) -> List[Any]:
        w = _width_of(self.elem)
        out = []
        hi = self.width
        for _ in range(self.n):
            lo = hi - w
            sl = wire[lo:hi]
            out.append(sl if isinstance(self.elem, int) else self.elem.unpack(sl))
            hi = lo
        return out


def _pack_value(layout: Layout, value: Any):
    """Lower one field's value to a WireVector: a scalar value is used as-is; a nested
    Bundle/Vec value (dict/list) is packed (a WireVector value is accepted as already-packed)."""
    if isinstance(layout, int):
        return value
    if hasattr(value, "bitwidth"):     # already a WireVector for the nested field
        return value
    return layout.pack(value)


# ── Decoupled: the ready-valid handshake interface ───────────────────────────────────────


class DecoupledWires:
    """A handshake instance: ``bits`` (payload), ``valid`` (producer->consumer), ``ready``
    (consumer->producer)."""

    __slots__ = ("bits", "valid", "ready", "payload")

    def __init__(self, bits, valid, ready, payload):
        self.bits = bits
        self.valid = valid
        self.ready = ready
        self.payload = payload     # the payload Layout (width or Bundle), for unpacking bits


class Decoupled:
    """A ready-valid channel carrying ``payload`` (a width or a Bundle). ``bits``+``valid`` flow
    forward; ``ready`` flows back. The standard inter-block flow-control interface."""

    def __init__(self, payload: Layout):
        self.payload = payload

    @property
    def width(self) -> int:
        return _width_of(self.payload)

    def wires(self, prefix: str = "") -> DecoupledWires:
        import pyrtl

        return DecoupledWires(
            bits=pyrtl.WireVector(self.width, f"{prefix}bits"),
            valid=pyrtl.WireVector(1, f"{prefix}valid"),
            ready=pyrtl.WireVector(1, f"{prefix}ready"),
            payload=self.payload)

    @staticmethod
    def fire(chan: DecoupledWires):
        """The transfer condition: a beat moves iff valid AND ready."""
        return chan.valid & chan.ready

    @staticmethod
    def connect(producer: DecoupledWires, consumer: DecoupledWires) -> None:
        """Wire a producer channel to a consumer channel: bits/valid forward, ready back."""
        consumer.bits <<= producer.bits
        consumer.valid <<= producer.valid
        producer.ready <<= consumer.ready

    def unpack_bits(self, chan: DecoupledWires) -> Any:
        """Interpret a channel's ``bits`` as the payload (a WireVector, or unpacked Bundle/Vec)."""
        if isinstance(self.payload, int):
            return chan.bits
        return self.payload.unpack(chan.bits)


__all__ = ["Bundle", "Vec", "Decoupled", "DecoupledWires"]
