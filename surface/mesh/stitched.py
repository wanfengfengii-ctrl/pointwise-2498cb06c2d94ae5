"""Saved stitched (quotient) surface records.

This module only parses and cross-checks information that was saved by the
one-shot build entry point.  It never reruns a union-find, never decides an
orientation and never transfers a field; those operations live in
``surface.mesh.stitching`` and are called only from
``SourceCatalog.build_stitched``.
"""
from dataclasses import dataclass
from types import MappingProxyType

from surface.fields.bindings import FieldValues
from surface.mesh.geometry import require_nondegenerate_triangle
from surface.mesh.model import FaceEdge, OrdinaryMesh
from surface.results.codec import canonical_digest

STITCH_KEYS = {"source_revision", "source_digest", "source_input", "seams",
               "node_map", "edge_map", "face_map"}
MESH_KEYS = {"nodes", "faces", "edges", "face_edges", "edge_endpoints"}
ORIENTATION_CORNERS = {1: (0, 1, 2), -1: (0, 2, 1)}


def is_pair(value):
    return type(value) is tuple and len(value) == 2


def validate_stitched_face_references(faces, nodes):
    if type(faces) is not dict or not 1 <= len(faces) <= 60:
        raise ValueError("one to sixty identified faces required")
    result = {}
    for face, vertices in faces.items():
        if type(face) is not str or not face:
            raise ValueError("nonempty string face ID required")
        if (type(vertices) is not tuple or len(vertices) != 3
                or any(type(v) is not str or v not in nodes for v in vertices)
                or len(set(vertices)) != 3):
            raise ValueError("invalid stitched triangle references")
        require_nondegenerate_triangle(*(nodes[v] for v in vertices))
        result[face] = tuple(vertices)
    return dict(sorted(result.items()))


def face_corners_from_stitch(record):
    """Read saved corners; validation against the mesh happens later."""
    if type(record) is not dict or set(record) != STITCH_KEYS:
        raise ValueError("invalid stitch record")
    face_map = record["face_map"]
    if type(face_map) is not dict or not face_map:
        raise ValueError("incomplete face map")
    corners = {}
    for face, entry in face_map.items():
        if type(face) is not str or not face:
            raise ValueError("invalid face identity in face map")
        if (type(entry) is not dict or set(entry) != {"sign", "corners"}
                or type(entry["sign"]) is not int
                or entry["sign"] not in ORIENTATION_CORNERS
                or type(entry["corners"]) is not tuple
                or entry["corners"] != ORIENTATION_CORNERS[entry["sign"]]):
            raise ValueError("invalid saved face orientation")
        corners[face] = entry["corners"]
    return corners


@dataclass(frozen=True)
class StitchedMesh:
    """A quotient mesh. ``face_corners[face][i]`` locates source corner ``i``."""

    nodes: object
    faces: object
    face_edges: object
    incidence: object
    edge_endpoints: object
    face_corners: object

    def __post_init__(self):
        for name in ("nodes", "faces", "face_edges", "incidence",
                     "edge_endpoints", "face_corners"):
            object.__setattr__(self, name, MappingProxyType(
                {key: tuple(value) for key, value in getattr(self, name).items()}))

    @property
    def edges(self):
        return tuple(sorted(self.edge_endpoints))

    def source_vertices(self, face):
        """Quotient vertices in the solver's original corner order."""
        vertices = self.faces[face]
        return tuple(vertices[position] for position in self.face_corners[face])

    def to_record(self):
        return {"nodes": dict(self.nodes), "faces": dict(self.faces),
                "edges": list(self.edges),
                "face_edges": {face: [{"edge": term.edge, "sign": term.sign}
                                     for term in self.face_edges[face]]
                               for face in sorted(self.faces)},
                "edge_endpoints": {edge: tuple(self.edge_endpoints[edge])
                                   for edge in self.edges}}

    @classmethod
    def from_record(cls, record, face_corners):
        """Parse a saved stitched mesh; corners come from the saved face map."""
        from surface.sources.mesh_input import validate_node_coordinates
        if type(record) is not dict or set(record) != MESH_KEYS:
            raise ValueError("invalid stitched mesh record")
        nodes = validate_node_coordinates(record["nodes"])
        faces = validate_stitched_face_references(record["faces"], nodes)
        if type(face_corners) is not dict or set(face_corners) != set(faces):
            raise ValueError("face corners must cover every saved face")
        corners = {}
        for face in sorted(faces):
            value = face_corners[face]
            if type(value) is not tuple or value not in ORIENTATION_CORNERS.values():
                raise ValueError("invalid saved face corners")
            corners[face] = value
        edges, endpoints = record["edges"], record["edge_endpoints"]
        if (type(edges) is not list or type(endpoints) is not dict
                or edges != sorted(endpoints) or len(set(edges)) != len(edges)):
            raise ValueError("inconsistent stitched edge directory")
        saved_endpoints = {}
        for edge, pair in endpoints.items():
            if (type(edge) is not tuple or len(edge) != 2
                    or any(type(v) is not str or not v for v in edge) or edge[0] >= edge[1]
                    or not is_pair(pair)
                    or any(type(v) is not str or v not in nodes for v in pair)
                    or pair[0] >= pair[1]):
                raise ValueError("invalid canonical stitched edge endpoints")
            saved_endpoints[edge] = tuple(pair)
        face_edges = record["face_edges"]
        if type(face_edges) is not dict or set(face_edges) != set(faces):
            raise ValueError("incomplete stitched face incidence")
        per_face, per_edge = {}, {}
        for face in sorted(faces):
            terms = face_edges[face]
            if type(terms) is not list or len(terms) != 3:
                raise ValueError("three stitched edge terms required")
            vertices = faces[face]
            for i, item in enumerate(terms):
                if (type(item) is not dict or set(item) != {"edge", "sign"}
                        or type(item["edge"]) is not tuple
                        or item["edge"] not in saved_endpoints
                        or type(item["sign"]) is not int or item["sign"] not in (1, -1)):
                    raise ValueError("invalid stitched edge term")
                a, b = vertices[i], vertices[(i + 1) % 3]
                if tuple(sorted((a, b))) != saved_endpoints[item["edge"]]:
                    raise ValueError("stitched term does not match saved endpoints")
                sign = 1 if (a, b) == saved_endpoints[item["edge"]] else -1
                if item["sign"] != sign:
                    raise ValueError("stitched incidence differs from face winding")
                per_edge.setdefault(item["edge"], []).append((face, sign))
                if len(per_edge[item["edge"]]) > 2:
                    raise ValueError("stitched edge has more than two incident faces")
            per_face[face] = tuple(FaceEdge(term["edge"], term["sign"]) for term in terms)
        if set(per_edge) != set(saved_endpoints):
            raise ValueError("orphan stitched edge")
        return cls(nodes, faces, per_face,
                   {edge: tuple(items) for edge, items in sorted(per_edge.items())},
                   saved_endpoints, corners)


@dataclass(frozen=True)
class StitchedStitch:
    source_revision: int
    source_digest: str
    source_input: object
    seams: tuple
    node_map: object
    edge_map: object
    face_map: object

    def __post_init__(self):
        for name in ("node_map", "edge_map", "face_map"):
            object.__setattr__(self, name, MappingProxyType(
                {key: (dict(value) if isinstance(value, dict) else value)
                 for key, value in getattr(self, name).items()}))

    def to_record(self):
        return {"source_revision": self.source_revision,
                "source_digest": self.source_digest,
                "source_input": {
                    "mesh": dict(self.source_input["mesh"]),
                    "fields": {name: dict(value)
                               for name, value in self.source_input["fields"].items()}},
                "seams": [tuple(tuple(side) for side in seam) for seam in self.seams],
                "node_map": dict(self.node_map),
                "edge_map": {edge: dict(entry) for edge, entry in self.edge_map.items()},
                "face_map": {face: {"sign": entry["sign"],
                                    "corners": tuple(entry["corners"])}
                             for face, entry in self.face_map.items()}}


def validate_stitch_record(record, mesh, fields):
    """Cross-check a saved stitch against its saved stitched mesh and fields.

    Every check reads saved associations only; no quotient, orientation or field
    transfer is recomputed.
    """
    if type(record) is not dict or set(record) != STITCH_KEYS:
        raise ValueError("invalid stitch record")
    revision = record["source_revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid stitch source revision")
    digest = record["source_digest"]
    source_input = record["source_input"]
    if (type(source_input) is not dict or set(source_input) != {"mesh", "fields"}
            or type(digest) is not str or canonical_digest(source_input) != digest):
        raise ValueError("stitch source digest mismatch")
    source = OrdinaryMesh.from_record(source_input["mesh"])
    from surface.sources.field_input import normalize_fields
    source_fields = normalize_fields(source, source_input["fields"])
    if not isinstance(fields, FieldValues):
        raise ValueError("normalized stitched fields required")

    seams = record["seams"]
    if type(seams) is not list:
        raise ValueError("stitch seams must be a list")
    normalized_seams = []
    for seam in seams:
        if (type(seam) is not tuple or len(seam) != 2
                or not is_pair(seam[0]) or not is_pair(seam[1])
                or any(type(v) is not str or v not in source.nodes
                       for v in seam[0] + seam[1])):
            raise ValueError("invalid seam pair")
        normalized_seams.append((tuple(seam[0]), tuple(seam[1])))

    node_map = record["node_map"]
    if type(node_map) is not dict or set(node_map) != set(source.nodes):
        raise ValueError("incomplete node map")
    if any(type(value) is not str or value not in mesh.nodes
           for value in node_map.values()):
        raise ValueError("invalid quotient node mapping")

    edge_map = record["edge_map"]
    if type(edge_map) is not dict or set(edge_map) != set(source.edges):
        raise ValueError("incomplete edge map")
    for edge, entry in edge_map.items():
        if type(entry) is not dict or set(entry) != {"edge", "sign"}:
            raise ValueError("invalid edge map entry")
        target, sign = entry["edge"], entry["sign"]
        if type(target) is not tuple or target not in mesh.edge_endpoints:
            raise ValueError("edge map names an unknown quotient edge")
        if type(sign) is not int or sign not in (1, -1):
            raise ValueError("invalid edge map sign")
        a, b = node_map[edge[0]], node_map[edge[1]]
        endpoints = tuple(sorted((a, b)))
        if mesh.edge_endpoints[target] != endpoints:
            raise ValueError("edge map endpoints disagree with node map")
        if sign != (1 if (a, b) == endpoints else -1):
            raise ValueError("edge map sign disagrees with node map")

    face_map = record["face_map"]
    if type(face_map) is not dict or set(face_map) != set(source.faces):
        raise ValueError("incomplete face map")
    for face, entry in face_map.items():
        if type(entry) is not dict or set(entry) != {"sign", "corners"}:
            raise ValueError("invalid face map entry")
        sign, corners = entry["sign"], entry["corners"]
        if type(sign) is not int or sign not in ORIENTATION_CORNERS:
            raise ValueError("invalid face orientation")
        if type(corners) is not tuple or corners != ORIENTATION_CORNERS[sign]:
            raise ValueError("face corners disagree with orientation")
        if mesh.face_corners[face] != corners:
            raise ValueError("saved face corners are inconsistent")
        source_vertices = source.faces[face]
        expected = tuple(node_map[source_vertices[i]] for i in
                         ((0, 1, 2) if sign > 0 else (0, 2, 1)))
        if mesh.faces[face] != expected:
            raise ValueError("face map disagrees with saved face winding")

    # Saved incidence must follow the saved edge map exactly, so a parallel
    # edge identity cannot be swapped after publication.  ``corners[p]`` is the
    # source corner shown at oriented position ``p``; its inverse sends an
    # oriented position back to its source corner.
    for face in source.faces:
        vertices = source.faces[face]
        corners = face_map[face]["corners"]
        inverse = {position: corner for corner, position in enumerate(corners)}
        expected_terms = []
        for position in range(3):
            a = vertices[inverse[position]]
            b = vertices[inverse[(position + 1) % 3]]
            original_edge = (a, b) if a < b else (b, a)
            entry = edge_map[original_edge]
            traversed = 1 if a < b else -1
            expected_terms.append(FaceEdge(entry["edge"],
                                           traversed * entry["sign"]))
        oriented = tuple(node_map[vertices[inverse[p]]] for p in range(3))
        if mesh.faces[face] != oriented:
            raise ValueError("face corners disagree with saved face winding")
        if mesh.face_edges[face] != tuple(expected_terms):
            raise ValueError("saved incidence disagrees with edge map")

    for node in source.nodes:
        if fields.node_scalars[node_map[node]] != source_fields.node_scalars[node]:
            raise ValueError("stitched node scalar differs from its source")
    for edge in source.edges:
        entry = edge_map[edge]
        expected = entry["sign"] * source_fields.edge_integrals[edge]
        if fields.edge_integrals[entry["edge"]] != expected:
            raise ValueError("stitched edge integral differs from its source")
    if fields.face_totals != source_fields.face_totals:
        raise ValueError("stitched face totals differ from their source")
    return StitchedStitch(revision, digest, source_input, tuple(normalized_seams),
                          node_map, edge_map, face_map)
