"""모바일 푸시 토큰과 짐 챙기기 알림 발송 이력."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_device",
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(user_id) <> ''", name=op.f("ck_push_device_user_not_blank")),
        sa.CheckConstraint("platform = 'ANDROID'", name=op.f("ck_push_device_platform_valid")),
        sa.CheckConstraint(
            "(active AND token IS NOT NULL AND token_hash IS NOT NULL) OR "
            "(NOT active AND token IS NULL AND token_hash IS NULL)",
            name=op.f("ck_push_device_token_state_valid"),
        ),
        sa.PrimaryKeyConstraint("device_id", name=op.f("pk_push_device")),
        sa.UniqueConstraint("token_hash", name="uq_push_device_token_hash"),
    )
    op.create_index("ix_push_device_user_active", "push_device", ["user_id", "active"])
    op.create_table(
        "cargo_reminder",
        sa.Column(
            "notification_id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(user_id) <> ''", name=op.f("ck_cargo_reminder_user_not_blank")),
        sa.CheckConstraint(
            "event_type IN ('DESTINATION_ARRIVED', 'DOOR_OPENED')",
            name=op.f("ck_cargo_reminder_event_type_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plan.plan_id"],
            ondelete="CASCADE",
            name=op.f("fk_cargo_reminder_plan_id_plan"),
        ),
        sa.PrimaryKeyConstraint("notification_id", name=op.f("pk_cargo_reminder")),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_cargo_reminder_user_key"),
    )
    op.create_table(
        "push_delivery",
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="PREPARED", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'SENDING', 'ACCEPTED', 'RETRYABLE', 'FAILED', 'UNKNOWN', "
            "'SKIPPED')",
            name=op.f("ck_push_delivery_status_valid"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name=op.f("ck_push_delivery_attempt_count_valid")),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["cargo_reminder.notification_id"],
            ondelete="CASCADE",
            name=op.f("fk_push_delivery_notification_id_cargo_reminder"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["push_device.device_id"],
            ondelete="RESTRICT",
            name=op.f("fk_push_delivery_device_id_push_device"),
        ),
        sa.PrimaryKeyConstraint("notification_id", "device_id", name=op.f("pk_push_delivery")),
    )


def downgrade() -> None:
    op.drop_table("push_delivery")
    op.drop_table("cargo_reminder")
    op.drop_index("ix_push_device_user_active", table_name="push_device")
    op.drop_table("push_device")
