"""계획 저장 재시도 키와 유지 좌석 추가."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan", sa.Column("idempotency_key", sa.Uuid(), nullable=True))
    op.add_column("plan", sa.Column("request_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_plan_creation_request_valid"),
        "plan",
        "(idempotency_key IS NULL AND request_hash IS NULL) OR "
        "(idempotency_key IS NOT NULL AND request_hash IS NOT NULL "
        "AND request_hash ~ '^[0-9a-f]{64}$')",
    )
    op.create_unique_constraint(
        "uq_plan_user_idempotency_key", "plan", ["user_id", "idempotency_key"]
    )
    op.create_table(
        "plan_retained_seat",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("seat_code", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "seat_code IN ('ROW_1_LEFT', 'ROW_1_CENTER', 'ROW_1_RIGHT', "
            "'ROW_2_LEFT', 'ROW_2_CENTER', 'ROW_2_RIGHT', "
            "'ROW_3_LEFT', 'ROW_3_CENTER', 'ROW_3_RIGHT')",
            name=op.f("ck_plan_retained_seat_seat_code_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plan.plan_id"],
            ondelete="CASCADE",
            name=op.f("fk_plan_retained_seat_plan_id_plan"),
        ),
        sa.PrimaryKeyConstraint("plan_id", "seat_code", name=op.f("pk_plan_retained_seat")),
    )


def downgrade() -> None:
    op.drop_table("plan_retained_seat")
    op.drop_constraint("uq_plan_user_idempotency_key", "plan", type_="unique")
    op.drop_constraint(op.f("ck_plan_creation_request_valid"), "plan", type_="check")
    op.drop_column("plan", "request_hash")
    op.drop_column("plan", "idempotency_key")
