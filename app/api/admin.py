import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps import Session, require_admin
from app.services.auth import create_api_key, create_project

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class ProjectCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=200, examples=["Acme Mobile App"])]


class ProjectCreated(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    api_key: str = Field(description="Full API key. Shown once; store it securely.")


@router.post("/projects", response_model=ProjectCreated, status_code=201, summary="Create project")
async def create_project_endpoint(body: ProjectCreate, session: Session) -> ProjectCreated:
    """Creates a project and its first API key. Requires the `X-Admin-Token` header."""
    project = await create_project(session, body.name)
    _, full_key = await create_api_key(session, project.id, name="default")
    await session.commit()
    return ProjectCreated(
        id=project.id, name=project.name, created_at=project.created_at, api_key=full_key
    )
