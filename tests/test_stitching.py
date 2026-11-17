import copy
import json
import sqlite3
import tempfile
import unittest
from fractions import Fraction as F
from pathlib import Path

from stitched_cases import cases, parallel, strip, zero_fields
from tests.test_sources import fixture, state, fail_at
from surface.sources.storage import ArchiveStore
from surface.sources.catalog import SourceCatalog
from surface.sources.revisions import RevisionRepository, ConflictError
from surface.fields.bindings import bind_fields
from surface.tasks.plans import PlanService
from surface.tasks.execution import AnalysisService
from surface.results.reading import ResultReader
from surface.results.exporting import ResultExporter
from surface.results.checking import check_export, check_result_payload


def build(store, name, case):
    catalog = SourceCatalog(store)
    source = catalog.put_source(name, case["mesh"], case["fields"])
    receipt = catalog.build_stitched(source["revision_id"], case["seams"])
    return catalog, source, receipt


def analysed(store, request, revision_id, samples):
    plan = PlanService(store).create_plan(request, revision_id, samples)
    result_id = AnalysisService(store).execute(plan)
    return plan, result_id


class TestStitchedSources(unittest.TestCase):
    def setUp(self):
        self.store = ArchiveStore.open()
        self.addCleanup(self.store.close)
        self.catalog = SourceCatalog(self.store)

    def test_parallel_identity_orientation_and_fields(self):
        case = parallel()
        before = self.catalog.put_source("parallel-source", case["mesh"], case["fields"])
        receipt = self.catalog.build_stitched(before["revision_id"], case["seams"])
        self.assertEqual(set(receipt), {"revision_id", "fingerprint"})
        self.assertNotEqual(receipt["revision_id"], before["revision_id"])
        self.assertEqual(self.catalog.current_revision(), receipt["revision_id"])
        fixed = self.catalog.read_source(receipt["revision_id"])
        self.assertEqual(set(fixed), {"revision_id", "fingerprint", "mesh", "fields", "stitch"})
        mesh = fixed["mesh"]
        self.assertEqual(set(mesh), {"nodes", "faces", "edges", "face_edges", "edge_endpoints"})
        # Quotient names are the minimal source node IDs, coordinates exact.
        self.assertEqual(mesh["nodes"], {"A": (F(0), F(0), F(0)), "B": (F(1), F(0), F(0)),
                                         "C": (F(0), F(1), F(0))})
        self.assertEqual(mesh["edges"], [("A", "B"), ("A", "C"), ("B", "C"), ("X", "Z")])
        self.assertEqual(mesh["edge_endpoints"],
                         {("A", "B"): ("A", "B"), ("A", "C"): ("A", "C"),
                          ("B", "C"): ("B", "C"), ("X", "Z"): ("A", "C")})
        # f1 is the minimum face ID of its component and keeps source winding,
        # f2 is flipped to make the two internal edges oppose.
        self.assertEqual(mesh["faces"], {"f1": ("A", "B", "C"), "f2": ("A", "C", "B")})
        self.assertEqual(mesh["face_edges"]["f1"],
                         [{"edge": ("A", "B"), "sign": 1},
                          {"edge": ("B", "C"), "sign": 1},
                          {"edge": ("A", "C"), "sign": -1}])
        self.assertEqual(mesh["face_edges"]["f2"],
                         [{"edge": ("X", "Z"), "sign": 1},
                          {"edge": ("B", "C"), "sign": -1},
                          {"edge": ("A", "B"), "sign": -1}])
        self.assertEqual(fixed["fields"], {
            "node_scalars": {"A": F(0), "B": F(2), "C": F(4)},
            "edge_integrals": {("A", "B"): F(1), ("A", "C"): F(2),
                               ("B", "C"): F(2), ("X", "Z"): F(5)},
            "face_totals": {"f1": F(0), "f2": F(-3)}})
        stitch = fixed["stitch"]
        self.assertEqual(stitch["source_revision"], before["revision_id"])
        self.assertEqual(stitch["source_digest"], before["fingerprint"])
        self.assertEqual(stitch["source_input"],
                         {"mesh": self.catalog.read_source(before["revision_id"])["mesh"],
                          "fields": case["fields"]})
        self.assertEqual(stitch["seams"],
                         [(("A", "B"), ("X", "Y")), (("B", "C"), ("Y", "Z"))])
        self.assertEqual(stitch["node_map"],
                         {"A": "A", "B": "B", "C": "C", "X": "A", "Y": "B", "Z": "C"})
        self.assertEqual(stitch["edge_map"], {
            ("A", "B"): {"edge": ("A", "B"), "sign": 1},
            ("A", "C"): {"edge": ("A", "C"), "sign": 1},
            ("B", "C"): {"edge": ("B", "C"), "sign": 1},
            ("X", "Y"): {"edge": ("A", "B"), "sign": 1},
            ("X", "Z"): {"edge": ("X", "Z"), "sign": 1},
            ("Y", "Z"): {"edge": ("B", "C"), "sign": 1}})
        self.assertEqual(stitch["face_map"], {
            "f1": {"sign": 1, "corners": (0, 1, 2)},
            "f2": {"sign": -1, "corners": (0, 2, 1)}})
        # Derived digest is over mesh + fields + stitch.
        from surface.results.codec import canonical_digest
        self.assertEqual(canonical_digest(
            {"mesh": mesh, "fields": fixed["fields"], "stitch": stitch}),
            receipt["fingerprint"])
        self.assertEqual(stitch["source_digest"],
                         canonical_digest(stitch["source_input"]))

    def test_reversed_pairing_yields_same_ledger(self):
        case = parallel(reverse=True)
        catalog, source, receipt = build(self.store, "reversed-source", case)
        fixed = catalog.read_source(receipt["revision_id"])
        self.assertEqual(len(fixed["mesh"]["nodes"]), 3)
        self.assertEqual(len(fixed["mesh"]["edges"]), 4)
        stitch = fixed["stitch"]
        self.assertEqual(stitch["edge_map"][("X", "Y")], {"edge": ("A", "B"), "sign": -1})
        self.assertEqual(stitch["edge_map"][("X", "Z")], {"edge": ("B", "C"), "sign": 1})
        self.assertEqual(fixed["fields"]["edge_integrals"][("A", "B")], F(1))
        self.assertEqual(fixed["fields"]["edge_integrals"][("B", "C")], F(2))
        bundle = self._bundle(receipt["revision_id"])
        self.assertEqual(bundle.edge_value("A", "C", edge_id=("Y", "Z")), F(5))
        for direction, sign in ((("A", "B"), 1), (("B", "A"), -1)):
            self.assertEqual(bundle.edge_value(*direction), F(sign))
        plan, result_id = analysed(self.store, "reversed-qc",
                                   receipt["revision_id"], case["samples"])
        payload = ResultReader(self.store).read_result(result_id)["payload"]
        self.assertEqual(payload["circulations"], {"f1": F(1), "f2": F(2)})
        self.assertEqual(payload["balance"]["boundary_total"], F(3))
        self.assertEqual(payload["balance"]["internal_total"], F(0))
        self.assertEqual(payload["balance"]["gap_total"], F(6))
        self.assertEqual(payload["samples"][0]["value"], F(4, 3))
        self.assertEqual(payload["samples"][0]["position"], (F(1, 3), F(1, 6), F(0)))
        check_result_payload(payload)

    def _bundle(self, revision_id):
        revision = RevisionRepository(self.store).read(revision_id)
        return bind_fields(revision.revision_id, revision.mesh, revision.fields)

    def test_empty_pairing_preserves_shared_ordinary_edges(self):
        mesh, fields, samples = fixture()
        source = self.catalog.put_source("shared-edge", mesh, fields)
        receipt = self.catalog.build_stitched(source["revision_id"], [])
        fixed = self.catalog.read_source(receipt["revision_id"])
        self.assertEqual(fixed["mesh"]["nodes"], mesh["nodes"])
        self.assertEqual(fixed["mesh"]["faces"], mesh["faces"])
        self.assertEqual(fixed["mesh"]["edges"],
                         [("A", "B"), ("A", "C"), ("B", "C"), ("B", "D"), ("C", "D")])
        self.assertEqual(fixed["stitch"]["seams"], [])
        self.assertEqual(fixed["fields"], fields)
        plan, result_id = analysed(self.store, "shared-edge-qc",
                                   receipt["revision_id"], samples)
        payload = ResultReader(self.store).read_result(result_id)["payload"]
        self.assertEqual(payload["circulations"], {"f1": F(62, 105), "f2": F(37, 70)})
        self.assertEqual(payload["balance"]["internal_terms"],
                         [{"edge": ("B", "C"), "coefficient": 0, "value": F(0)}])
        # A and D are connected by no edge at all.
        bundle = self._bundle(receipt["revision_id"])
        with self.assertRaises(KeyError):
            bundle.edge_value("A", "D")
        with self.assertRaises(KeyError):
            bundle.edge_value("A", "Z")

    def test_second_build_adds_another_revision_without_replay(self):
        case = parallel()
        catalog, source, first = build(self.store, "twice", case)
        second = catalog.build_stitched(source["revision_id"], case["seams"])
        self.assertNotEqual(first["revision_id"], second["revision_id"])
        self.assertEqual(catalog.current_revision(), second["revision_id"])
        self.assertEqual(catalog.read_source(first["revision_id"]),
                         catalog.read_source(second["revision_id"]) | {
                             "revision_id": first["revision_id"],
                             "fingerprint": first["fingerprint"]})
        # Derived revisions cannot be stitched again.
        with self.assertRaises(ValueError):
            catalog.build_stitched(first["revision_id"], [])
        with self.assertRaises(KeyError):
            catalog.build_stitched(999, [])
        with self.assertRaises(KeyError):
            catalog.build_stitched("1", [])

    def test_illegal_pairings_leave_no_revision(self):
        case = parallel()
        source = self.catalog.put_source("illegal", case["mesh"], case["fields"])
        snapshot = state(self.store)
        internal = (("A", "B"), ("A", "C"))  # AB and AC of one triangle
        bad = [
            [(("A", "B"), ("Q", "Y"))],                      # unknown edge
            [(("A", "Q"), ("X", "Y"))],                      # unknown node
            [(("A", "B"), ("A", "C"))],                      # not a boundary edge
            [(("A", "B"), ("C", "A"))],                      # same edge twice
            [(("A", "B"), ("X", "Y")), (("A", "B"), ("Y", "Z"))],  # reused side
            [(("A", "B"), ("X", "Z"))],                      # coordinates differ
            [(("A", "B"),)],                                 # malformed outer
            [(("A", "B", "C"), ("X", "Y"))],                 # malformed side
            [("A", "B")],                                    # not a pair of pairs
            [(("A", 7), ("X", "Y"))],                        # non-string endpoint
        ]
        for seams in bad:
            with self.subTest(seams=seams):
                with self.assertRaises(ValueError):
                    self.catalog.build_stitched(source["revision_id"], seams)
                self.assertEqual(state(self.store), snapshot)
                self.assertEqual(self.catalog.current_revision(),
                                 source["revision_id"])
        with self.assertRaises(ValueError):
            self.catalog.build_stitched(source["revision_id"], "not-a-sequence")
        self.assertEqual(state(self.store), snapshot)
        # An internal edge truly is rejected, not merely due to reuse.
        with self.assertRaises(ValueError):
            self.catalog.build_stitched(source["revision_id"], [internal])

    def test_topology_and_field_rejections(self):
        snapshot_by_case = {}
        for name, case in cases().items():
            store = ArchiveStore.open()
            try:
                catalog = SourceCatalog(store)
                source = catalog.put_source(name, case["mesh"], case["fields"])
                snapshot = state(store)
                snapshot_by_case[name] = snapshot
                if case["expected"] == "reject":
                    with self.subTest(case=name):
                        with self.assertRaises(ValueError):
                            catalog.build_stitched(source["revision_id"], case["seams"])
                        self.assertEqual(state(store), snapshot)
                        self.assertEqual(catalog.current_revision(),
                                         source["revision_id"])
            finally:
                store.close()
        # Explicitly name the reasons so a rejection cannot pass by accident.
        expectations = {
            "mobius": "orient", "pinched": "fan",
            "node_conflict": "node scalar", "edge_conflict": "edge integral",
        }
        for name, fragment in expectations.items():
            case = cases()[name]
            store = ArchiveStore.open()
            try:
                catalog = SourceCatalog(store)
                source = catalog.put_source(name, case["mesh"], case["fields"])
                with self.assertRaises(ValueError) as caught:
                    catalog.build_stitched(source["revision_id"], case["seams"])
                self.assertIn(fragment, str(caught.exception))
            finally:
                store.close()

    def test_isolated_vertex_is_rejected(self):
        mesh = {"nodes": {"A": (F(0), F(0), F(0)), "B": (F(1), F(0), F(0)),
                          "C": (F(0), F(1), F(0)), "Z": (F(9), F(9), F(9))},
                "faces": {"f1": ("A", "B", "C")}}
        fields = zero_fields(mesh)
        source = self.catalog.put_source("isolated", mesh, fields)
        snapshot = state(self.store)
        with self.assertRaises(ValueError):
            self.catalog.build_stitched(source["revision_id"], [])
        self.assertEqual(state(self.store), snapshot)

    def test_nonmanifold_edge_after_stitching_is_rejected(self):
        # Two triangles share AB in the same direction; the quotient edge AB has
        # two faces, but stitching X->A and Y->B additionally identifies two
        # more faces onto the same edge, producing three incident faces.
        mesh = {"nodes": {"A": (F(0), F(0), F(0)), "B": (F(1), F(0), F(0)),
                          "C": (F(0), F(1), F(0)), "D": (F(1), F(1), F(0)),
                          "X": (F(-1), F(0), F(0)), "Y": (F(-2), F(0), F(0)),
                          "P": (F(-1), F(1), F(0)), "Q": (F(-2), F(1), F(0))},
                "faces": {"f1": ("A", "B", "C"), "f2": ("B", "A", "D"),
                          "f3": ("X", "Y", "P"), "f4": ("Y", "X", "Q")}}
        fields = zero_fields(mesh)
        source = self.catalog.put_source("overfull", mesh, fields)
        snapshot = state(self.store)
        with self.assertRaises(ValueError):
            self.catalog.build_stitched(source["revision_id"],
                                        [(("A", "B"), ("X", "Y"))])
        self.assertEqual(state(self.store), snapshot)

    def test_components_anchor_on_minimum_source_face(self):
        # Two disjoint components. In each, the smallest face ID keeps its
        # winding and the other face flips because both traverse their shared
        # edge in the same canonical direction.
        mesh = {"nodes": {"a": (F(0), F(0), F(0)), "b": (F(1), F(0), F(0)),
                          "c": (F(0), F(1), F(0)), "d": (F(1), F(1), F(0)),
                          "p": (F(5), F(0), F(0)), "q": (F(6), F(0), F(0)),
                          "r": (F(5), F(1), F(0)), "s": (F(6), F(1), F(0))},
                "faces": {"m1": ("a", "b", "c"), "m2": ("b", "c", "d"),
                          "z1": ("p", "s", "q"), "z2": ("q", "p", "r")}}
        fields = zero_fields(mesh)
        source = self.catalog.put_source("components", mesh, fields)
        receipt = self.catalog.build_stitched(source["revision_id"], [])
        fixed = self.catalog.read_source(receipt["revision_id"])
        self.assertEqual(fixed["mesh"]["faces"]["m1"], ("a", "b", "c"))
        self.assertEqual(fixed["mesh"]["faces"]["m2"], ("b", "d", "c"))
        self.assertEqual(fixed["mesh"]["faces"]["z1"], ("p", "s", "q"))
        self.assertEqual(fixed["mesh"]["faces"]["z2"], ("q", "r", "p"))
        self.assertEqual(fixed["stitch"]["face_map"]["m1"],
                         {"sign": 1, "corners": (0, 1, 2)})
        self.assertEqual(fixed["stitch"]["face_map"]["m2"],
                         {"sign": -1, "corners": (0, 2, 1)})
        self.assertEqual(fixed["stitch"]["face_map"]["z1"],
                         {"sign": 1, "corners": (0, 1, 2)})
        self.assertEqual(fixed["stitch"]["face_map"]["z2"],
                         {"sign": -1, "corners": (0, 2, 1)})
        # Anchors are per component, not global: z1 keeps its winding although
        # it is not the globally smallest face.
        self.assertEqual(fixed["mesh"]["edge_endpoints"],
                         {("a", "b"): ("a", "b"), ("a", "c"): ("a", "c"),
                          ("b", "c"): ("b", "c"), ("b", "d"): ("b", "d"),
                          ("c", "d"): ("c", "d"),
                          ("p", "q"): ("p", "q"), ("p", "r"): ("p", "r"),
                          ("p", "s"): ("p", "s"), ("q", "r"): ("q", "r"),
                          ("q", "s"): ("q", "s")})

    def test_field_conflict_values_are_not_averaged(self):
        case = parallel()
        bad = copy.deepcopy(case)
        bad["fields"]["edge_integrals"][("X", "Y")] = F(2)
        source = self.catalog.put_source("conflict", bad["mesh"], bad["fields"])
        snapshot = state(self.store)
        with self.assertRaises(ValueError) as caught:
            self.catalog.build_stitched(source["revision_id"], bad["seams"])
        self.assertIn("edge integral", str(caught.exception))
        self.assertEqual(state(self.store), snapshot)
        # Face totals are carried unchanged even when the face flips.
        good = parallel()
        good["fields"]["face_totals"] = {"f1": F(11), "f2": F(-13)}
        src = self.catalog.put_source("totals", good["mesh"], good["fields"])
        receipt = self.catalog.build_stitched(src["revision_id"], good["seams"])
        fixed = self.catalog.read_source(receipt["revision_id"])
        self.assertEqual(fixed["fields"]["face_totals"], {"f1": F(11), "f2": F(-13)})

    def test_container_isolation(self):
        case = parallel()
        source = self.catalog.put_source("isolation", case["mesh"], case["fields"])
        seams = [[list(side) for side in seam] for seam in case["seams"]]
        receipt = self.catalog.build_stitched(source["revision_id"], seams)
        original = self.catalog.read_source(receipt["revision_id"])
        seams[0][0][0] = "Z"
        seams.append([["A", "B"], ["Y", "Z"]])
        case["mesh"]["nodes"]["A"] = (F(50), F(0), F(0))
        case["fields"]["node_scalars"]["A"] = F(50)
        self.assertEqual(self.catalog.read_source(receipt["revision_id"]), original)
        detached = self.catalog.read_source(receipt["revision_id"])
        detached["stitch"]["node_map"]["A"] = "Z"
        detached["stitch"]["seams"][0] = (("Q", "R"), ("S", "T"))
        detached["mesh"]["faces"]["f1"] = ("C", "B", "A")
        detached["fields"]["edge_integrals"][("A", "B")] = F(99)
        receipt["revision_id"] = 99
        self.assertEqual(self.catalog.read_source(original["revision_id"]), original)

    def test_stitched_plan_and_result_binding(self):
        case = parallel()
        _, source, receipt = build(self.store, "binding", case)
        plan, result_id = analysed(self.store, "binding-qc",
                                   receipt["revision_id"], case["samples"])
        saved_plan = PlanService(self.store).read_plan(plan)
        self.assertEqual(saved_plan.convention, "stitched-surface-v1")
        self.assertEqual(saved_plan.source_digest, receipt["fingerprint"])
        payload = ResultReader(self.store).read_result(result_id)["payload"]
        self.assertEqual(payload["kind"], "stitched-analysis-v1")
        self.assertEqual(payload["binding"]["convention"], "stitched-surface-v1")
        self.assertEqual(payload["binding"]["revision_id"], receipt["revision_id"])
        self.assertEqual(payload["binding"]["source_digest"], receipt["fingerprint"])
        self.assertEqual(payload["input"]["stitch"], payload["input"]["stitch"])
        self.assertEqual(payload["requests"], case["samples"])
        self.assertEqual(AnalysisService(self.store).execute(plan), result_id)
        # A V1 plan on the source revision keeps the ordinary convention.
        ordinary_plan = PlanService(self.store).create_plan(
            "ordinary-qc", source["revision_id"], [])
        ordinary = ResultReader(self.store).read_result(
            AnalysisService(self.store).execute(ordinary_plan))["payload"]
        self.assertEqual(ordinary["kind"], "ordinary-analysis-v1")
        self.assertEqual(ordinary["binding"]["convention"], "ordinary-surface-v1")

    def test_flipped_samples_keep_source_corner_order(self):
        case = parallel()
        _, _, receipt = build(self.store, "sampling", case)
        flipped = {"face": "f2", "weights": (F(1, 6), F(1, 3), F(1, 2))}
        repeated = [copy.deepcopy(flipped), copy.deepcopy(flipped),
                    {"face": "f1", "weights": (F(0), F(1), F(0))}]
        plan, result_id = analysed(self.store, "sampling-qc",
                                   receipt["revision_id"], repeated)
        payload = ResultReader(self.store).read_result(result_id)["payload"]
        self.assertEqual(payload["requests"], repeated)
        self.assertEqual([item["weights"] for item in payload["samples"]],
                         [item["weights"] for item in repeated])
        # f2 source corners are X,Y,Z at A,B,C; flipping only changes orientation.
        self.assertEqual(payload["samples"][0]["value"], F(8, 3))
        self.assertEqual(payload["samples"][0]["position"], (F(1, 3), F(1, 2), F(0)))
        self.assertEqual(payload["samples"][1], payload["samples"][0])
        self.assertEqual(payload["samples"][2]["value"], F(2))
        self.assertEqual(payload["samples"][2]["position"], (F(1), F(0), F(0)))

    def test_explicit_edge_queries(self):
        case = parallel()
        _, _, receipt = build(self.store, "edges", case)
        bundle = self._bundle(receipt["revision_id"])
        with self.assertRaises(ValueError):
            bundle.edge_value("A", "C")           # two parallel candidates
        self.assertEqual(bundle.edge_value("A", "C", edge_id=("X", "Z")), F(5))
        self.assertEqual(bundle.edge_value("C", "A", edge_id=("X", "Z")), F(-5))
        self.assertEqual(bundle.edge_value("A", "C", edge_id=("A", "C")), F(2))
        self.assertEqual(bundle.edge_value("A", "B", edge_id=("A", "B")), F(1))
        self.assertEqual(bundle.edge_value("B", "A"), F(-1))
        with self.assertRaises(KeyError):
            bundle.edge_value("A", "C", edge_id=("Q", "Z"))
        with self.assertRaises(KeyError):
            bundle.edge_value("A", "Q")
        with self.assertRaises(ValueError):
            bundle.edge_value("A", "A", edge_id=("A", "B"))
        with self.assertRaises(ValueError):
            bundle.edge_value("A", "B", edge_id=("X", "Z"))

    def test_publication_failures_roll_back_and_retry(self):
        case = parallel()
        source = self.catalog.put_source("fault-source", case["mesh"], case["fields"])
        snapshot = state(self.store)
        for stage in ("source_after_revision", "source_after_head"):
            self.store.fault = fail_at(stage)
            with self.assertRaises(sqlite3.OperationalError):
                self.catalog.build_stitched(source["revision_id"], case["seams"])
            self.assertEqual(state(self.store), snapshot)
            self.assertEqual(self.catalog.current_revision(), source["revision_id"])
        self.store.fault = None
        receipt = self.catalog.build_stitched(source["revision_id"], case["seams"])
        published_source = state(self.store)
        plan, result_id = analysed(self.store, "fault-qc",
                                   receipt["revision_id"], case["samples"])
        published_result = state(self.store)
        self.assertNotEqual(published_source, published_result)
        retry_plan = PlanService(self.store).create_plan(
            "fault-retry", receipt["revision_id"], [])
        ready = state(self.store)
        for stage in ("compute_after_samples", "publication_after_result",
                      "publication_after_receipt"):
            self.store.fault = fail_at(stage)
            with self.assertRaises(sqlite3.OperationalError):
                AnalysisService(self.store).execute(retry_plan)
            self.assertEqual(state(self.store), ready)
        self.store.fault = None
        retry_id = AnalysisService(self.store).execute(retry_plan)
        self.assertEqual(AnalysisService(self.store).execute(retry_plan), retry_id)
        self.assertEqual(ResultReader(self.store).read_result(result_id)["payload"]["kind"],
                         "stitched-analysis-v1")
        self.assertEqual(len(state(self.store)["source_revisions"]),
                         len(published_source["source_revisions"]))

    def test_disk_reopen_correction_and_offline_check(self):
        case = parallel()
        with tempfile.TemporaryDirectory(prefix="surface-stitched-") as directory:
            path = Path(directory) / "archive.sqlite"
            store = ArchiveStore.open(path)
            try:
                catalog = SourceCatalog(store)
                source = catalog.put_source("disk", case["mesh"], case["fields"])
                receipt = catalog.build_stitched(source["revision_id"], case["seams"])
                fixed = catalog.read_source(receipt["revision_id"])
                _, result_id = analysed(store, "disk-qc", receipt["revision_id"],
                                        case["samples"])
                reader = ResultReader(store)
                stored = reader.read_result(result_id)
                raw = ResultExporter(reader).export_result(result_id)
                # Upstream correction after the derived snapshot.
                changed = copy.deepcopy(case["fields"])
                changed["node_scalars"]["A"] = F(77)
                changed["face_totals"]["f2"] = F(-99)
                catalog.put_source("disk-correction", case["mesh"], changed)
                self.assertEqual(catalog.read_source(receipt["revision_id"]), fixed)
                self.assertEqual(reader.read_result(result_id), stored)
                self.assertEqual(ResultExporter(reader).export_result(result_id), raw)
            finally:
                store.close()
            store = ArchiveStore.open(path)
            try:
                catalog = SourceCatalog(store)
                self.assertEqual(catalog.read_source(receipt["revision_id"]), fixed)
                reader = ResultReader(store)
                self.assertEqual(reader.read_result(result_id), stored)
                self.assertEqual(ResultExporter(reader).export_result(result_id), raw)
                report = check_export(ResultExporter(reader).export_result(result_id))
                self.assertEqual(report.boundary_total, F(3))
                self.assertEqual(report.internal_total, F(0))
                self.assertEqual(report.gap_total, F(6))
                # Result delivery must not touch source or plan tables.
                denied = []
                forbidden = {"source_revisions", "source_requests", "source_head",
                             "analysis_plans"}
                def authorize(action, table, column, database, trigger):
                    if action == sqlite3.SQLITE_READ and table in forbidden:
                        denied.append(table)
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                store.connection.set_authorizer(authorize)
                self.assertEqual(ResultReader(store).read_result(result_id), stored)
                self.assertEqual(ResultExporter(ResultReader(store)).export_result(result_id),
                                 raw)
                self.assertEqual(denied, [])
                store.connection.set_authorizer(None)
            finally:
                store.close()
            # Offline review after the connection is gone.
            report = check_export(raw)
            self.assertEqual(report.boundary_total, F(3))
            self.assertEqual(report.internal_total, F(0))
            self.assertEqual(report.gap_total, F(6))

    def test_read_and_export_never_rebuild_the_quotient(self):
        import surface.mesh.stitching as stitching
        case = parallel()
        _, _, receipt = build(self.store, "no-rebuild", case)
        _, result_id = analysed(self.store, "no-rebuild-qc",
                                receipt["revision_id"], case["samples"])
        reader = ResultReader(self.store)
        raw = ResultExporter(reader).export_result(result_id)
        original_build = stitching.build_stitched_content

        def forbidden(*args, **kwargs):
            raise AssertionError("saved associations must not be rebuilt")

        stitching.build_stitched_content = forbidden
        try:
            self.assertEqual(self.catalog.read_source(receipt["revision_id"]),
                             self.catalog.read_source(receipt["revision_id"]))
            reader.read_result(result_id)
            self.assertEqual(ResultExporter(reader).export_result(result_id), raw)
            check_export(raw)
        finally:
            stitching.build_stitched_content = original_build

    def test_corrupt_stitched_source_is_reported(self):
        case = parallel()
        _, _, receipt = build(self.store, "corrupt", case)
        row = self.store.connection.execute(
            "SELECT fingerprint,payload FROM source_revisions WHERE id=?",
            (receipt["revision_id"],)).fetchone()
        from surface.results.codec import decode_payload, encode_payload, canonical_digest
        value = decode_payload(row[1])
        value["stitch"]["face_map"]["f2"]["sign"] = 1
        broken = encode_payload(value)
        self.store.connection.execute(
            "UPDATE source_revisions SET fingerprint=?,payload=? WHERE id=?",
            (canonical_digest(value), broken, receipt["revision_id"]))
        with self.assertRaises(ValueError):
            self.catalog.read_source(receipt["revision_id"])
        self.store.connection.execute(
            "UPDATE source_revisions SET fingerprint=?,payload=? WHERE id=?",
            (row[0], row[1], receipt["revision_id"]))
        self.assertEqual(self.catalog.read_source(receipt["revision_id"])["mesh"]["faces"],
                         {"f1": ("A", "B", "C"), "f2": ("A", "C", "B")})

    def test_two_edge_ring_neighbourhood_is_accepted(self):
        # Both boundary edges of each triangle are paired, so the quotient is
        # two faces glued along all three edges. Each vertex link is a 2-cycle,
        # which STITCHING.md allows even though it contains parallel arcs.
        nodes = {"A": (F(0), F(0), F(0)), "B": (F(1), F(0), F(0)),
                 "C": (F(0), F(1), F(0)), "X": (F(0), F(0), F(0)),
                 "Y": (F(1), F(0), F(0)), "Z": (F(0), F(1), F(0))}
        faces = {"f1": ("A", "B", "C"), "f2": ("X", "Y", "Z")}
        fields = {"node_scalars": {k: F(0) for k in nodes},
                  "edge_integrals": {("A", "B"): F(1), ("A", "C"): F(2),
                                     ("B", "C"): F(3), ("X", "Y"): F(1),
                                     ("X", "Z"): F(2), ("Y", "Z"): F(3)},
                  "face_totals": {"f1": F(0), "f2": F(0)}}
        seams = [(("A", "B"), ("X", "Y")), (("B", "C"), ("Y", "Z")),
                 (("A", "C"), ("X", "Z"))]
        source = self.catalog.put_source("ring", {"nodes": nodes, "faces": faces}, fields)
        receipt = self.catalog.build_stitched(source["revision_id"], seams)
        fixed = self.catalog.read_source(receipt["revision_id"])
        self.assertEqual(fixed["mesh"]["nodes"],
                         {"A": nodes["A"], "B": nodes["B"], "C": nodes["C"]})
        self.assertEqual(len(fixed["mesh"]["edges"]), 3)
        self.assertEqual(fixed["mesh"]["faces"], {"f1": ("A", "B", "C"),
                                                  "f2": ("A", "C", "B")})
        bundle = self._bundle(receipt["revision_id"])
        self.assertEqual(bundle.values.edge_integrals,
                         {("A", "B"): F(1), ("B", "C"): F(3), ("A", "C"): F(2)})

    def test_tampered_stitched_export_is_rejected_offline(self):
        from surface.results.codec import decode_payload, encode_payload, canonical_digest
        case = parallel()
        _, _, receipt = build(self.store, "tamper", case)
        _, result_id = analysed(self.store, "tamper-qc",
                                receipt["revision_id"], case["samples"])
        reader = ResultReader(self.store)
        raw = ResultExporter(reader).export_result(result_id)
        for mutate in (
                lambda p: p["input"]["stitch"]["edge_map"].__setitem__(
                    ("X", "Z"), {"edge": ("A", "C"), "sign": 1}),
                lambda p: p["input"]["stitch"]["face_map"]["f2"].__setitem__("sign", 1),
                lambda p: p["input"]["stitch"]["seams"].append((("A", "C"), ("X", "Z"))),
                lambda p: p["input"]["fields"]["node_scalars"].__setitem__("A", F(9)),
                lambda p: p["input"]["mesh"]["edge_endpoints"].__setitem__(
                    ("X", "Z"), ("A", "B")),
                lambda p: p["samples"][0].__setitem__("value", F(9)),
                lambda p: p["balance"].__setitem__("internal_total", F(1)),
                lambda p: p["input"].pop("stitch")):
            envelope = decode_payload(raw)
            payload = envelope["result"]["payload"]
            mutate(payload)
            envelope["result"]["fingerprint"] = canonical_digest(payload)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError):
                    check_export(encode_payload(envelope))
        check_export(raw)

    def test_resealed_stitched_export_is_rejected_offline(self):
        # An attacker who recomputes the source/plan digests still cannot make
        # the offline reviewer accept inconsistent quotient data.
        from surface.results.codec import decode_payload, encode_payload, canonical_digest
        case = parallel()
        _, _, receipt = build(self.store, "reseal", case)
        _, result_id = analysed(self.store, "reseal-qc",
                                receipt["revision_id"], case["samples"])
        reader = ResultReader(self.store)
        raw = ResultExporter(reader).export_result(result_id)

        def reseal(payload):
            binding = payload["binding"]
            binding["source_digest"] = canonical_digest(payload["input"])
            binding["plan_digest"] = canonical_digest({
                "revision_id": binding["revision_id"],
                "source_digest": binding["source_digest"],
                "samples": payload["requests"], "convention": binding["convention"]})

        mutations = {
            "face sign": lambda p: p["input"]["stitch"]["face_map"]["f2"]
            .__setitem__("sign", 1),
            "edge map retarget": lambda p: p["input"]["stitch"]["edge_map"]
            .__setitem__(("X", "Z"), {"edge": ("A", "C"), "sign": 1}),
            "derived face winding": lambda p: p["input"]["mesh"]["faces"]
            .__setitem__("f2", ("A", "B", "C")),
            "edge integral": lambda p: p["input"]["fields"]["edge_integrals"]
            .__setitem__(("A", "C"), F(7)),
            "node scalar": lambda p: p["input"]["fields"]["node_scalars"]
            .__setitem__("A", F(9)),
            "face total": lambda p: p["input"]["fields"]["face_totals"]
            .__setitem__("f1", F(1)),
            "sample weights": lambda p: p["samples"][0]
            .__setitem__("weights", (F(1), F(0), F(0))),
            "circulation": lambda p: p["circulations"].__setitem__("f2", F(0)),
        }
        for name, mutate in mutations.items():
            envelope = decode_payload(raw)
            payload = envelope["result"]["payload"]
            mutate(payload)
            reseal(payload)
            envelope["result"]["fingerprint"] = canonical_digest(payload)
            with self.subTest(mutation=name):
                with self.assertRaises(ValueError):
                    check_export(encode_payload(envelope))
        check_export(raw)

    def test_legacy_v1_archive_opens_without_backfill(self):
        from surface.results.codec import encode_payload, canonical_digest
        from surface.sources.mesh_input import normalize_mesh
        from surface.sources.field_input import normalize_fields
        with tempfile.TemporaryDirectory(prefix="surface-legacy-") as directory:
            path = Path(directory) / "legacy.sqlite"
            # A V1-only archive written without any stitched table or flag.
            mesh, fields, samples = fixture()
            normalized_mesh = normalize_mesh(mesh)
            normalized_fields = normalize_fields(normalized_mesh, fields)
            content = {"mesh": normalized_mesh.to_record(),
                       "fields": normalized_fields.to_record()}
            import sqlite3 as sqlite
            connection = sqlite.connect(path, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE archive_format (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)")
                connection.execute("INSERT INTO archive_format VALUES(1,1)")
                connection.execute("CREATE TABLE source_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, payload BLOB NOT NULL)")
                connection.execute("CREATE TABLE source_requests (request_id TEXT PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES source_revisions(id))")
                connection.execute("CREATE TABLE source_head (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision_id INTEGER NOT NULL REFERENCES source_revisions(id))")
                connection.execute("INSERT INTO source_revisions(fingerprint,payload) VALUES(?,?)",
                                   (canonical_digest(content), encode_payload(content)))
                connection.execute("INSERT INTO source_requests VALUES('legacy',1)")
                connection.execute("INSERT INTO source_head VALUES(1,1)")
                connection.commit()
            finally:
                connection.close()
            store = ArchiveStore.open(path)
            try:
                catalog = SourceCatalog(store)
                self.assertEqual(catalog.current_revision(), 1)
                legacy = catalog.read_source(1)
                self.assertEqual(set(legacy), {"revision_id", "fingerprint",
                                               "mesh", "fields"})
                self.assertEqual(legacy["mesh"], normalized_mesh.to_record())
                derived = catalog.build_stitched(1, [])
                self.assertEqual(catalog.current_revision(), derived["revision_id"])
                self.assertEqual(catalog.read_source(1), legacy)
            finally:
                store.close()
            store = ArchiveStore.open(path)
            try:
                self.assertEqual(SourceCatalog(store).read_source(1), legacy)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
