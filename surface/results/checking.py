import argparse
import json
from dataclasses import dataclass
from fractions import Fraction
from surface.results.codec import decode_payload, canonical_digest

ORDINARY_CONVENTION = "ordinary-surface-v1"
STITCHED_CONVENTION = "stitched-surface-v1"


@dataclass(frozen=True)
class CheckReport:
    faces: int
    samples: int
    boundary_total: Fraction
    internal_total: Fraction
    gap_total: Fraction


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def check_result_payload(payload):
    try:
        return _check(payload)
    except (KeyError, TypeError, IndexError, AttributeError, ZeroDivisionError) as exc:
        raise ValueError("invalid offline quality record") from exc


def _check(p):
    _require(type(p) is dict and set(p) == {
        "kind", "binding", "input", "requests", "samples", "circulations",
        "face_totals", "balance"}, "invalid payload members")
    if p["kind"] == "stitched-analysis-v1":
        return _check_stitched(p)
    return _check_ordinary(p)


# --------------------------------------------------------------------------
# Ordinary V1 review (unchanged contract)
# --------------------------------------------------------------------------

def _check_ordinary(p):
    _require(p["kind"] == "ordinary-analysis-v1", "unsupported analysis")
    binding, source = p["binding"], p["input"]
    _require(type(binding) is dict and set(binding) == {
        "plan_id", "plan_digest", "revision_id", "source_digest", "convention"},
        "incomplete binding")
    _require(all(type(binding[k]) is int and binding[k] > 0
                 for k in ("plan_id", "revision_id")), "invalid binding IDs")
    _require(binding["convention"] == ORDINARY_CONVENTION, "unsupported convention")
    _require(type(source) is dict and set(source) == {"mesh", "fields"},
             "incomplete source snapshot")
    _require(canonical_digest(source) == binding["source_digest"], "source digest mismatch")
    mesh, fields = source["mesh"], source["fields"]
    _require(type(mesh) is dict and set(mesh) == {"nodes", "faces", "edges", "face_edges"},
             "invalid ordinary mesh")
    nodes, faces = mesh["nodes"], mesh["faces"]
    _require(type(nodes) is dict and bool(nodes) and type(faces) is dict
             and 1 <= len(faces) <= 60, "invalid geometry size")
    for node, xyz in nodes.items():
        _require(type(node) is str and bool(node) and type(xyz) is tuple
                 and len(xyz) == 3 and all(isinstance(x, Fraction) for x in xyz),
                 "invalid node coordinates")
    uses, face_terms = {}, {}
    for face, vertices in sorted(faces.items()):
        _require(type(face) is str and bool(face) and type(vertices) is tuple
                 and len(vertices) == 3 and all(type(v) is str and v in nodes for v in vertices)
                 and len(set(vertices)) == 3, "invalid face references")
        _check_nondegenerate(nodes, vertices)
        terms = []
        for i in range(3):
            start, end = vertices[i], vertices[(i + 1) % 3]
            edge = tuple(sorted((start, end)))
            sign = 1 if start < end else -1
            terms.append({"edge": edge, "sign": sign})
            uses.setdefault(edge, []).append(sign)
        face_terms[face] = terms
    _require(all(1 <= len(signs) <= 2 for signs in uses.values()), "nonmanifold edge")
    _require(mesh["edges"] == sorted(uses) and mesh["face_edges"] == face_terms,
             "saved incidence differs from original face winding")
    _require(type(fields) is dict and set(fields) == {
        "node_scalars", "edge_integrals", "face_totals"}, "invalid fields")
    for name, identities in (("node_scalars", nodes), ("edge_integrals", uses),
                             ("face_totals", faces)):
        _require(type(fields[name]) is dict and set(fields[name]) == set(identities)
                 and all(isinstance(x, Fraction) for x in fields[name].values()),
                 "inexact field coverage")
    requests = p["requests"]
    _require(type(requests) is list and type(p["samples"]) is list, "invalid sample sequence")
    expected_samples = []
    for request in requests:
        face, weights = _sample_request(request, faces)
        vertices = faces[face]
        position = tuple(sum((weights[i] * nodes[vertices[i]][axis] for i in range(3)),
                             Fraction(0)) for axis in range(3))
        value = sum((weights[i] * fields["node_scalars"][vertices[i]]
                     for i in range(3)), Fraction(0))
        expected_samples.append({"face": face, "weights": weights,
                                 "position": position, "value": value})
    _check_saved_samples(p["samples"], expected_samples)
    plan_binding = {"revision_id": binding["revision_id"],
                    "source_digest": binding["source_digest"], "samples": requests,
                    "convention": binding["convention"]}
    _require(canonical_digest(plan_binding) == binding["plan_digest"], "plan digest mismatch")
    integrals = fields["edge_integrals"]
    circulations = {face: sum((term["sign"] * integrals[term["edge"]]
                              for term in terms), Fraction(0))
                    for face, terms in face_terms.items()}
    _require(type(p["circulations"]) is dict
             and all(isinstance(x, Fraction) for x in p["circulations"].values())
             and p["circulations"] == circulations, "incorrect face circulation")
    totals = fields["face_totals"]
    _require(type(p["face_totals"]) is dict
             and all(isinstance(x, Fraction) for x in p["face_totals"].values())
             and p["face_totals"] == totals, "incorrect saved face totals")
    expected_balance = _expected_balance(uses, integrals, circulations, totals)
    _require(p["balance"] == expected_balance and
             expected_balance["circulation_total"]
             == expected_balance["boundary_total"] + expected_balance["internal_total"],
             "quality ledger mismatch")
    return CheckReport(len(faces), len(expected_samples),
                       expected_balance["boundary_total"],
                       expected_balance["internal_total"],
                       expected_balance["gap_total"])


# --------------------------------------------------------------------------
# Stitched V2 review: saved source input, seams and maps only
# --------------------------------------------------------------------------

STITCH_KEYS = {"source_revision", "source_digest", "source_input", "seams",
               "node_map", "edge_map", "face_map"}
STITCHED_MESH_KEYS = {"nodes", "faces", "edges", "face_edges", "edge_endpoints"}


def _check_stitched(p):
    binding, source = p["binding"], p["input"]
    _require(type(binding) is dict and set(binding) == {
        "plan_id", "plan_digest", "revision_id", "source_digest", "convention"},
        "incomplete binding")
    _require(all(type(binding[k]) is int and binding[k] > 0
                 for k in ("plan_id", "revision_id")), "invalid binding IDs")
    _require(binding["convention"] == STITCHED_CONVENTION, "unsupported convention")
    _require(type(source) is dict and set(source) == {"mesh", "fields", "stitch"},
             "incomplete stitched source snapshot")
    _require(canonical_digest(source) == binding["source_digest"], "source digest mismatch")
    mesh, fields, stitch = source["mesh"], source["fields"], source["stitch"]
    _require(type(stitch) is dict and set(stitch) == STITCH_KEYS, "invalid stitch record")
    source_input = stitch["source_input"]
    _require(type(source_input) is dict and set(source_input) == {"mesh", "fields"},
             "incomplete stitch source input")
    _require(canonical_digest(source_input) == stitch["source_digest"],
             "stitch source digest mismatch")
    _require(type(stitch["source_revision"]) is int and stitch["source_revision"] > 0,
             "invalid stitch source revision")
    src_nodes, src_faces, src_uses = _source_parts(source_input["mesh"])
    _require(type(source_input["fields"]) is dict
             and set(source_input["fields"]) == {"node_scalars", "edge_integrals",
                                                 "face_totals"}, "invalid source fields")
    for name, identities in (("node_scalars", src_nodes), ("edge_integrals", src_uses),
                             ("face_totals", src_faces)):
        _require(type(source_input["fields"][name]) is dict
                 and set(source_input["fields"][name]) == set(identities)
                 and all(isinstance(x, Fraction)
                         for x in source_input["fields"][name].values()),
                 "inexact source field coverage")

    seams = _saved_seams(stitch["seams"])
    expected = _expected_stitch(src_nodes, src_faces, src_uses, seams)

    _check_saved_mesh(mesh, expected)
    _check_saved_stitch_fields(fields, expected, source_input["fields"])
    _check_saved_maps(stitch, expected, source_input, seams)
    _check_vertex_links(expected)

    requests = p["requests"]
    _require(type(requests) is list and type(p["samples"]) is list,
             "invalid sample sequence")
    expected_samples = []
    for request in requests:
        face, weights = _sample_request(request, src_faces)
        corners = src_faces[face]
        position = tuple(sum((weights[i] * src_nodes[corners[i]][axis]
                              for i in range(3)), Fraction(0)) for axis in range(3))
        value = sum((weights[i] * source_input["fields"]["node_scalars"][corners[i]]
                     for i in range(3)), Fraction(0))
        expected_samples.append({"face": face, "weights": weights,
                                 "position": position, "value": value})
    _check_saved_samples(p["samples"], expected_samples)

    plan_binding = {"revision_id": binding["revision_id"],
                    "source_digest": binding["source_digest"], "samples": requests,
                    "convention": STITCHED_CONVENTION}
    _require(canonical_digest(plan_binding) == binding["plan_digest"],
             "plan digest mismatch")

    integrals = expected["edge_integrals"]
    circulations = {face: sum((sign * integrals[edge] for edge, sign in terms),
                              Fraction(0))
                    for face, terms in expected["face_terms"].items()}
    _require(p["circulations"] == circulations, "incorrect stitched face circulation")
    totals = expected["totals"]
    _require(p["face_totals"] == totals, "incorrect saved face totals")
    expected_balance = _expected_balance(expected["uses"], integrals,
                                         circulations, totals)
    _require(p["balance"] == expected_balance and
             expected_balance["circulation_total"]
             == expected_balance["boundary_total"] + expected_balance["internal_total"],
             "quality ledger mismatch")
    return CheckReport(len(src_faces), len(expected_samples),
                       expected_balance["boundary_total"],
                       expected_balance["internal_total"],
                       expected_balance["gap_total"])


def _check_nondegenerate(nodes, vertices):
    a, b, c = [nodes[node] for node in vertices]
    u = [b[i] - a[i] for i in range(3)]
    v = [c[i] - a[i] for i in range(3)]
    cross = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
    _require(any(x != 0 for x in cross), "degenerate triangle")


def _canonical(a, b):
    return (a, b) if a < b else (b, a)


def _source_parts(mesh):
    _require(type(mesh) is dict and set(mesh) == {
        "nodes", "faces", "edges", "face_edges"}, "invalid ordinary source mesh")
    nodes, faces = mesh["nodes"], mesh["faces"]
    _require(type(nodes) is dict and bool(nodes) and type(faces) is dict
             and 1 <= len(faces) <= 60, "invalid source geometry size")
    for node, xyz in nodes.items():
        _require(type(node) is str and bool(node) and type(xyz) is tuple
                 and len(xyz) == 3 and all(isinstance(x, Fraction) for x in xyz),
                 "invalid source node coordinates")
    uses, terms = {}, {}
    for face, vertices in sorted(faces.items()):
        _require(type(face) is str and bool(face) and type(vertices) is tuple
                 and len(vertices) == 3
                 and all(type(v) is str and v in nodes for v in vertices)
                 and len(set(vertices)) == 3, "invalid source face references")
        _check_nondegenerate(nodes, vertices)
        row = []
        for i in range(3):
            a, b = vertices[i], vertices[(i + 1) % 3]
            edge = _canonical(a, b)
            row.append({"edge": edge, "sign": 1 if (a, b) == edge else -1})
            uses.setdefault(edge, []).append(face)
        terms[face] = row
    _require(all(len(faces_using) <= 2 for faces_using in uses.values()),
             "nonmanifold source edge")
    _require(mesh["edges"] == sorted(uses) and mesh["face_edges"] == terms,
             "source incidence differs from its face winding")
    return nodes, faces, uses


def _saved_seams(value):
    _require(type(value) is list, "seams must be a list")
    result = []
    for seam in value:
        _require(type(seam) is tuple and len(seam) == 2
                 and type(seam[0]) is tuple and type(seam[1]) is tuple
                 and len(seam[0]) == 2 and len(seam[1]) == 2
                 and all(type(v) is str for v in seam[0] + seam[1]),
                 "invalid seam pair")
        result.append((seam[0], seam[1]))
    return result


def _expected_stitch(nodes, faces, uses, seams):
    """Rebuild the quotient from the saved source input and saved seams only."""
    parent = {node: node for node in nodes}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    paired, used = {}, set()
    for (a, b), (c, d) in seams:
        _require(a in nodes and b in nodes and c in nodes and d in nodes,
                 "seam names an unknown node")
        e1, e2 = _canonical(a, b), _canonical(c, d)
        _require(e1 != e2, "a seam must join distinct original edges")
        for edge in (e1, e2):
            _require(edge in uses, "seam names an unknown original edge")
            _require(len(uses[edge]) == 1, "seams must join boundary edges")
            _require(edge not in used, "an edge joins at most one seam")
            used.add(edge)
        _require(nodes[a] == nodes[c] and nodes[b] == nodes[d],
                 "seam endpoint coordinates differ")
        paired[e1], paired[e2] = e2, e1
        ra, rc = find(a), find(c)
        if ra != rc:
            parent[rc] = ra
        rb, rd = find(b), find(d)
        if rb != rd:
            parent[rd] = rb
    groups = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    node_map = {}
    for members in groups.values():
        name = min(members)
        for node in members:
            node_map[node] = name

    edge_class, members_by_id = {}, {}
    for edge in sorted(uses):
        if edge in edge_class:
            continue
        members = tuple(sorted({edge, paired[edge]} if edge in paired else {edge}))
        edge_id = members[0]
        _require(edge_id not in members_by_id, "duplicate stitched edge identity")
        members_by_id[edge_id] = members
        for member in members:
            edge_class[member] = edge_id
    edge_ids = sorted(members_by_id)
    endpoints = {}
    for edge_id, members in members_by_id.items():
        u, v = members[0]
        target = tuple(sorted((node_map[u], node_map[v])))
        _require(target[0] != target[1], "stitched edge collapses")
        for member in members:
            _require(tuple(sorted((node_map[member[0]], node_map[member[1]]))) == target,
                     "paired edges disagree on endpoints")
        endpoints[edge_id] = target
    edge_sign = {}
    for edge in uses:
        direction = (node_map[edge[0]], node_map[edge[1]])
        edge_sign[edge] = 1 if direction == endpoints[edge_class[edge]] else -1

    face_q, t_sign, face_classes = {}, {}, {}
    for face in sorted(faces):
        q = tuple(node_map[node] for node in faces[face])
        _require(len(set(q)) == 3, "stitching collapses a face")
        _check_nondegenerate(nodes, q)
        face_q[face] = q
        classes = []
        for i in range(3):
            a, b = faces[face][i], faces[face][(i + 1) % 3]
            classes.append(edge_class[_canonical(a, b)])
        face_classes[face] = classes
        t_sign[face] = [1 if (q[i], q[(i + 1) % 3]) == endpoints[classes[i]]
                        else -1 for i in range(3)]

    adjacency = {}
    for edge_id in edge_ids:
        owners = [face for face in sorted(faces) if edge_id in face_classes[face]]
        if len(owners) == 1:
            continue
        _require(len(owners) == 2 and owners[0] != owners[1],
                 "stitched edge must join one or two faces")
        f, g = owners
        adjacency.setdefault(f, []).append((g, edge_id))
        adjacency.setdefault(g, []).append((f, edge_id))
    orientation = {}
    for face in sorted(faces):
        if face in orientation:
            continue
        orientation[face] = 1
        pending = [face]
        while pending:
            current = pending.pop()
            for neighbour, edge_id in adjacency.get(current, ()):
                i = face_classes[current].index(edge_id)
                j = face_classes[neighbour].index(edge_id)
                expected_sign = -orientation[current] * t_sign[current][i] \
                    * t_sign[neighbour][j]
                if neighbour in orientation:
                    _require(orientation[neighbour] == expected_sign,
                             "faces cannot be oriented consistently")
                    continue
                orientation[neighbour] = expected_sign
                pending.append(neighbour)

    oriented_faces, face_terms, quotient_uses = {}, {}, {}
    for face in sorted(faces):
        q = face_q[face]
        if orientation[face] > 0:
            oriented_faces[face] = q
            terms = [(face_classes[face][i], t_sign[face][i]) for i in range(3)]
        else:
            oriented_faces[face] = (q[0], q[2], q[1])
            terms = [(face_classes[face][2], -t_sign[face][2]),
                     (face_classes[face][1], -t_sign[face][1]),
                     (face_classes[face][0], -t_sign[face][0])]
        face_terms[face] = terms
        for edge_id, sign in terms:
            quotient_uses.setdefault(edge_id, []).append(sign)
    _require(set(quotient_uses) == set(edge_ids), "orphan stitched edge")
    _require(all(1 <= len(signs) <= 2 for signs in quotient_uses.values()),
             "stitched edge must join one or two faces")

    component = {}
    for face in sorted(faces):
        if face in component:
            continue
        pending = [face]
        component[face] = face
        while pending:
            current = pending.pop()
            for neighbour, _ in adjacency.get(current, ()):
                if neighbour not in component:
                    component[neighbour] = face
                    pending.append(neighbour)

    return {"node_map": node_map, "edge_ids": edge_ids, "endpoints": endpoints,
            "edge_sign": edge_sign, "edge_class": edge_class,
            "members_by_id": members_by_id, "faces": oriented_faces,
            "face_terms": face_terms, "face_q": face_q,
            "orientation": orientation, "face_classes": face_classes,
            "t_sign": t_sign, "uses": quotient_uses, "component": component,
            "source_nodes": nodes, "source_faces": faces, "source_uses": uses}


def _check_saved_mesh(mesh, expected):
    _require(type(mesh) is dict and set(mesh) == STITCHED_MESH_KEYS,
             "invalid stitched mesh")
    nodes = mesh["nodes"]
    _require(type(nodes) is dict, "invalid stitched nodes")
    expected_nodes = {}
    for node in sorted(set(expected["node_map"].values())):
        sources = [name for name, target in expected["node_map"].items() if target == node]
        expected_nodes[node] = expected["source_nodes"][min(sources)]
    _require(nodes == expected_nodes, "stitched node coordinates differ")
    _require(mesh["faces"] == expected["faces"], "stitched face winding differs")
    _require(mesh["edges"] == expected["edge_ids"], "stitched edge directory differs")
    _require(mesh["edge_endpoints"] == expected["endpoints"],
             "stitched edge endpoints differ")
    face_edges = mesh["face_edges"]
    _require(type(face_edges) is dict and set(face_edges) == set(expected["faces"]),
             "incomplete stitched incidence")
    saved_terms = {}
    for face, terms in face_edges.items():
        _require(type(terms) is list and len(terms) == 3, "three terms required")
        saved = []
        for item in terms:
            _require(type(item) is dict and set(item) == {"edge", "sign"}
                     and type(item["sign"]) is int and item["sign"] in (1, -1),
                     "invalid stitched edge term")
            saved.append((item["edge"], item["sign"]))
        saved_terms[face] = saved
    _require(saved_terms == {face: list(terms)
                             for face, terms in expected["face_terms"].items()},
             "stitched incidence differs from oriented winding")


def _check_saved_stitch_fields(fields, expected, source_fields):
    _require(type(fields) is dict and set(fields) == {
        "node_scalars", "edge_integrals", "face_totals"}, "invalid stitched fields")
    expected_scalars = {}
    for source, node in expected["node_map"].items():
        value = source_fields["node_scalars"][source]
        _require(node not in expected_scalars or expected_scalars[node] == value,
                 "stitched node scalars conflict")
        expected_scalars[node] = value
    expected_integrals = {}
    for source, entry in expected["edge_sign"].items():
        edge_id = expected["edge_class"][source]
        value = entry * source_fields["edge_integrals"][source]
        _require(edge_id not in expected_integrals
                 or expected_integrals[edge_id] == value,
                 "stitched edge integrals conflict")
        expected_integrals[edge_id] = value
    for name, expected_values in (("node_scalars", expected_scalars),
                                  ("edge_integrals", expected_integrals),
                                  ("face_totals", source_fields["face_totals"])):
        _require(type(fields[name]) is dict and fields[name] == expected_values
                 and all(isinstance(v, Fraction) for v in fields[name].values()),
                 "stitched field transfer differs")
    expected["edge_integrals"] = expected_integrals
    expected["totals"] = source_fields["face_totals"]


def _check_saved_maps(stitch, expected, source_input, seams):
    node_map = stitch["node_map"]
    _require(node_map == expected["node_map"], "saved node map differs")
    edge_map = stitch["edge_map"]
    _require(type(edge_map) is dict and set(edge_map) == set(expected["source_uses"]),
             "incomplete edge map")
    rebuilt = {edge: {"edge": expected["edge_class"][edge],
                      "sign": expected["edge_sign"][edge]}
               for edge in expected["source_uses"]}
    _require(edge_map == rebuilt, "saved edge map differs")
    face_map = stitch["face_map"]
    _require(type(face_map) is dict and set(face_map) == set(expected["source_faces"]),
             "incomplete face map")
    rebuilt_faces = {}
    for face in expected["source_faces"]:
        sign = expected["orientation"][face]
        rebuilt_faces[face] = {"sign": sign,
                               "corners": (0, 1, 2) if sign > 0 else (0, 2, 1)}
    _require(face_map == rebuilt_faces, "saved face map differs")
    saved = _saved_seams(stitch["seams"])
    _require(saved == seams, "saved seams differ from the exported pairing")


def _check_vertex_links(expected):
    face_q, face_classes = expected["face_q"], expected["face_classes"]
    links, incident_edges = {}, {}
    for face in sorted(face_q):
        q = face_q[face]
        for i in range(3):
            vertex = q[i]
            incoming = face_classes[face][(i - 1) % 3]
            outgoing = face_classes[face][i]
            _require(incoming != outgoing, "vertex link is not a path or cycle")
            incident_edges.setdefault(vertex, set()).update((incoming, outgoing))
            links.setdefault(vertex, {}).setdefault(incoming, []).append(outgoing)
            links.setdefault(vertex, {}).setdefault(outgoing, []).append(incoming)
    _require(set(incident_edges) == set(expected["node_map"].values()),
             "quotient has an isolated vertex")
    for vertex, nodes in incident_edges.items():
        arcs = links[vertex]
        degree = {node: len(arcs.get(node, ())) for node in nodes}
        _require(all(value in (1, 2) for value in degree.values()),
                 "vertex link branches")
        leaves = [node for node, value in degree.items() if value == 1]
        _require(not leaves or len(leaves) == 2, "vertex has separate face fans")
        seen, pending = {next(iter(nodes))}, [next(iter(nodes))]
        while pending:
            for neighbour in arcs.get(pending.pop(), ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    pending.append(neighbour)
        _require(seen == nodes, "vertex has separate face fans")
        arcs_total = sum(len(value) for value in arcs.values()) // 2
        if leaves:
            _require(arcs_total == len(nodes) - 1, "vertex link is not a half-disk")
        else:
            _require(arcs_total == len(nodes)
                     or (len(nodes) == 2 and arcs_total == 2),
                     "vertex link is not a disk")


def _sample_request(request, faces):
    _require(type(request) is dict and set(request) == {"face", "weights"},
             "invalid request")
    face, weights = request["face"], request["weights"]
    _require(type(face) is str and face in faces and type(weights) is tuple
             and len(weights) == 3
             and all(isinstance(w, Fraction) and w >= 0 for w in weights)
             and sum(weights) == 1, "invalid exact barycentric weights")
    return face, weights


def _check_saved_samples(samples, expected_samples):
    for sample in samples:
        _require(type(sample) is dict and set(sample) == {
            "face", "weights", "position", "value"}
            and isinstance(sample["value"], Fraction)
            and type(sample["position"]) is tuple and len(sample["position"]) == 3
            and all(isinstance(x, Fraction) for x in sample["position"]),
            "nonrational sample evidence")
    _require(samples == expected_samples, "sample position/value/order mismatch")


def _expected_balance(uses, integrals, circulations, totals):
    """Ledger from per-edge signed uses (ordinary signs or quotient signs)."""
    boundary, internal = [], []
    for edge, signs in sorted(uses.items()):
        coefficient = sum(signs)
        item = {"edge": edge, "coefficient": coefficient,
                "value": coefficient * integrals[edge]}
        (boundary if len(signs) == 1 else internal).append(item)
    bt = sum((item["value"] for item in boundary), Fraction(0))
    it = sum((item["value"] for item in internal), Fraction(0))
    ct, ft = sum(circulations.values(), Fraction(0)), sum(totals.values(), Fraction(0))
    return {
        "face_gaps": {face: circulations[face] - totals[face] for face in sorted(totals)},
        "boundary_terms": boundary, "internal_terms": internal,
        "boundary_total": bt, "internal_total": it,
        "circulation_total": ct, "face_total": ft, "gap_total": ct - ft}


def check_export(raw):
    envelope = decode_payload(raw)
    _require(type(envelope) is dict and set(envelope) == {
        "export_kind", "export_version", "result"}
        and envelope["export_kind"] == "surface-quality-record"
        and type(envelope["export_version"]) is int and envelope["export_version"] == 1,
        "unsupported export")
    result = envelope["result"]
    _require(type(result) is dict and set(result) == {"result_id", "fingerprint", "payload"}
             and type(result["result_id"]) is int and result["result_id"] > 0,
             "invalid result directory")
    _require(canonical_digest(result["payload"]) == result["fingerprint"],
             "export digest mismatch")
    return check_result_payload(result["payload"])


def load_solver_sample(path):
    with open(path, encoding="utf-8") as stream:
        source = json.load(stream)
    def rational(pair):
        if (type(pair) is not list or len(pair) != 2
                or any(type(x) is not int for x in pair) or pair[1] == 0):
            raise ValueError("sample rational pair required")
        return Fraction(*pair)
    mesh = {"nodes": {node: tuple(rational(x) for x in xyz)
                      for node, xyz in source["mesh"]["nodes"].items()},
            "faces": {face: tuple(vertices) for face, vertices in source["mesh"]["faces"].items()}}
    fields = {"node_scalars": {k: rational(v) for k, v in source["fields"]["node_scalars"].items()},
              "edge_integrals": {tuple(edge): rational(value)
                                 for edge, value in source["fields"]["edge_integrals"]},
              "face_totals": {k: rational(v) for k, v in source["fields"]["face_totals"].items()}}
    samples = [{"face": item["face"], "weights": tuple(rational(w) for w in item["weights"])}
               for item in source["samples"]]
    return mesh, fields, samples


def _functional_demo(sample_path):
    import tempfile
    from pathlib import Path
    from surface.sources.storage import ArchiveStore
    from surface.sources.catalog import SourceCatalog
    from surface.tasks.plans import PlanService
    from surface.tasks.execution import AnalysisService
    from surface.results.reading import ResultReader
    from surface.results.exporting import ResultExporter
    mesh, fields, samples = load_solver_sample(sample_path)
    with tempfile.TemporaryDirectory(prefix="surface-demo-") as directory:
        database = Path(directory) / "quality.sqlite"
        store = ArchiveStore.open(database)
        catalog = SourceCatalog(store)
        receipt = catalog.put_source("solver-day-1", mesh, fields)
        plan = PlanService(store).create_plan("quality-day-1", receipt["revision_id"], samples)
        result_id = AnalysisService(store).execute(plan)
        original = ResultReader(store).read_result(result_id)
        fields["node_scalars"]["A"] = Fraction(99)
        catalog.put_source("solver-correction", mesh, fields)
        store.close()
        reopened = ArchiveStore.open(database)
        try:
            reader = ResultReader(reopened)
            assert reader.read_result(result_id) == original
            raw = ResultExporter(reader).export_result(result_id)
        finally:
            reopened.close()
        report = check_export(raw)
        payload = original["payload"]
        expected = {"samples": ["0", "1", "4/3", "5", "4/3"],
                    "circulations": {"f1": "62/105", "f2": "37/70"}}
        actual = {"samples": [str(item["value"]) for item in payload["samples"]],
                  "circulations": {k: str(v) for k, v in payload["circulations"].items()}}
        downstream = {"boundary": str(report.boundary_total), "internal": str(report.internal_total),
                      "gap": str(report.gap_total), "frozen_revision": payload["binding"]["revision_id"]}
        downstream_expected = {"boundary": "47/42", "internal": "0", "gap": "25/14",
                               "frozen_revision": receipt["revision_id"]}
        assert actual == expected
        assert downstream == downstream_expected
        print("POINTWISE_SCENARIO " + json.dumps({
            "case_id": "v8.offline_exact", "input": {"sample": sample_path, "correction_A": "99"},
            "expected": expected, "actual": actual,
            "downstream_expected": downstream_expected, "downstream_actual": downstream,
            "code_paths": ["surface/tasks/execution.py", "surface/tasks/inputs.py",
                           "surface/fields/sampling.py", "surface/fields/circulation.py",
                           "surface/fields/balances.py", "surface/results/exporting.py",
                           "surface/results/checking.py"],
            "oracle": "independent hand fractions and offline traversal of saved solver input"
        }, sort_keys=True))


def _stitched_demo():
    import tempfile
    from pathlib import Path
    from surface.sources.storage import ArchiveStore
    from surface.sources.catalog import SourceCatalog
    from surface.sources.revisions import RevisionRepository
    from surface.fields.bindings import bind_fields
    from surface.tasks.plans import PlanService
    from surface.tasks.execution import AnalysisService
    from surface.results.reading import ResultReader
    from surface.results.exporting import ResultExporter
    F = Fraction
    nodes = {"A": (F(0), F(0), F(0)), "B": (F(1), F(0), F(0)),
             "C": (F(0), F(1), F(0)), "X": (F(0), F(0), F(0)),
             "Y": (F(1), F(0), F(0)), "Z": (F(0), F(1), F(0))}
    faces = {"f1": ("A", "B", "C"), "f2": ("X", "Y", "Z")}
    fields = {"node_scalars": {"A": F(0), "B": F(2), "C": F(4),
                               "X": F(0), "Y": F(2), "Z": F(4)},
              "edge_integrals": {("A", "B"): F(1), ("A", "C"): F(2), ("B", "C"): F(2),
                                 ("X", "Y"): F(1), ("X", "Z"): F(5), ("Y", "Z"): F(2)},
              "face_totals": {"f1": F(0), "f2": F(-3)}}
    seams = [(("A", "B"), ("X", "Y")), (("B", "C"), ("Y", "Z"))]
    samples = [{"face": "f2", "weights": (F(1, 2), F(1, 3), F(1, 6))}]
    with tempfile.TemporaryDirectory(prefix="surface-stitched-demo-") as directory:
        database = Path(directory) / "quality.sqlite"
        store = ArchiveStore.open(database)
        try:
            catalog = SourceCatalog(store)
            raw = catalog.put_source("parallel-solver", {"nodes": nodes, "faces": faces},
                                     fields)
            derived = catalog.build_stitched(raw["revision_id"], seams)
            fixed = catalog.read_source(derived["revision_id"])
            reader = ResultReader(store)
            exported = None
            plan = PlanService(store).create_plan("parallel-qc", derived["revision_id"], samples)
            result_id = AnalysisService(store).execute(plan)
            stored = reader.read_result(result_id)
            exported = ResultExporter(reader).export_result(result_id)
            revision = RevisionRepository(store).read(derived["revision_id"])
            bundle = bind_fields(revision.revision_id, revision.mesh, revision.fields)
            try:
                bundle.edge_value("A", "C")
                ambiguous = "accepted"
            except ValueError:
                ambiguous = "rejected"
            explicit = str(bundle.edge_value("A", "C", edge_id=("X", "Z")))
            changed = {"node_scalars": dict(fields["node_scalars"]),
                       "edge_integrals": dict(fields["edge_integrals"]),
                       "face_totals": {"f1": F(7), "f2": F(-8)}}
            catalog.put_source("parallel-correction", {"nodes": nodes, "faces": faces},
                               changed)
            store.close()
            store = ArchiveStore.open(database)
            try:
                reader = ResultReader(store)
                assert reader.read_result(result_id) == stored
                exported_after = ResultExporter(reader).export_result(result_id)
            finally:
                store.close()
            assert exported_after == exported
            report = check_export(exported)
            payload = stored["payload"]
            observed = {
                "vertices": len(fixed["mesh"]["nodes"]),
                "edges": len(fixed["mesh"]["edges"]),
                "faces": {k: list(v) for k, v in fixed["mesh"]["faces"].items()},
                "circulations": {k: str(v) for k, v in payload["circulations"].items()},
                "boundary": str(payload["balance"]["boundary_total"]),
                "internal": str(payload["balance"]["internal_total"]),
                "gap": str(payload["balance"]["gap_total"]),
                "sample_value": str(payload["samples"][0]["value"]),
                "sample_position": [str(x) for x in payload["samples"][0]["position"]],
                "sample_weights": [str(x) for x in payload["samples"][0]["weights"]],
                "ambiguous_plain_lookup": ambiguous,
                "explicit_parallel_edge": explicit,
                "reopen_export_identical": exported_after == exported,
                "revision_frozen": payload["binding"]["revision_id"]
                == derived["revision_id"],
            }
            assert observed == {
                "vertices": 3, "edges": 4,
                "faces": {"f1": ["A", "B", "C"], "f2": ["A", "C", "B"]},
                "circulations": {"f1": "1", "f2": "2"},
                "boundary": "3", "internal": "0", "gap": "6",
                "sample_value": "4/3",
                "sample_position": ["1/3", "1/6", "0"],
                "sample_weights": ["1/2", "1/3", "1/6"],
                "ambiguous_plain_lookup": "rejected",
                "explicit_parallel_edge": "5",
                "reopen_export_identical": True, "revision_frozen": True}
            print("POINTWISE_SCENARIO " + json.dumps({
                "case_id": "v9.stitched_parallel",
                "input": {"seams": [[list(side) for side in seam] for seam in seams],
                          "upstream_correction": "face_totals 7/-8"},
                "expected": {
                    "vertices": 3, "edges": 4, "circulations": {"f1": "1", "f2": "2"},
                    "boundary": "3", "internal": "0", "gap": "6",
                    "sample_value": "4/3",
                    "sample_position": ["1/3", "1/6", "0"],
                    "ambiguous_plain_lookup": "rejected",
                    "explicit_parallel_edge": "5"},
                "actual": observed,
                "oracle": "hand seam pairing, source-corner weights and offline "
                          "recomputation of the exported stitch record"
            }, sort_keys=True))
            assert report.boundary_total == F(3)
            assert report.internal_total == F(0)
            assert report.gap_total == F(6)
        finally:
            store.close()


def demo(sample_path):
    _functional_demo(sample_path)
    _stitched_demo()


def main():
    parser = argparse.ArgumentParser(description="Offline surface quality review")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", metavar="SOLVER_JSON")
    group.add_argument("--check", metavar="EXPORT_FILE")
    group.add_argument("--stitched-demo", action="store_true")
    args = parser.parse_args()
    if args.demo:
        demo(args.demo)
    elif args.stitched_demo:
        _stitched_demo()
    else:
        with open(args.check, "rb") as stream:
            report = check_export(stream.read())
        print(json.dumps({"faces": report.faces, "samples": report.samples,
                          "boundary_total": str(report.boundary_total),
                          "internal_total": str(report.internal_total),
                          "gap_total": str(report.gap_total)}, sort_keys=True))


if __name__ == "__main__":
    main()
