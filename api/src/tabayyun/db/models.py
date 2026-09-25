"""SQLAlchemy 2 declarative models for the persistence schema (spec 001).

Conventions:
- Primary keys are UUIDs generated in Python; timestamps are `timestamptz`.
- Every tenant table carries `org_id` (tables that belong to one workspace also
  `workspace_id`); spec 007 attaches one row-level security policy per table, keyed by
  `org_id` (ADR-0007). `users` is the one table without it: a user spans orgs.
- Enumerated columns are text with named CHECK constraints rather than Postgres enum types,
  so a later spec extends the values with a plain migration.
- `findings`, `metrics` and `scores` become TimescaleDB hypertables when available; their
  primary keys therefore include the time column.
- No column stores a secret: `sources.credentials_ref` names a key in the environment or a
  secret store, never the value.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tabayyun.db import Base

# Fixed identifiers of the seed tenant and of the bootstrap user who owns it (spec 007);
# requests act as that user until login arrives (spec 013).
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
BOOTSTRAP_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

SOURCE_TYPES = ("upload", "csv_dir", "pi_web_api", "opc_ua")
SERIES_KINDS = ("measurement", "counter", "setpoint", "status")
RUN_TRIGGERS = ("upload", "suite", "manual")
RUN_STATUSES = ("queued", "running", "succeeded", "failed")
FINDING_STATUSES = ("open", "acked", "muted", "resolved")
GROUP_KINDS = ("related", "redundant", "balance")
MEMBER_ROLES = ("member", "input", "output")
ORG_ROLES = ("owner", "admin", "member")
WORKSPACE_ROLES = ("admin", "editor", "viewer")


def _in(column: str, values: tuple[str, ...]) -> str:
    """SQL for a CHECK constraint restricting `column` to `values`."""
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _uuid_pk() -> Mapped[uuid.UUID]:
    """UUID primary key generated in Python."""
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    """`timestamptz` column defaulting to the database clock."""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrgMixin:
    """`org_id` on every tenant table: the key of its row-level security policy (spec 007)."""

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False)


class TenantMixin(OrgMixin):
    """`org_id` and `workspace_id` on every table that belongs to one workspace."""

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    """A person; spans orgs, so no `org_id` and no RLS. Logins arrive with spec 013."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email"), CheckConstraint("email = lower(email)", name="email_lower"))

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrgMembership(Base):
    """A user's role in an org: owner and admin act as admin in every workspace (spec 007)."""

    __tablename__ = "org_memberships"
    __table_args__ = (CheckConstraint(_in("role", ORG_ROLES), name="role"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class WorkspaceMembership(OrgMixin, Base):
    """A user's direct role in one workspace."""

    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint(_in("role", WORKSPACE_ROLES), name="role"),
        Index("ix_workspace_memberships_user_id", "user_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Team(OrgMixin, Base):
    """A named set of users in one org; teams get roles in workspaces."""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class TeamMember(OrgMixin, Base):
    """A user in a team."""

    __tablename__ = "team_members"
    __table_args__ = (Index("ix_team_members_user_id", "user_id"),)

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _created_at()


class WorkspaceTeamRole(OrgMixin, Base):
    """A team's role in one workspace; every member of the team holds it."""

    __tablename__ = "workspace_team_roles"
    __table_args__ = (CheckConstraint(_in("role", WORKSPACE_ROLES), name="role"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)


class Source(TenantMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(_in("type", SOURCE_TYPES), name="type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    credentials_ref: Mapped[str | None] = mapped_column(Text)
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()


class Series(TenantMixin, Base):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint(_in("kind", SERIES_KINDS), name="kind"),
        Index("ix_series_workspace_id_name", "workspace_id", "name"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="measurement")
    expected_interval_ns: Mapped[int | None] = mapped_column(BigInteger)
    physical_min: Mapped[float | None] = mapped_column(Double)
    physical_max: Mapped[float | None] = mapped_column(Double)
    operational_min: Mapped[float | None] = mapped_column(Double)
    operational_max: Mapped[float | None] = mapped_column(Double)
    resolution: Mapped[float | None] = mapped_column(Double)
    non_negative: Mapped[bool | None] = mapped_column(Boolean)
    asset_path: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Dataset(TenantMixin, Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    window_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()


class DatasetSeries(OrgMixin, Base):
    __tablename__ = "dataset_series"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id"), primary_key=True
    )


class SeriesGroup(TenantMixin, Base):
    """Series that belong together and how (spec 008): related, redundant or a balance."""

    __tablename__ = "series_groups"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(_in("kind", GROUP_KINDS), name="kind"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SeriesGroupMember(OrgMixin, Base):
    """One member of a series group, in declaration order (`position`)."""

    __tablename__ = "series_group_members"
    __table_args__ = (
        CheckConstraint(_in("role", MEMBER_ROLES), name="role"),
        Index("ix_series_group_members_series_id", "series_id"),
    )

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series_groups.id", ondelete="CASCADE"), primary_key=True
    )
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class Run(TenantMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("trigger", RUN_TRIGGERS), name="trigger"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_runs_workspace_id_created_at", "workspace_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # A deleted dataset keeps its past runs (spec 008).
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="SET NULL")
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    now_ns: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Upload(OrgMixin, Base):
    __tablename__ = "uploads"

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"), primary_key=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()


class Finding(TenantMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (
        PrimaryKeyConstraint("id", "window_start"),
        CheckConstraint(_in("status", FINDING_STATUSES), name="status"),
        Index(
            "ix_findings_workspace_id_status_window_start",
            "workspace_id",
            "status",
            text("window_start DESC"),
        ),
        Index(
            "ix_findings_series_id_check_id_window_start", "series_id", "check_id", text("window_start DESC")
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("series.id"), nullable=False)
    check_id: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    score_impact: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    status_reason: Mapped[str | None] = mapped_column(Text)
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"))
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Metric(OrgMixin, Base):
    __tablename__ = "metrics"
    __table_args__ = (
        PrimaryKeyConstraint("series_id", "check_id", "name", "ts"),
        Index("ix_metrics_series_id_name_ts", "series_id", "name", text("ts DESC")),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("series.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    check_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)


class Score(OrgMixin, Base):
    __tablename__ = "scores"
    __table_args__ = (
        PrimaryKeyConstraint("series_id", "layer", "computed_at"),
        Index("ix_scores_series_id_computed_at", "series_id", text("computed_at DESC")),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("series.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False)
    layer: Mapped[str] = mapped_column(Text, nullable=False, server_default="raw")
    method_version: Mapped[str] = mapped_column(Text, nullable=False)
    overall: Mapped[float] = mapped_column(Double, nullable=False)
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    n_findings: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Coverage(OrgMixin, Base):
    __tablename__ = "coverage"
    __table_args__ = (PrimaryKeyConstraint("series_id", "layer", "range_start"),)

    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("series.id"), nullable=False)
    layer: Mapped[str] = mapped_column(Text, nullable=False, server_default="raw")
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
