"""Initial schema (spec 001): tenants, sources, series, datasets, runs, uploads, findings,
metrics, scores and cache coverage, plus the seed org and workspace.

TimescaleDB handling follows TABAYYUN_TIMESCALE (ADR-0003): `auto` converts findings, metrics
and scores into hypertables when the extension is available and continues with a warning
otherwise; `on` fails without it; `off` never touches it. The default indexes Timescale would
add on the time column are disabled so that the schema is identical in every mode and
`alembic check` stays clean.

Revision ID: 0001
Revises:
Create Date: 2026-09-22 07:05:06+00:00
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

log = logging.getLogger("alembic.runtime.migration")

HYPERTABLES = {"findings": "window_start", "metrics": "ts", "scores": "computed_at"}
CHUNK_INTERVAL = "30 days"
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-000000000002"

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create every table, index and constraint, seed the tenant, then the hypertables."""
    op.create_table(
        "orgs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orgs")),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), server_default="UTC", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_workspaces_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("org_id", "name", name=op.f("uq_workspaces_org_id_name")),
    )
    op.create_table(
        "datasets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("selection", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column(
            "window_policy", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_datasets_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_datasets_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("credentials_ref", sa.Text(), nullable=True),
        sa.Column("health", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "type IN ('upload', 'csv_dir', 'pi_web_api', 'opc_ua')", name=op.f("ck_sources_type")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_sources_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_sources_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("workspace_id", "name", name=op.f("uq_sources_workspace_id_name")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("dataset_id", sa.UUID(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("now_ns", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')", name=op.f("ck_runs_status")
        ),
        sa.CheckConstraint("trigger IN ('upload', 'suite', 'manual')", name=op.f("ck_runs_trigger")),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name=op.f("fk_runs_dataset_id_datasets")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_runs_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_runs_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    op.create_index(
        "ix_runs_workspace_id_created_at",
        "runs",
        ["workspace_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "series",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), server_default="measurement", nullable=False),
        sa.Column("expected_interval_ns", sa.BigInteger(), nullable=True),
        sa.Column("physical_min", sa.Double(), nullable=True),
        sa.Column("physical_max", sa.Double(), nullable=True),
        sa.Column("operational_min", sa.Double(), nullable=True),
        sa.Column("operational_max", sa.Double(), nullable=True),
        sa.Column("resolution", sa.Double(), nullable=True),
        sa.Column("non_negative", sa.Boolean(), nullable=True),
        sa.Column("asset_path", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('measurement', 'counter', 'setpoint', 'status')", name=op.f("ck_series_kind")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_series_org_id_orgs")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_series_source_id_sources")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_series_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series")),
        sa.UniqueConstraint("source_id", "external_id", name=op.f("uq_series_source_id_external_id")),
    )
    op.create_index("ix_series_workspace_id_name", "series", ["workspace_id", "name"], unique=False)
    op.create_table(
        "coverage",
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("layer", sa.Text(), server_default="raw", nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name=op.f("fk_coverage_series_id_series")),
        sa.PrimaryKeyConstraint("series_id", "layer", "range_start", name=op.f("pk_coverage")),
    )
    op.create_table(
        "dataset_series",
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"], ["datasets.id"], name=op.f("fk_dataset_series_dataset_id_datasets")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name=op.f("fk_dataset_series_series_id_series")
        ),
        sa.PrimaryKeyConstraint("dataset_id", "series_id", name=op.f("pk_dataset_series")),
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("check_id", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score_impact", sa.Double(), server_default="0", nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("status_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_run_id", sa.UUID(), nullable=True),
        sa.Column("last_run_id", sa.UUID(), nullable=True),
        sa.Column("occurrences", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'acked', 'muted', 'resolved')", name=op.f("ck_findings_status")
        ),
        sa.ForeignKeyConstraint(["first_run_id"], ["runs.id"], name=op.f("fk_findings_first_run_id_runs")),
        sa.ForeignKeyConstraint(["last_run_id"], ["runs.id"], name=op.f("fk_findings_last_run_id_runs")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_findings_org_id_orgs")),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name=op.f("fk_findings_series_id_series")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_findings_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", "window_start", name=op.f("pk_findings")),
    )
    op.create_index(
        "ix_findings_series_id_check_id_window_start",
        "findings",
        ["series_id", "check_id", sa.text("window_start DESC")],
        unique=False,
    )
    op.create_index(
        "ix_findings_workspace_id_status_window_start",
        "findings",
        ["workspace_id", "status", sa.text("window_start DESC")],
        unique=False,
    )
    op.create_table(
        "metrics",
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("check_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_metrics_run_id_runs")),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name=op.f("fk_metrics_series_id_series")),
        sa.PrimaryKeyConstraint("series_id", "check_id", "name", "ts", name=op.f("pk_metrics")),
    )
    op.create_index(
        "ix_metrics_series_id_name_ts",
        "metrics",
        ["series_id", "name", sa.text("ts DESC")],
        unique=False,
    )
    op.create_table(
        "scores",
        sa.Column("series_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("layer", sa.Text(), server_default="raw", nullable=False),
        sa.Column("method_version", sa.Text(), nullable=False),
        sa.Column("overall", sa.Double(), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("n_findings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_scores_run_id_runs")),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"], name=op.f("fk_scores_series_id_series")),
        sa.PrimaryKeyConstraint("series_id", "layer", "computed_at", name=op.f("pk_scores")),
    )
    op.create_index(
        "ix_scores_series_id_computed_at",
        "scores",
        ["series_id", sa.text("computed_at DESC")],
        unique=False,
    )
    op.create_table(
        "uploads",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_uploads_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_uploads")),
    )

    _seed_default_tenant()
    _create_hypertables(_timescale_mode())


def _timescale_mode() -> str:
    """`auto`, `on` or `off`: the test suite passes it through the Alembic config, the CLI and
    the container read it from settings."""
    mode = context.config.attributes.get("timescale")
    if mode is None:
        from tabayyun.settings import Settings

        mode = Settings().timescale
    if mode not in {"auto", "on", "off"}:
        raise ValueError(f"TABAYYUN_TIMESCALE must be auto, on or off, not {mode!r}")
    return str(mode)


def _seed_default_tenant() -> None:
    """One org and one workspace with fixed ids, the tenant until spec 007 adds memberships."""
    op.execute(
        sa.text("INSERT INTO orgs (id, name) VALUES (:id, 'default')").bindparams(
            sa.bindparam("id", DEFAULT_ORG_ID, type_=sa.UUID())
        )
    )
    op.execute(
        sa.text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :org_id, 'default')").bindparams(
            sa.bindparam("id", DEFAULT_WORKSPACE_ID, type_=sa.UUID()),
            sa.bindparam("org_id", DEFAULT_ORG_ID, type_=sa.UUID()),
        )
    )


def _create_hypertables(mode: str) -> None:
    """Convert findings, metrics and scores per TABAYYUN_TIMESCALE; see the module docstring."""
    if mode == "off":
        log.info("TABAYYUN_TIMESCALE=off: hypertables not created")
        return
    conn = op.get_bind()
    available = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")
    ).scalar()
    if not available:
        if mode == "on":
            raise RuntimeError("TABAYYUN_TIMESCALE=on but the timescaledb extension is not available")
        log.warning("timescaledb extension not available: findings, metrics and scores stay plain tables")
        return
    # CREATE EXTENSION terminates the connection when the library is not preloaded, which a
    # transaction cannot recover from; check shared_preload_libraries first.
    preloaded = conn.execute(sa.text("SHOW shared_preload_libraries")).scalar() or ""
    if "timescaledb" not in {name.strip() for name in str(preloaded).split(",")}:
        if mode == "on":
            raise RuntimeError("TABAYYUN_TIMESCALE=on but timescaledb is not in shared_preload_libraries")
        log.warning("timescaledb is not preloaded: findings, metrics and scores stay plain tables")
        return
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    for table, time_column in HYPERTABLES.items():
        # CAST(:chunk AS interval), not :chunk::interval: text() does not recognise a bind that
        # is directly followed by the :: cast operator.
        conn.execute(
            sa.text(
                "SELECT create_hypertable(:table, :column, chunk_time_interval => CAST(:chunk AS interval), "
                "create_default_indexes => false, if_not_exists => true)"
            ).bindparams(table=table, column=time_column, chunk=CHUNK_INTERVAL)
        )
    log.info("hypertables created: %s", ", ".join(HYPERTABLES))


def downgrade() -> None:
    """Drop everything in dependency order; hypertables drop with their tables."""
    op.drop_table("uploads")
    op.drop_index("ix_scores_series_id_computed_at", table_name="scores")
    op.drop_table("scores")
    op.drop_index("ix_metrics_series_id_name_ts", table_name="metrics")
    op.drop_table("metrics")
    op.drop_index(
        "ix_findings_workspace_id_status_window_start",
        table_name="findings",
    )
    op.drop_index(
        "ix_findings_series_id_check_id_window_start",
        table_name="findings",
    )
    op.drop_table("findings")
    op.drop_table("dataset_series")
    op.drop_table("coverage")
    op.drop_index("ix_series_workspace_id_name", table_name="series")
    op.drop_table("series")
    op.drop_index("ix_runs_workspace_id_created_at", table_name="runs")
    op.drop_table("runs")
    op.drop_table("sources")
    op.drop_table("datasets")
    op.drop_table("workspaces")
    op.drop_table("orgs")
