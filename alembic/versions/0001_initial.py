"""initial schema: events table

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("vendor", sa.String(length=64), nullable=False),
        sa.Column("device_name", sa.String(length=128), nullable=False),
        sa.Column(
            "device_version",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("platform", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "max_version",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "kind",
            sa.Enum("event", "error", "metric", "crash", name="event_kind"),
            nullable=False,
        ),
        sa.Column(
            "level",
            sa.Enum("info", "warning", "error", "fatal", name="event_level"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ts_ms", sa.BigInteger(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column(
            "props", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )

    op.create_index("ix_events_received_at", "events", ["received_at"])
    op.create_index("ix_events_vendor", "events", ["vendor"])
    op.create_index("ix_events_device_name", "events", ["device_name"])
    op.create_index("ix_events_device_id", "events", ["device_id"])
    op.create_index("ix_events_session_id", "events", ["session_id"])
    op.create_index("ix_events_kind", "events", ["kind"])
    op.create_index("ix_events_level", "events", ["level"])
    op.create_index("ix_events_name", "events", ["name"])
    op.create_index(
        "ix_events_vendor_device_ts", "events", ["vendor", "device_name", "ts"]
    )
    op.create_index("ix_events_kind_level_ts", "events", ["kind", "level", "ts"])
    op.create_index(
        "ix_events_props_gin", "events", ["props"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_events_props_gin", table_name="events")
    op.drop_index("ix_events_kind_level_ts", table_name="events")
    op.drop_index("ix_events_vendor_device_ts", table_name="events")
    op.drop_index("ix_events_name", table_name="events")
    op.drop_index("ix_events_level", table_name="events")
    op.drop_index("ix_events_kind", table_name="events")
    op.drop_index("ix_events_session_id", table_name="events")
    op.drop_index("ix_events_device_id", table_name="events")
    op.drop_index("ix_events_device_name", table_name="events")
    op.drop_index("ix_events_vendor", table_name="events")
    op.drop_index("ix_events_received_at", table_name="events")
    op.drop_table("events")
    sa.Enum(name="event_level").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="event_kind").drop(op.get_bind(), checkfirst=False)
