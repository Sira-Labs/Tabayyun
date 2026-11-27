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
- No column stores a secret in the clear: connector credentials are AES-GCM ciphertext in
  `source_credentials` (spec 021), readable only through `tabayyun.secrets`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tabayyun.db import Base

# Fixed identifiers of the seed tenant and of the bootstrap user who owns it (spec 007);
# requests act as that user until login arrives (spec 013).
DEFAULT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
BOOTSTRAP_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

SOURCE_TYPES = ("upload", "csv_dir", "pi_web_api", "opc_ua", "synthetic")
SERIES_KINDS = ("measurement", "counter", "setpoint", "status")
RUN_TRIGGERS = ("upload", "suite", "manual")
RUN_STATUSES = ("queued", "running", "succeeded", "failed")
FINDING_STATUSES = ("open", "acked", "muted", "resolved")
GROUP_KINDS = ("related", "redundant", "balance")
BASELINE_TRIGGERS = ("schedule", "manual")
FINDING_FEEDBACK = ("false_positive",)
MEMBER_ROLES = ("member", "input", "output")
ORG_ROLES = ("owner", "admin", "member")
WORKSPACE_ROLES = ("admin", "editor", "viewer")
SIGN_IN_METHODS = ("google", "github", "passkey")
INVITABLE_ORG_ROLES = ("admin", "member")
EMAIL_STATUSES = ("not_configured", "queued", "sent", "failed")
FETCH_TRIGGERS = ("manual", "poll", "run", "check", "search", "metadata", "ingest")
FILE_STATUSES = ("ingested", "failed")
FETCH_STATUSES = ("queued", "running", "succeeded", "partial", "failed")


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


class UserIdentity(Base):
    """A login at the identity provider (issuer, subject) and the user it belongs to (spec 013).

    Read-only to the app login: `tabayyun_login()` (migration 0005) is the only writer.
    """

    __tablename__ = "user_identities"
    __table_args__ = (Index("ix_user_identities_user_id", "user_id"),)

    issuer: Mapped[str] = mapped_column(Text, primary_key=True)
    subject: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    email_at_login: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuthSession(Base):
    """A signed-in browser (spec 013); the cookie holds a token, the table only its HMAC.

    No row-level security: a request looks its session up before any org is known. `org_id`
    null means signed in without access yet.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(_in("sign_in_method", SIGN_IN_METHODS), name="sign_in_method"),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_idp_sid", "idp_sid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    id_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE")
    )
    sign_in_method: Mapped[str] = mapped_column(Text, nullable=False)
    idp_sid: Mapped[str | None] = mapped_column(Text)
    id_token: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginFlow(Base):
    """A login in progress between `/api/auth/login` and the callback (10 minutes, single use)."""

    __tablename__ = "login_flows"

    id_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    next_path: Mapped[str] = mapped_column("next", Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


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
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Series(TenantMixin, Base):
    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        CheckConstraint(_in("kind", SERIES_KINDS), name="kind"),
        Index("ix_series_workspace_id_name", "workspace_id", "name"),
        # The baseline scheduler takes the longest-waiting series first (spec 028).
        Index("ix_series_baseline_checked_at", text("baseline_checked_at NULLS FIRST")),
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
    # When the baseline scheduler last queued the series (spec 028); not a change of the series.
    baseline_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SeriesBaseline(TenantMixin, Base):
    """One version of a series' baseline profile (spec 028)."""

    __tablename__ = "series_baselines"
    __table_args__ = (
        UniqueConstraint("series_id", "version"),
        CheckConstraint(_in("trigger", BASELINE_TRIGGERS), name="trigger"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    # What was left out: `{"bad": n, "findings": n, "windows": [[start_ns, end_ns], ...]}`.
    excluded: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


CORRECTION_STATUSES = ("proposed", "computed", "failed", "approved", "rejected")


class Correction(TenantMixin, Base):
    """One proposed repair of a window of a series and its corrected-layer version (spec 035)."""

    __tablename__ = "corrections"
    __table_args__ = (
        UniqueConstraint("series_id", "version"),
        CheckConstraint(_in("status", CORRECTION_STATUSES), name="status"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("window_end_ns > window_start_ns", name="window"),
        # A series' corrections, newest version first.
        Index("ix_corrections_series_id_version", "series_id", text("version DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False
    )
    # Allocated when proposed; the cache layer of its rows is `corrected/v{version}`.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    method: Mapped[str] = mapped_column(Text, nullable=False)
    # The operation's parameters without `method`; `*_ns` values are integers.
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    window_start_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_end_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # The finding that motivated it; no foreign key, `findings` is a hypertable.
    finding_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Highest approved version when it was computed; null is the raw layer.
    base_version: Mapped[int | None] = mapped_column(Integer)
    # `{rows, changed, added, removed, unrepaired}` once computed.
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    proposed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CorrectedRange(TenantMixin, Base):
    """A run of samples a correction changed, added or removed (spec 035). The values before
    are the base layer's, the values after the version's in the cache."""

    __tablename__ = "corrected_ranges"
    __table_args__ = (
        Index("ix_corrected_ranges_correction_id_range_start_ns", "correction_id", "range_start_ns"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    correction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("corrections.id", ondelete="CASCADE"), nullable=False
    )
    range_start_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    range_end_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    removed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Dataset(TenantMixin, Base):
    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    window_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()


class CheckSuite(TenantMixin, Base):
    """A dataset, the checks to run on it and optionally a cron schedule (spec 027)."""

    __tablename__ = "check_suites"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        # The scheduler's claim: enabled suites by the time they are due.
        Index("ix_check_suites_next_run_at", "next_run_at", postgresql_where=text("enabled")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # A deleted dataset leaves the suite, which then waits for a new one.
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="SET NULL")
    )
    # Five-field cron in UTC; null means the suite only runs on request.
    schedule: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # `[{id, enabled, params}]` in the order given.
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


ALERT_KINDS = ("finding", "score")
ALERT_CHANNELS = ("email", "webhook")
NOTIFICATION_KINDS = ("alert", "test")
NOTIFICATION_STATUSES = ("pending", "sending", "sent", "failed", "skipped")


class AlertRule(TenantMixin, Base):
    """A condition over a run's findings or scores and where to tell about it (spec 031)."""

    __tablename__ = "alert_rules"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint(_in("kind", ALERT_KINDS), name="kind"),
        CheckConstraint(_in("channel", ALERT_CHANNELS), name="channel"),
        CheckConstraint("throttle_minutes BETWEEN 0 AND 1440", name="throttle_minutes"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    # Email: the addresses; webhook: empty.
    recipients: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    url: Mapped[str | None] = mapped_column(Text)
    # The webhook's signing secret: AES-GCM ciphertext bound to the rule (`tabayyun.secrets`).
    secret_key_id: Mapped[str | None] = mapped_column(Text)
    secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    throttle_minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AlertNotification(TenantMixin, Base):
    """One message of a rule, gathered from the matches of one or more runs (spec 031)."""

    __tablename__ = "alert_notifications"
    __table_args__ = (
        CheckConstraint(_in("kind", NOTIFICATION_KINDS), name="kind"),
        CheckConstraint(_in("status", NOTIFICATION_STATUSES), name="status"),
        # A rule's history, newest first, and its newest notification for the throttle.
        Index("ix_alert_notifications_rule_id_created_at", "rule_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="alert")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    n_matches: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The first matches as they are told; `n_matches` counts all.
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        # A suite's run history and its newest run (spec 027); most runs have no suite.
        Index(
            "ix_runs_suite_id_created_at",
            "suite_id",
            text("created_at DESC"),
            postgresql_where=text("suite_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # A deleted dataset keeps its past runs (spec 008).
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="SET NULL")
    )
    # The suite that started the run and its checks as they were then (spec 027); a deleted
    # suite keeps its past runs.
    suite_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("check_suites.id", ondelete="SET NULL")
    )
    checks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    now_ns: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    # Set by the one evaluation of the alert rules on this run (spec 031).
    alerts_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint(_in("feedback", FINDING_FEEDBACK), name="feedback"),
        # The task that opens expired mutes looks only at mutes with an end (spec 029).
        Index(
            "ix_findings_muted_until",
            "muted_until",
            postgresql_where=text("status = 'muted' AND muted_until IS NOT NULL"),
        ),
        Index(
            "ix_findings_workspace_id_status_window_start",
            "workspace_id",
            "status",
            text("window_start DESC"),
        ),
        # New findings per day on the overview (spec 032).
        Index("ix_findings_workspace_id_created_at", "workspace_id", "created_at"),
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
    # Triage (spec 029): when a mute ends (null: no end), who works on it, and the user's verdict.
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    feedback: Mapped[str | None] = mapped_column(Text)
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SavedFindingFilter(TenantMixin, Base):
    """A user's saved filter of the findings page (spec 029): personal, per workspace."""

    __tablename__ = "finding_filters"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # The page's URL search string.
    query: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


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


# The dimensions a score can hold, in the order the overview shows them (spec 032).
SCORE_DIMENSIONS = (
    "completeness",
    "timeliness",
    "validity",
    "accuracy",
    "consistency",
    "plausibility",
    "integrity",
)


class ScoreDay(TenantMixin, Base):
    """The last score of a series on one UTC day (spec 032): what the overview reads, so it
    never scans `scores`, which holds a row per run."""

    __tablename__ = "score_days"
    __table_args__ = (
        PrimaryKeyConstraint("series_id", "layer", "day"),
        Index("ix_score_days_workspace_id_layer_day", "workspace_id", "layer", "day"),
    )

    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("series.id", ondelete="CASCADE"), nullable=False
    )
    layer: Mapped[str] = mapped_column(Text, nullable=False, server_default="raw")
    day: Mapped[date] = mapped_column(Date, nullable=False)
    overall: Mapped[float] = mapped_column(Double, nullable=False)
    completeness: Mapped[float | None] = mapped_column(Double)
    timeliness: Mapped[float | None] = mapped_column(Double)
    validity: Mapped[float | None] = mapped_column(Double)
    accuracy: Mapped[float | None] = mapped_column(Double)
    consistency: Mapped[float | None] = mapped_column(Double)
    plausibility: Mapped[float | None] = mapped_column(Double)
    integrity: Mapped[float | None] = mapped_column(Double)
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


class Invitation(OrgMixin, Base):
    """An invitation bound to an email (spec 014): accepted when that verified email signs in.

    At most one is open per org and email; an expired one is revoked when a new one is made.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        CheckConstraint(_in("org_role", INVITABLE_ORG_ROLES), name="org_role"),
        CheckConstraint(
            f"workspace_role IS NULL OR {_in('workspace_role', WORKSPACE_ROLES)}", name="workspace_role"
        ),
        CheckConstraint("(workspace_id IS NULL) = (workspace_role IS NULL)", name="workspace_grant"),
        CheckConstraint(_in("email_status", EMAIL_STATUSES), name="email_status"),
        CheckConstraint("email = lower(email)", name="email_lower"),
        Index(
            "uq_invitations_open_email",
            "org_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
        Index("ix_invitations_email", "email"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    org_role: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    workspace_role: Mapped[str | None] = mapped_column(Text)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_configured")
    email_error: Mapped[str | None] = mapped_column(Text)


class AuditEvent(OrgMixin, Base):
    """One authz-relevant change (spec 014). Append-only: the app login may only insert and read.

    `workspace_id` has no foreign key, so events outlive a deleted workspace; `actor_user_id`
    null means the system (an invitation accepted at sign-in).
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_org_created", "org_id", text("created_at DESC"), text("id DESC")),
        Index("ix_audit_events_workspace_created", "org_id", "workspace_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class SourceCredentials(OrgMixin, Base):
    """A connector source's credentials (spec 021): AES-256-GCM ciphertext bound to the source by
    its associated data; written and read only through `tabayyun.secrets`."""

    __tablename__ = "source_credentials"

    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SourceFetch(TenantMixin, Base):
    """One job of a connector source: a fetch (requested, polled, inside a run), a check (spec
    021), a point search or a metadata import (spec 022)."""

    __tablename__ = "source_fetches"
    __table_args__ = (
        CheckConstraint(_in("trigger", FETCH_TRIGGERS), name="trigger"),
        CheckConstraint(_in("status", FETCH_STATUSES), name="status"),
        Index("ix_source_fetches_source_created", "source_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    series_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    force: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceFile(TenantMixin, Base):
    """One file of a `csv_dir` source (spec 026): what reading its current content gave.

    One row per path; a changed file updates it. Rows outlive the scans that read them.
    """

    __tablename__ = "source_files"
    __table_args__ = (
        UniqueConstraint("source_id", "path"),
        CheckConstraint(_in("status", FILE_STATUSES), name="status"),
        Index("ix_source_files_source_updated", "source_id", text("updated_at DESC"), text("id DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    n_series: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    n_new_series: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    versions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    fetch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("source_fetches.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RateLimit(Base):
    """One fixed window of one rate-limit bucket and key (spec 015). Not tenant data: no
    `org_id`, no RLS; read before any org is known, pruned after a day."""

    __tablename__ = "rate_limits"
    __table_args__ = (
        PrimaryKeyConstraint("bucket", "key", "window_start"),
        Index("ix_rate_limits_window_start", "window_start"),
    )

    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
