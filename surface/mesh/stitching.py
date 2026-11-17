"""One-shot construction of an explicitly stitched (quotient) source revision.

Called only by ``SourceCatalog.build_stitched``.  Reads, plan execution and
export never call this module: they consume the association records parsed by
:mod:`surface.mesh.stitched`.
"""
from surface.mesh.geometry import require_nondegenerate_triangle
from surface.mesh.incidence import canonical_edge
from surface.mesh.stitched import StitchedMesh, validate_stitch_record
from surface.sources.field_input import normalize_fields


class _UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left

    def quotient_map(self):
        groups = {}
        for item in self.parent:
            groups.setdefault(self.find(item), []).append(item)
        representative = {root: min(members) for root, members in groups.items()}
        return {item: representative[self.find(item)] for item in self.parent}


def _normalize_seams(mesh, seams):
    if type(seams) not in (list, tuple):
        raise ValueError("seams must be a list or tuple")
    result, used = [], set()
    for seam in seams:
        if type(seam) not in (list, tuple) or len(seam) != 2:
            raise ValueError("each seam must hold two boundary edges")
        sides = []
        for side in seam:
            if type(side) not in (list, tuple) or len(side) != 2:
                raise ValueError("each seam side must be a node pair")
            if any(type(node) is not str for node in side):
                raise ValueError("seam endpoints must be node IDs")
            sides.append(tuple(side))
        (a, b), (c, d) = sides
        first, second = canonical_edge(a, b), canonical_edge(c, d)
        if first == second:
            raise ValueError("a seam must join two distinct original edges")
        for edge in (first, second):
            if edge not in mesh.incidence:
                raise ValueError("seam names an unknown original edge")
            if len(mesh.incidence[edge]) != 1:
                raise ValueError("seams may only join original boundary edges")
            if edge in used:
                raise ValueError("an original edge may participate in one seam only")
            used.add(edge)
        if mesh.nodes[a] != mesh.nodes[c] or mesh.nodes[b] != mesh.nodes[d]:
            raise ValueError("seam endpoint coordinates must match exactly")
        result.append((sides[0], sides[1]))
    return result


def _classify_edges(mesh, paired):
    edge_class, members_by_id = {}, {}
    for edge in sorted(mesh.incidence):
        if edge in edge_class:
            continue
        members = tuple(sorted({edge, paired[edge]} if edge in paired else {edge}))
        edge_id = members[0]
        if edge_id in members_by_id:
            raise ValueError("two stitched edges share an identity")
        members_by_id[edge_id] = members
        for member in members:
            edge_class[member] = edge_id
    return edge_class, members_by_id, sorted(members_by_id)


def _edge_endpoints(node_map, members_by_id):
    endpoints = {}
    for edge_id, members in members_by_id.items():
        u, v = members[0]
        target = tuple(sorted((node_map[u], node_map[v])))
        if target[0] == target[1]:
            raise ValueError("stitched edge collapses to a point")
        for member in members:
            if tuple(sorted((node_map[member[0]], node_map[member[1]]))) != target:
                raise ValueError("paired edges disagree on quotient endpoints")
        endpoints[edge_id] = target
    return endpoints


def _edge_signs(mesh, node_map, edge_class, endpoints):
    signs = {}
    for edge in sorted(mesh.incidence):
        target = endpoints[edge_class[edge]]
        signs[edge] = 1 if (node_map[edge[0]], node_map[edge[1]]) == target else -1
    return signs


def _orient_faces(mesh, face_q, source_edges, edge_class, edge_endpoints):
    t_signs, uses = {}, {}
    for face in sorted(mesh.faces):
        q = face_q[face]
        row = []
        for i in range(3):
            edge_id = edge_class[source_edges[face][i]]
            row.append(1 if (q[i], q[(i + 1) % 3]) == edge_endpoints[edge_id] else -1)
            uses.setdefault(edge_id, []).append(face)
        t_signs[face] = row
    adjacency = {}
    for edge_id, faces in uses.items():
        if len(faces) == 1:
            continue
        if len(faces) != 2 or faces[0] == faces[1]:
            raise ValueError("a stitched edge must join one or two distinct faces")
        f, g = faces
        adjacency.setdefault(f, []).append((g, edge_id))
        adjacency.setdefault(g, []).append((f, edge_id))
    orientation = {}
    for face in sorted(mesh.faces):
        if face in orientation:
            continue
        orientation[face] = 1
        pending = [face]
        while pending:
            current = pending.pop()
            i_of = {edge_class[source_edges[current][i]]: i for i in range(3)}
            for neighbour, edge_id in adjacency.get(current, ()):
                i, j = i_of[edge_id], \
                    [edge_class[e] for e in source_edges[neighbour]].index(edge_id)
                expected = -orientation[current] * t_signs[current][i] * t_signs[neighbour][j]
                if neighbour in orientation:
                    if orientation[neighbour] != expected:
                        raise ValueError("incident faces cannot be oriented consistently")
                    continue
                orientation[neighbour] = expected
                pending.append(neighbour)
    return orientation, uses


def _check_vertex_links(quotient_nodes, face_q, source_edges, edge_class):
    """Every quotient vertex link must be one path or one simple cycle."""
    links, incident_edges, incident_faces = {}, {}, {}
    for face in sorted(face_q):
        q = face_q[face]
        for i in range(3):
            vertex = q[i]
            incoming = edge_class[source_edges[face][(i - 1) % 3]]
            outgoing = edge_class[source_edges[face][i]]
            if incoming == outgoing:
                raise ValueError("vertex neighbourhood is not a disk or half-disk")
            incident_faces[vertex] = incident_faces.get(vertex, 0) + 1
            incident_edges.setdefault(vertex, set()).update((incoming, outgoing))
            links.setdefault(vertex, {}).setdefault(incoming, []).append(outgoing)
            links.setdefault(vertex, {}).setdefault(outgoing, []).append(incoming)
    for vertex in quotient_nodes:
        if vertex not in incident_faces:
            raise ValueError("isolated quotient vertex")
        nodes = incident_edges[vertex]
        arcs = links[vertex]
        degree = {node: len(arcs.get(node, ())) for node in nodes}
        if any(value not in (1, 2) for value in degree.values()):
            raise ValueError("vertex neighbourhood is not a disk or half-disk")
        leaves = [node for node, value in degree.items() if value == 1]
        if leaves and len(leaves) != 2:
            raise ValueError("vertex neighbourhood has separate face fans")
        seen, pending = {next(iter(nodes))}, [next(iter(nodes))]
        while pending:
            for neighbour in arcs.get(pending.pop(), ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    pending.append(neighbour)
        if seen != nodes:
            raise ValueError("vertex neighbourhood has separate face fans")
        arc_count = sum(len(value) for value in arcs.values()) // 2
        if leaves:
            if arc_count != len(nodes) - 1:
                raise ValueError("vertex neighbourhood is not a half-disk")
        else:
            two_cycle = len(nodes) == 2 and arc_count == 2
            if arc_count != len(nodes) and not two_cycle:
                raise ValueError("vertex neighbourhood is not a disk")


def build_stitched_content(revision, seams):
    """Return the complete derived ``{'mesh','fields','stitch'}`` record."""
    mesh, values = revision.mesh, revision.fields
    if type(mesh).__name__ != "OrdinaryMesh":
        raise ValueError("only ordinary V1 revisions can be stitched")
    normalized = _normalize_seams(mesh, seams)
    union, paired = _UnionFind(mesh.nodes), {}
    for (a, b), (c, d) in normalized:
        union.union(a, c)
        union.union(b, d)
        first, second = canonical_edge(a, b), canonical_edge(c, d)
        paired[first], paired[second] = second, first
    node_map = union.quotient_map()
    edge_class, members_by_id, edge_ids = _classify_edges(mesh, paired)
    edge_endpoints = _edge_endpoints(node_map, members_by_id)
    edge_signs = _edge_signs(mesh, node_map, edge_class, edge_endpoints)

    face_q, source_edges = {}, {}
    for face in sorted(mesh.faces):
        q = tuple(node_map[node] for node in mesh.faces[face])
        if len(set(q)) != 3:
            raise ValueError("stitching collapses two face corners")
        require_nondegenerate_triangle(*(mesh.nodes[node] for node in q))
        face_q[face] = q
        source_edges[face] = [term.edge for term in mesh.face_edges[face]]
    orientation, _ = _orient_faces(mesh, face_q, source_edges, edge_class,
                                   edge_endpoints)
    _check_vertex_links(sorted(set(node_map.values())), face_q, source_edges,
                        edge_class)

    face_edges, face_corners, faces = {}, {}, {}
    for face in sorted(mesh.faces):
        q = face_q[face]
        ids = [edge_class[edge] for edge in source_edges[face]]
        signs = [1 if (q[i], q[(i + 1) % 3]) == edge_endpoints[ids[i]] else -1
                 for i in range(3)]
        if orientation[face] > 0:
            faces[face] = q
            face_edges[face] = [(ids[i], signs[i]) for i in range(3)]
            face_corners[face] = (0, 1, 2)
        else:
            faces[face] = (q[0], q[2], q[1])
            face_edges[face] = [(ids[2], -signs[2]), (ids[1], -signs[1]),
                                (ids[0], -signs[0])]
            face_corners[face] = (0, 2, 1)

    per_edge = {}
    for face, terms in face_edges.items():
        for edge_id, _ in terms:
            per_edge.setdefault(edge_id, []).append(face)
    if set(per_edge) != set(edge_ids):
        raise ValueError("stitched edge directory is incomplete")
    for edge_id, faces_using in per_edge.items():
        if len(faces_using) not in (1, 2) or len(set(faces_using)) != len(faces_using):
            raise ValueError("stitched edge must be used once or twice")

    node_scalars = {}
    for quotient_node in sorted(set(node_map.values())):
        candidates = {values.node_scalars[node] for node in mesh.nodes
                      if node_map[node] == quotient_node}
        if len(candidates) != 1:
            raise ValueError("stitched node scalars must agree exactly")
        node_scalars[quotient_node] = candidates.pop()
    edge_integrals = {}
    for edge_id in edge_ids:
        candidates = {edge_signs[edge] * values.edge_integrals[edge]
                      for edge in members_by_id[edge_id]}
        if len(candidates) != 1:
            raise ValueError("stitched edge integrals must agree exactly")
        edge_integrals[edge_id] = candidates.pop()

    mesh_record = {
        "nodes": {node: mesh.nodes[node] for node in sorted(node_scalars)},
        "faces": faces,
        "edges": list(edge_ids),
        "face_edges": {face: [{"edge": edge_id, "sign": sign}
                              for edge_id, sign in face_edges[face]]
                       for face in sorted(faces)},
        "edge_endpoints": {edge_id: edge_endpoints[edge_id] for edge_id in edge_ids},
    }
    fields_record = {"node_scalars": node_scalars,
                     "edge_integrals": edge_integrals,
                     "face_totals": dict(values.face_totals)}
    stitch_record = {
        "source_revision": revision.revision_id,
        "source_digest": revision.fingerprint,
        "source_input": {"mesh": revision.mesh.to_record(),
                         "fields": revision.fields.to_record()},
        "seams": [tuple(side) for side in normalized],
        "node_map": {node: node_map[node] for node in sorted(mesh.nodes)},
        "edge_map": {edge: {"edge": edge_class[edge], "sign": edge_signs[edge]}
                     for edge in sorted(mesh.incidence)},
        "face_map": {face: {"sign": orientation[face],
                            "corners": face_corners[face]}
                     for face in sorted(mesh.faces)},
    }

    parsed = StitchedMesh.from_record(mesh_record, face_corners)
    fields = normalize_fields(parsed, fields_record)
    validate_stitch_record(stitch_record, parsed, fields)
    return {"mesh": mesh_record, "fields": fields_record, "stitch": stitch_record}
