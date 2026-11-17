from surface.sources.mesh_input import normalize_mesh
from surface.sources.field_input import normalize_fields
from surface.sources.revisions import RevisionRepository, STITCHED
from surface.mesh.stitching import build_stitched_content


class SourceCatalog:
    def __init__(self, store):
        self.store = store
        self.revisions = RevisionRepository(store)

    def put_source(self, request_id, mesh, fields):
        if type(request_id) is not str or not request_id:
            raise ValueError("nonempty source request ID required")
        mesh = normalize_mesh(mesh)
        fields = normalize_fields(mesh, fields)
        content = {"mesh": mesh.to_record(), "fields": fields.to_record()}
        with self.store.transaction():
            return self.revisions.append(request_id, content)

    def build_stitched(self, source_revision, seams):
        if type(source_revision) is not int or source_revision < 1:
            raise KeyError(source_revision)
        revision = self.revisions.read(source_revision)
        if revision.kind == STITCHED:
            raise ValueError("only ordinary V1 revisions can be stitched")
        content = build_stitched_content(revision, seams)
        with self.store.transaction():
            return self.revisions.append_stitched(content)

    def read_source(self, revision_id):
        return self.revisions.read(revision_id).to_record()

    def current_revision(self):
        return self.revisions.current()
