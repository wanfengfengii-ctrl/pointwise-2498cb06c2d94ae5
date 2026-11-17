from dataclasses import dataclass
from surface.fields.bindings import bind_fields
from surface.mesh.barycentric import validate_sample
from surface.sources.revisions import STITCHED
from surface.tasks.plans import CONVENTION, STITCHED_CONVENTION


@dataclass(frozen=True)
class ResolvedInputs:
    plan: object
    revision: object
    bundle: object


def resolve_inputs(plan, revisions):
    revision = revisions.read(plan.revision_id)
    if revision.fingerprint != plan.source_digest:
        raise ValueError("plan source digest mismatch")
    if plan.convention != (STITCHED_CONVENTION if revision.kind == STITCHED
                           else CONVENTION):
        raise ValueError("plan convention does not match its source revision")
    for sample in plan.samples:
        validate_sample(revision.mesh, sample)
    bundle = bind_fields(plan.revision_id, revision.mesh, revision.fields)
    return ResolvedInputs(plan, revision, bundle)
