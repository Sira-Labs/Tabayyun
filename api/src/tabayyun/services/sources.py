"""Connector sources (specs 021, 022): create and change them, their credentials, their series,
and requests for fetches, checks, point searches and metadata imports. Every change writes an
audit event in the same transaction.

Credentials are write-only: nothing here returns them, and validation errors are reduced to
the field names and messages, never the submitted values.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun import connectors, secrets
from tabayyun.authz.scope import Scope
from tabayyun.db.models import Series, Source, SourceFetch
from tabayyun.services import fetches
from tabayyun.services.admin import audit

MAX_WINDOW = timedelta(days=366)
MAX_SERIES_PER_CALL = 500
# The connector method each source job needs beyond the interface's required ones.
OPTIONAL_METHODS = {"search": "search", "metadata": "describe"}


class SourceError(Exception):
    """A request the sources API refuses: an HTTP status, a code and optional field errors."""

    def __init__(self, status: int, code: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.errors = errors


@dataclass(frozen=True)
class SourceView:
    """A source with what the detail route shows besides its columns."""

    source: Source
    n_series: int
    credentials_updated_at: datetime | None


def field_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Pydantic errors without the submitted values (they may be credentials)."""
    return [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]


def validate_config(source_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """The config as the connector's model normalises it.

    Raises:
        SourceError: 422 `not_a_connector`, or 422 `invalid_config` with the field errors.
    """
    cls = connectors.get(source_type)
    if cls is None:
        raise SourceError(422, "not_a_connector")
    try:
        model = cls.config_model.model_validate(config)
    except ValidationError as exc:
        raise SourceError(422, "invalid_config", field_errors(exc)) from None
    return model.model_dump(mode="json", exclude_unset=True)


async def get_source(session: AsyncSession, scope: Scope, source_id: uuid.UUID) -> Source:
    """The source in the scope's workspace.

    Raises:
        SourceError: 404 when it is not there.
    """
    source = await session.scalar(
        select(Source).where(Source.id == source_id, Source.workspace_id == scope.workspace_id)
    )
    if source is None:
        raise SourceError(404, "not found")
    return source


def require_connector(source: Source) -> type[connectors.Connector]:
    """The source's connector class.

    Raises:
        SourceError: 409 `not_a_connector` (an upload source).
    """
    cls = connectors.get(source.type)
    if cls is None:
        raise SourceError(409, "not_a_connector")
    return cls


async def view(session: AsyncSession, source: Source) -> SourceView:
    """The source with its series count and when its credentials were set."""
    n_series = await session.scalar(
        select(func.count()).select_from(Series).where(Series.source_id == source.id)
    )
    return SourceView(source, int(n_series or 0), await secrets.status(session, source.id))


async def create(
    session: AsyncSession,
    scope: Scope,
    actor: audit.Actor,
    *,
    source_type: str,
    name: str,
    config: dict[str, Any],
) -> Source:
    """A new connector source, enabled, with unknown health.

    Raises:
        SourceError: 422 for a non-connector type or an invalid config, 409 `name_taken`.
    """
    normalised = validate_config(source_type, config)
    source = Source(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        type=source_type,
        name=name.strip(),
        config=normalised,
        health={"status": "unknown"},
    )
    session.add(source)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise SourceError(409, "name_taken") from None
    await audit.record(
        session,
        actor,
        "source.created",
        "source",
        source.id,
        workspace_id=scope.workspace_id,
        details={"type": source_type, "name": source.name},
    )
    return source


async def update(
    session: AsyncSession,
    scope: Scope,
    actor: audit.Actor,
    source_id: uuid.UUID,
    *,
    name: str | None = None,
    config: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> Source:
    """Change a connector source's name, whole config or enabled flag.

    Raises:
        SourceError: 404, 409 `not_a_connector`, 409 `name_taken`, 422 `invalid_config`.
    """
    source = await get_source(session, scope, source_id)
    require_connector(source)
    changed: list[str] = []
    if name is not None and name.strip() != source.name:
        source.name = name.strip()
        changed.append("name")
    if config is not None:
        normalised = validate_config(source.type, config)
        if normalised != source.config:
            source.config = normalised
            changed.append("config")
    if enabled is not None and enabled != source.enabled:
        source.enabled = enabled
        changed.append("enabled")
    if not changed:
        return source
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        raise SourceError(409, "name_taken") from None
    await session.refresh(source)  # `updated_at` is set by the database
    await audit.record(
        session,
        actor,
        "source.updated",
        "source",
        source.id,
        workspace_id=scope.workspace_id,
        details={"changed": changed, **({"enabled": source.enabled} if "enabled" in changed else {})},
    )
    return source


async def set_credentials(
    session: AsyncSession,
    scope: Scope,
    actor: audit.Actor,
    source_id: uuid.UUID,
    payload: dict[str, Any],
    keyring: secrets.Keyring | None,
) -> None:
    """Validate and store a source's credentials, encrypted.

    Raises:
        SourceError: 404, 409 `not_a_connector`, 422 `no_credentials` (the connector takes
            none) or `invalid_credentials` (field errors only), 503 `credentials_unavailable`.
    """
    source = await get_source(session, scope, source_id)
    cls = require_connector(source)
    if cls.credentials_model is None:
        raise SourceError(422, "no_credentials")
    try:
        model: BaseModel = cls.credentials_model.model_validate(payload)
    except ValidationError as exc:
        raise SourceError(422, "invalid_credentials", field_errors(exc)) from None
    try:
        await secrets.store(
            session,
            keyring,
            org_id=scope.org_id,
            source_id=source.id,
            payload=model.model_dump(mode="json"),
            user_id=actor.user_id,
        )
    except secrets.CredentialsUnavailableError:
        raise SourceError(503, "credentials_unavailable") from None
    await audit.record(
        session, actor, "source.credentials_set", "source", source.id, workspace_id=scope.workspace_id
    )


async def clear_credentials(
    session: AsyncSession, scope: Scope, actor: audit.Actor, source_id: uuid.UUID
) -> None:
    """Delete a source's credentials (audited only when there were some).

    Raises:
        SourceError: 404, 409 `not_a_connector`.
    """
    source = await get_source(session, scope, source_id)
    require_connector(source)
    if await secrets.clear(session, source.id):
        await audit.record(
            session, actor, "source.credentials_cleared", "source", source.id, workspace_id=scope.workspace_id
        )


async def register_series(
    session: AsyncSession, scope: Scope, actor: audit.Actor, source_id: uuid.UUID, items: list[dict[str, Any]]
) -> tuple[int, int]:
    """Make each point a series of the source; (created, already there). Audited with the counts.

    Raises:
        SourceError: 404, 409 `not_a_connector`.
    """
    source = await get_source(session, scope, source_id)
    require_connector(source)
    created = 0
    for item in items:
        stmt = (
            insert(Series)
            .values(
                id=uuid.uuid4(),
                org_id=scope.org_id,
                workspace_id=scope.workspace_id,
                source_id=source.id,
                **item,
            )
            .on_conflict_do_nothing(index_elements=[Series.source_id, Series.external_id])
            .returning(Series.id)
        )
        if (await session.execute(stmt)).first() is not None:
            created += 1
    existing = len(items) - created
    await audit.record(
        session,
        actor,
        "source.series_registered",
        "source",
        source.id,
        workspace_id=scope.workspace_id,
        details={"created": created, "existing": existing},
    )
    return created, existing


async def request_fetch(
    session: AsyncSession,
    scope: Scope,
    actor: audit.Actor,
    source_id: uuid.UUID,
    *,
    trigger: str,
    start: datetime,
    end: datetime,
    series_ids: list[uuid.UUID] | None = None,
    force: bool = False,
    params: dict[str, Any] | None = None,
) -> SourceFetch:
    """A queued job of a source (a fetch, check, search or metadata import); the caller defers
    its job or runs it inline.

    Raises:
        SourceError: 404, 409 `not_a_connector`, `not_supported` or `source_disabled` (fetches
            only), 422 `invalid_window` or `unknown_series`.
    """
    source = await get_source(session, scope, source_id)
    cls = require_connector(source)
    method = OPTIONAL_METHODS.get(trigger)
    if method is not None and not cls.supports(method):
        raise SourceError(409, "not_supported")
    if trigger in fetches.DATA_TRIGGERS:
        if not source.enabled:
            raise SourceError(409, "source_disabled")
        if end <= start or end - start > MAX_WINDOW:
            raise SourceError(422, "invalid_window")
    if series_ids is not None:
        known = set(
            (
                await session.scalars(
                    select(Series.id).where(Series.source_id == source.id, Series.id.in_(series_ids))
                )
            ).all()
        )
        if known != set(series_ids):
            raise SourceError(422, "unknown_series")
    fetch = await fetches.create_fetch(
        session,
        source,
        trigger=trigger,
        start=start,
        end=end,
        series_ids=series_ids,
        force=force,
        user_id=actor.user_id,
        params=params,
    )
    details: dict[str, Any] = {"fetch_id": str(fetch.id), "trigger": trigger}
    if trigger in fetches.DATA_TRIGGERS:
        details.update(start=start.isoformat(), end=end.isoformat(), force=force)
    if trigger != "check":
        details["series"] = None if series_ids is None else len(series_ids)
    if params:
        details.update(params)
    await audit.record(
        session,
        actor,
        "source.fetch_requested",
        "source",
        source.id,
        workspace_id=scope.workspace_id,
        details=details,
    )
    return fetch


async def get_fetch(
    session: AsyncSession, scope: Scope, source_id: uuid.UUID, fetch_id: uuid.UUID
) -> SourceFetch:
    """One job of the source.

    Raises:
        SourceError: 404 when the source or the job is not there.
    """
    source = await get_source(session, scope, source_id)
    fetch = await session.scalar(
        select(SourceFetch).where(SourceFetch.id == fetch_id, SourceFetch.source_id == source.id)
    )
    if fetch is None:
        raise SourceError(404, "not found")
    return fetch


async def list_fetches(
    session: AsyncSession, scope: Scope, source_id: uuid.UUID, limit: int
) -> list[SourceFetch]:
    """The source's latest fetches, newest first.

    Raises:
        SourceError: 404.
    """
    source = await get_source(session, scope, source_id)
    stmt = (
        select(SourceFetch)
        .where(SourceFetch.source_id == source.id)
        .order_by(SourceFetch.created_at.desc(), SourceFetch.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())
