from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from surface.mesh.incidence import canonical_edge


def validate_coverage(mesh, values):
    expected = (set(mesh.nodes), set(mesh.edges), set(mesh.faces))
    actual = (set(values.node_scalars), set(values.edge_integrals),
              set(values.face_totals))
    if expected != actual:
        raise ValueError("field identities must exactly cover the mesh")
    for mapping in (values.node_scalars, values.edge_integrals, values.face_totals):
        if any(not isinstance(value, Fraction) for value in mapping.values()):
            raise ValueError("field values must be Fractions")


@dataclass(frozen=True)
class FieldValues:
    node_scalars: object
    edge_integrals: object
    face_totals: object

    def __post_init__(self):
        for name in ("node_scalars", "edge_integrals", "face_totals"):
            object.__setattr__(self, name, MappingProxyType(
                dict(sorted(getattr(self, name).items()))))

    def to_record(self):
        return {name: dict(getattr(self, name))
                for name in ("node_scalars", "edge_integrals", "face_totals")}


@dataclass(frozen=True)
class FieldBundle:
    revision_id: int
    mesh: object
    values: FieldValues

    @property
    def stitched(self):
        return hasattr(self.mesh, "edge_endpoints")

    def edge_value(self, start, end, *, edge_id=None):
        if not self.stitched:
            if edge_id is not None:
                raise ValueError("ordinary surface has no independent edge identity")
            key = canonical_edge(start, end)
            value = self.values.edge_integrals[key]
            return value if (start, end) == key else -value
        return self._stitched_edge_value(start, end, edge_id)

    def _stitched_edge_value(self, start, end, edge_id):
        endpoints = self.mesh.edge_endpoints
        if type(start) is not str or type(end) is not str:
            raise ValueError("edge endpoints must be node IDs")
        if start not in self.mesh.nodes or end not in self.mesh.nodes:
            raise KeyError(start if start not in self.mesh.nodes else end)
        if start == end:
            raise ValueError("edge endpoints must differ")
        if edge_id is None:
            canonical = canonical_edge(start, end)
            candidates = [edge for edge, pair in sorted(endpoints.items())
                          if pair == canonical]
            if not candidates:
                raise KeyError(canonical)
            if len(candidates) > 1:
                raise ValueError("parallel edges require an explicit edge_id")
            edge_id = candidates[0]
        elif type(edge_id) is not tuple or edge_id not in endpoints:
            raise KeyError(edge_id)
        pair = endpoints[edge_id]
        if set(pair) != {start, end}:
            raise ValueError("edge_id endpoints do not match the requested pair")
        value = self.values.edge_integrals[edge_id]
        return value if (start, end) == pair else -value


def bind_fields(revision_id, mesh, values):
    if type(revision_id) is not int or revision_id < 1:
        raise ValueError("invalid revision ID")
    validate_coverage(mesh, values)
    return FieldBundle(revision_id, mesh, values)
