from dataclasses import dataclass
from surface.results.codec import encode_payload, decode_payload, canonical_digest
from surface.mesh.model import OrdinaryMesh
from surface.mesh.stitched import StitchedMesh, face_corners_from_stitch, \
    validate_stitch_record
from surface.sources.field_input import normalize_fields

ORDINARY = "ordinary"
STITCHED = "stitched"


class ConflictError(ValueError):
    pass


def revision_fingerprint(content):
    return canonical_digest(content)


@dataclass(frozen=True)
class SourceRevision:
    revision_id: int
    fingerprint: str
    mesh: object
    fields: object

    @property
    def kind(self):
        return ORDINARY

    def input_record(self):
        return {"mesh": self.mesh.to_record(), "fields": self.fields.to_record()}

    def to_record(self):
        return {"revision_id": self.revision_id, "fingerprint": self.fingerprint,
                "mesh": self.mesh.to_record(), "fields": self.fields.to_record()}


@dataclass(frozen=True)
class StitchedSourceRevision:
    revision_id: int
    fingerprint: str
    mesh: object
    fields: object
    stitch: object

    @property
    def kind(self):
        return STITCHED

    def input_record(self):
        return {"mesh": self.mesh.to_record(), "fields": self.fields.to_record(),
                "stitch": self.stitch.to_record()}

    def to_record(self):
        return {"revision_id": self.revision_id, "fingerprint": self.fingerprint,
                "mesh": self.mesh.to_record(), "fields": self.fields.to_record(),
                "stitch": self.stitch.to_record()}


def _parse_content(content):
    if type(content) is dict and set(content) == {"mesh", "fields"}:
        mesh = OrdinaryMesh.from_record(content["mesh"])
        fields = normalize_fields(mesh, content["fields"])
        return mesh, fields, None
    if type(content) is dict and set(content) == {"mesh", "fields", "stitch"}:
        stitch = content["stitch"]
        corners = face_corners_from_stitch(stitch)
        mesh = StitchedMesh.from_record(content["mesh"], corners)
        fields = normalize_fields(mesh, content["fields"])
        stitch = validate_stitch_record(content["stitch"], mesh, fields)
        return mesh, fields, stitch
    raise ValueError("invalid source revision content")


class RevisionRepository:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def initialize(store):
        statements = [
            "CREATE TABLE IF NOT EXISTS source_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, payload BLOB NOT NULL)",
            "CREATE TABLE IF NOT EXISTS source_requests (request_id TEXT PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES source_revisions(id))",
            "CREATE TABLE IF NOT EXISTS source_head (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision_id INTEGER NOT NULL REFERENCES source_revisions(id))"]
        for statement in statements:
            store.connection.execute(statement)

    def lookup_request(self, request_id, content):
        row = self.store.connection.execute(
            "SELECT r.id,r.fingerprint,r.payload FROM source_requests q JOIN source_revisions r ON r.id=q.revision_id WHERE q.request_id=?",
            (request_id,)).fetchone()
        if row is None:
            return None
        prior = self.read(row[0])
        if encode_payload(prior.input_record()) != encode_payload(content):
            raise ConflictError("source request bound to different complete content")
        return {"revision_id": row[0], "fingerprint": row[1]}

    def append(self, request_id, content):
        self.store.require_transaction()
        existing = self.lookup_request(request_id, content)
        if existing is not None:
            return existing
        fingerprint = revision_fingerprint(content)
        cursor = self.store.connection.execute(
            "INSERT INTO source_revisions(fingerprint,payload) VALUES(?,?)",
            (fingerprint, encode_payload(content)))
        revision_id = cursor.lastrowid
        self.store.checkpoint("source_after_revision")
        self.store.connection.execute("INSERT INTO source_requests VALUES(?,?)",
                                      (request_id, revision_id))
        self.store.checkpoint("source_after_request")
        self._advance_head(revision_id)
        return {"revision_id": revision_id, "fingerprint": fingerprint}

    def append_stitched(self, content):
        self.store.require_transaction()
        fingerprint = revision_fingerprint(content)
        cursor = self.store.connection.execute(
            "INSERT INTO source_revisions(fingerprint,payload) VALUES(?,?)",
            (fingerprint, encode_payload(content)))
        revision_id = cursor.lastrowid
        self.store.checkpoint("source_after_revision")
        self._advance_head(revision_id)
        return {"revision_id": revision_id, "fingerprint": fingerprint}

    def _advance_head(self, revision_id):
        self.store.connection.execute(
            "INSERT INTO source_head VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET revision_id=excluded.revision_id",
            (revision_id,))
        self.store.checkpoint("source_after_head")

    def read(self, revision_id):
        if type(revision_id) is not int or revision_id < 1:
            raise KeyError(revision_id)
        row = self.store.connection.execute(
            "SELECT fingerprint,payload FROM source_revisions WHERE id=?",
            (revision_id,)).fetchone()
        if row is None:
            raise KeyError(revision_id)
        try:
            content = decode_payload(row[1])
            if (type(content) is not dict
                    or set(content) not in ({"mesh", "fields"},
                                            {"mesh", "fields", "stitch"})
                    or revision_fingerprint(content) != row[0]):
                raise ValueError("source digest mismatch")
            mesh, fields, stitch = _parse_content(content)
            if stitch is None:
                return SourceRevision(revision_id, row[0], mesh, fields)
            return StitchedSourceRevision(revision_id, row[0], mesh, fields, stitch)
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError("damaged source revision") from exc

    def current(self):
        row = self.store.connection.execute(
            "SELECT revision_id FROM source_head WHERE singleton=1").fetchone()
        return None if row is None else row[0]
