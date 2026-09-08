"""Per-person and workspace export (spec 25.4, 26.4)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ...db.mapping import person_to_domain
from ...db.repositories import PeopleRepository, PersonNotFound, RevisionRepository, WorkspaceScope
from ...exporters.json_export import person_to_json, workspace_export
from ...exporters.markdown import person_to_markdown
from ...services.auth import AuthContext
from ..app import db_session
from ..deps import require_auth, workspace_scope

router = APIRouter(prefix="/export")


def _person(scope: WorkspaceScope, public_id: str):  # type: ignore[no-untyped-def]
    try:
        return person_to_domain(PeopleRepository(scope).require(public_id, include_archived=True))
    except PersonNotFound:
        raise HTTPException(status_code=404, detail="No such person") from None


@router.get("/person/{public_id}.md")
def person_markdown(public_id: str, scope: WorkspaceScope = Depends(workspace_scope)) -> Response:
    md = person_to_markdown(_person(scope, public_id))
    return Response(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{public_id}.md"'},
    )


@router.get("/person/{public_id}.json")
def person_json(public_id: str, scope: WorkspaceScope = Depends(workspace_scope)) -> Response:
    return Response(
        person_to_json(_person(scope, public_id)),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{public_id}.json"'},
    )


@router.get("/workspace.json")
def workspace_json(
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    repo = PeopleRepository(scope)
    active = [person_to_domain(r) for r in repo.list_people(archived=False)]
    archived = [person_to_domain(r) for r in repo.list_people(archived=True)]

    rev_repo = RevisionRepository(scope)
    revisions = []
    for r in [*repo.list_people(archived=None)]:
        for rev in rev_repo.for_person(r.id, limit=1000):
            revisions.append(
                {
                    "person_public_id": r.public_id,
                    "entity_type": rev.entity_type,
                    "field": rev.field,
                    "old": rev.old_value,
                    "new": rev.new_value,
                    "actor_type": rev.actor_type,
                    "actor_id": rev.actor_id,
                    "authority": rev.authority,
                    "reason": rev.reason,
                    "occurred_at": rev.occurred_at.isoformat(),
                }
            )

    payload = workspace_export(
        workspace_name=auth.workspace_slug,
        workspace_slug=auth.workspace_slug,
        active=active,
        archived=archived,
        revisions=revisions,
    )
    return Response(
        json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="montauk-{auth.workspace_slug}-export.json"'},
    )
