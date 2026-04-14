"""Pydantic models for knowledge base entries."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Valid content types matching the DB CHECK constraint
VALID_CONTENT_TYPES = {
    "context",
    "project",
    "meeting",
    "decision",
    "intelligence",
    "daily",
    "resource",
    "department",
    "team",
    "system",
    "onboarding",
}

VALID_SENSITIVITIES = {
    "system",
    "strategic",
    "operational",
    "private",
    "project",
    "meeting",
    "shared",
}


class EntryCreate(BaseModel):
    title: str
    content: str
    summary: str | None = None
    content_type: str  # must be one of the 11 valid types
    logical_path: str
    sensitivity: str = "shared"
    department: str | None = None
    tags: list[str] = []
    domain_meta: dict = {}
    project_id: str | None = None


class EntryUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    content_type: str | None = None
    logical_path: str | None = None
    sensitivity: str | None = None
    department: str | None = None
    tags: list[str] | None = None
    domain_meta: dict | None = None
    expected_version: int | None = None


class EntryAppend(BaseModel):
    content: str  # content to append
    separator: str = "\n\n"  # separator between existing and new content
    expected_version: int | None = None


class EntryResponse(BaseModel):
    id: str
    org_id: str
    title: str
    content: str
    summary: str | None
    content_type: str
    logical_path: str
    sensitivity: str
    department: str | None
    owner_id: str | None
    tags: list[str]
    domain_meta: dict
    version: int
    status: str
    source: str
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class EntryList(BaseModel):
    entries: list[EntryResponse]
    total: int
    limit: int
    offset: int


# =============================================================================
# Link models
# =============================================================================

VALID_LINK_TYPES = {
    "relates_to",
    "supersedes",
    "contradicts",
    "depends_on",
    "part_of",
    "tagged_with",
}


class LinkCreate(BaseModel):
    target_entry_id: str
    link_type: str  # relates_to, supersedes, contradicts, depends_on, part_of, tagged_with
    weight: float = 1.0
    metadata: dict = {}


class LinkResponse(BaseModel):
    id: str
    source_entry_id: str
    target_entry_id: str
    link_type: str
    weight: float
    metadata: dict
    created_by: str
    source: str
    created_at: datetime


class LinkNeighbor(BaseModel):
    entry_id: str
    title: str
    summary: str | None
    content_type: str
    link_type: str
    weight: float
    depth: int


class TraversalResponse(BaseModel):
    origin_id: str
    depth: int
    neighbors: list[LinkNeighbor]


# =============================================================================
# Graph models (bulk org-wide graph for frontend /graph page)
# =============================================================================


class GraphNode(BaseModel):
    id: str
    title: str
    content_type: str
    logical_path: str
    summary: str | None
    updated_at: datetime


class GraphEdge(BaseModel):
    source: str
    target: str
    link_type: str
    weight: float


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    total_nodes: int
    total_edges: int
    truncated: bool
    generated_at: datetime


# =============================================================================
# Index models (tiered index map, L1-L5)
# =============================================================================


class IndexCategory(BaseModel):
    content_type: str
    count: int


class IndexEntry(BaseModel):
    id: str
    title: str
    content_type: str
    logical_path: str
    updated_at: datetime


class IndexRelationship(BaseModel):
    source_id: str
    target_id: str
    link_type: str


class IndexResponse(BaseModel):
    depth: int
    total_entries: int
    categories: list[IndexCategory]
    entries: list[IndexEntry] | None = None  # L2+
    relationships: list[IndexRelationship] | None = None  # L3+
    summaries: dict[str, str] | None = None  # L4+ (entry_id -> summary)
    contents: dict[str, str] | None = None  # L5+ (entry_id -> content)


# =============================================================================
# Staging / Governance models
# =============================================================================


class StagingSubmit(BaseModel):
    target_entry_id: str | None = None  # None = new entry creation
    target_path: str
    change_type: str = "create"  # create, update, append, delete, create_link
    proposed_title: str | None = None
    proposed_content: str | None = None  # None for metadata-only updates
    proposed_meta: dict | None = None
    content_type: str | None = None  # explicit content_type; required for create
    submission_category: str = "user_direct"
    expected_version: int | None = None


class StagingResponse(BaseModel):
    id: str
    org_id: str
    target_entry_id: str | None
    target_path: str
    change_type: str
    proposed_title: str | None
    proposed_content: str | None
    proposed_meta: dict | None
    governance_tier: int
    submission_category: str
    status: str
    priority: int
    submitted_by: str
    source: str
    created_at: datetime
    promoted_entry_id: str | None = None


class StagingList(BaseModel):
    items: list[StagingResponse]
    total: int


class ReviewAction(BaseModel):
    reason: str | None = None


class AIReviewResult(BaseModel):
    """Result from the AI reviewer for Tier 3 staging items."""
    action: str  # "approve", "reject", or "escalate"
    reasoning: str
    confidence: float = 0.0


class ProcessResult(BaseModel):
    approved: int
    flagged: int
    rejected: int
    details: list[dict]


# =============================================================================
# Import models
# =============================================================================


class ImportFile(BaseModel):
    filename: str
    content: str


class ImportRequest(BaseModel):
    files: list[ImportFile]
    base_path: str = ""  # prefix for logical_path


class ImportSummary(BaseModel):
    created: int
    staged: int
    linked: int
    errors: list[str]
    type_mappings: dict[str, str] = {}
    unrecognized_types: list[str] = []


VALID_COLLISION_TYPES = {"path", "title", "content_hash"}
VALID_COLLISION_RESOLUTIONS = {"skip", "rename", "merge"}


class CollisionEntry(BaseModel):
    filename: str
    proposed_title: str
    proposed_path: str
    existing_entry_id: str | None = None
    existing_title: str | None = None
    collision_type: str  # path, title, content_hash
    resolution: str = "skip"  # skip, rename, merge


class ImportPreviewRequest(BaseModel):
    files: list[ImportFile]
    base_path: str = ""
    dry_run: bool = True


class ImportPreviewResponse(BaseModel):
    files_analyzed: int
    would_create: int
    would_stage: int
    would_link: int
    collisions: list[CollisionEntry]
    type_mappings: dict[str, str] = {}
    unrecognized_types: list[str] = []
    errors: list[str] = []


class ImportBatchResponse(BaseModel):
    id: str
    org_id: str
    source_vault: str
    base_path: str
    status: str
    file_count: int
    created_count: int
    staged_count: int
    linked_count: int
    skipped_count: int
    error_count: int
    created_by: str
    created_at: datetime
    rolled_back_at: datetime | None = None
    rolled_back_by: str | None = None


class ImportExecuteRequest(BaseModel):
    files: list[ImportFile]
    base_path: str = ""
    source_vault: str
    collisions: list[CollisionEntry] = []


class ImportExecuteResponse(BaseModel):
    created: int
    staged: int
    linked: int
    errors: list[str]
    type_mappings: dict[str, str] = {}
    unrecognized_types: list[str] = []
    batch_id: str
    collisions_resolved: int = 0


class RollbackResponse(BaseModel):
    batch_id: str
    entries_archived: int
    links_removed: int
    staging_removed: int


# =============================================================================
# Invitation models
# =============================================================================


class InviteCreate(BaseModel):
    default_role: str = "viewer"
    email_hint: str | None = None


class InviteResponse(BaseModel):
    id: str
    org_id: str
    invite_code: str
    token: str | None = None  # only set on creation (shown once)
    default_role: str
    email_hint: str | None
    status: str
    invited_by: str | None
    expires_at: datetime
    created_at: datetime


class InviteRedeem(BaseModel):
    invite_code: str
    token: str
    email: str
    display_name: str
    password: str


class InviteRedeemResponse(BaseModel):
    user_id: str
    api_key: str  # shown once
    email: str
    display_name: str
    role: str
    org_id: str


# =============================================================================
# Auth models
# =============================================================================


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    api_key: str
    user: "UserResponse"


class UserResponse(BaseModel):
    id: str
    org_id: str
    display_name: str
    email: str | None
    role: str
    department: str | None
    is_active: bool


class UserRoleUpdate(BaseModel):
    role: str


# =============================================================================
# Permission models (entry-level and path-level ACL)
# =============================================================================


class PermissionGrant(BaseModel):
    """Grant a permission on an entry to a principal (user or group).

    Accepts either ``principal_id`` (preferred) or the legacy ``user_id`` field
    for one release of back-compat. ``principal_type`` defaults to ``'user'``
    so existing callers that omit it continue to work unchanged.
    """

    model_config = ConfigDict(populate_by_name=True)

    principal_type: Literal["user", "group"] = "user"
    principal_id: str = Field(..., alias="user_id")
    role: str

    @model_validator(mode="before")
    @classmethod
    def _accept_either_principal_or_user_id(cls, data):
        if isinstance(data, dict):
            if "principal_id" not in data and "user_id" in data:
                data = {**data, "principal_id": data["user_id"]}
        return data


class PathPermissionGrant(BaseModel):
    """Grant a path-pattern permission to a principal (user or group)."""

    model_config = ConfigDict(populate_by_name=True)

    path_pattern: str
    principal_type: Literal["user", "group"] = "user"
    principal_id: str = Field(..., alias="user_id")
    role: str

    @model_validator(mode="before")
    @classmethod
    def _accept_either_principal_or_user_id(cls, data):
        if isinstance(data, dict):
            if "principal_id" not in data and "user_id" in data:
                data = {**data, "principal_id": data["user_id"]}
        return data


class EntryPermissionResponse(BaseModel):
    id: str
    entry_id: str
    principal_type: str
    principal_id: str
    role: str
    granted_by: str
    created_at: datetime


class PathPermissionResponse(BaseModel):
    id: str
    path_pattern: str
    principal_type: str
    principal_id: str
    role: str
    granted_by: str
    created_at: datetime


# =============================================================================
# Group models (permissions v2 — P1)
# =============================================================================


class GroupCreate(BaseModel):
    name: str
    description: str | None = None


class GroupResponse(BaseModel):
    id: str
    org_id: str
    name: str
    description: str | None
    created_by: str
    created_at: datetime
    member_count: int | None = None


class GroupMemberGrant(BaseModel):
    user_id: str


class GroupMemberResponse(BaseModel):
    group_id: str
    user_id: str
    org_id: str
    added_by: str
    added_at: datetime


class GroupDetailResponse(BaseModel):
    id: str
    org_id: str
    name: str
    description: str | None
    created_by: str
    created_at: datetime
    members: list[GroupMemberResponse] | None = None  # None when caller is non-member


# =============================================================================
# Comment models (spec 0026 — first-class comments subsystem)
# =============================================================================


VALID_COMMENT_STATUSES = {"open", "resolved", "escalated", "dismissed"}
VALID_COMMENT_UPDATE_STATUSES = {"resolved", "dismissed", "escalated"}


class CommentCreate(BaseModel):
    body: str
    parent_comment_id: str | None = None


class CommentUpdate(BaseModel):
    status: str  # resolved | dismissed | escalated
    escalated_to: str | None = None  # user_id; required when status == 'escalated'


class CommentResponse(BaseModel):
    id: str
    org_id: str
    entry_id: str
    author_id: str
    author_kind: str  # user | agent
    body: str
    status: str
    escalated_to: str | None
    parent_comment_id: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
