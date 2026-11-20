"""Live events (spec 030): the reaper tells who watches that it failed a run.

`tabayyun_reap_stale_runs(interval)` is replaced by a version that also publishes
`run.completed` on the `tabayyun_events` channel for every run it fails, in the shape of
`tabayyun.events.Event`. `CREATE OR REPLACE` keeps the function's owner and its grants (the
app role only, 0004). No table changes.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19 22:50:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The channel and the payload version of `tabayyun.events` at this revision.
REAPER_NOTIFYING = """
CREATE OR REPLACE FUNCTION tabayyun_reap_stale_runs(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    reaped integer;
BEGIN
    WITH failed AS (
        UPDATE runs SET status = 'failed', error = 'worker lost', finished_at = now()
         WHERE status = 'running' AND started_at < now() - older_than
        RETURNING id, org_id, workspace_id),
    told AS (
        SELECT pg_notify('tabayyun_events', json_build_object(
                   'v', 1, 'type', 'run.completed', 'org_id', org_id, 'workspace_id', workspace_id,
                   'at', now(),
                   'data', json_build_object('run_id', id, 'status', 'failed', 'n_findings_new', 0)
               )::text)
          FROM failed)
    SELECT count(*)::integer INTO reaped FROM told;
    DELETE FROM uploads u USING runs r
     WHERE u.run_id = r.id AND r.status = 'failed' AND r.error = 'worker lost';
    RETURN reaped;
END
$$;
"""

REAPER_SILENT = """
CREATE OR REPLACE FUNCTION tabayyun_reap_stale_runs(older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
    reaped integer;
BEGIN
    UPDATE runs SET status = 'failed', error = 'worker lost', finished_at = now()
     WHERE status = 'running' AND started_at < now() - older_than;
    GET DIAGNOSTICS reaped = ROW_COUNT;
    DELETE FROM uploads u USING runs r
     WHERE u.run_id = r.id AND r.status = 'failed' AND r.error = 'worker lost';
    RETURN reaped;
END
$$;
"""


def upgrade() -> None:
    """Replace the reaper with the version that publishes `run.completed`."""
    op.execute(REAPER_NOTIFYING)


def downgrade() -> None:
    """Put the reaper of 0004 back: it fails lost runs without telling anyone."""
    op.execute(REAPER_SILENT)
