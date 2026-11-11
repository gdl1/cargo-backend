"""적재 계획과 계산 결과의 초기 테이블."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vehicle",
        sa.Column("vehicle_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_vehicle_name_not_blank")),
        sa.CheckConstraint("btrim(vehicle_id) <> ''", name=op.f("ck_vehicle_vehicle_id_not_blank")),
        sa.PrimaryKeyConstraint("vehicle_id", name=op.f("pk_vehicle")),
    )
    op.create_table(
        "plan",
        sa.Column(
            "plan_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("vehicle_id", sa.Text(), nullable=False),
        sa.Column("passenger_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(user_id) <> ''", name=op.f("ck_plan_user_id_not_blank")),
        sa.CheckConstraint("passenger_count >= 1", name=op.f("ck_plan_passenger_count_positive")),
        sa.ForeignKeyConstraint(
            ["vehicle_id"],
            ["vehicle.vehicle_id"],
            name=op.f("fk_plan_vehicle_id_vehicle"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_plan")),
    )
    op.create_index(
        "ix_plan_user_created", "plan", ["user_id", "created_at", "plan_id"], unique=False
    )
    op.create_index(op.f("ix_plan_vehicle_id"), "plan", ["vehicle_id"], unique=False)
    op.create_table(
        "calculation",
        sa.Column(
            "calculation_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("is_success", sa.Boolean(), nullable=False),
        sa.Column(
            "current_folded_segments",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(current_folded_segments) = 'array' AND NOT "
            "jsonb_path_exists(current_folded_segments, '$[*] ? (@.type() != \"string\")')",
            name=op.f("ck_calculation_current_folded_segments_array"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plan.plan_id"],
            name=op.f("fk_calculation_plan_id_plan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("calculation_id", name=op.f("pk_calculation")),
        sa.UniqueConstraint("plan_id", "calculation_id", name="uq_calculation_plan_calculation"),
    )
    op.create_index(
        "ix_calculation_plan_created",
        "calculation",
        ["plan_id", "created_at", "calculation_id"],
        unique=False,
    )
    op.create_table(
        "plan_car_seat",
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("seat_code", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "seat_code IN ('ROW_1_RIGHT', 'ROW_1_CENTER', 'ROW_2_LEFT', 'ROW_2_CENTER', "
            "'ROW_2_RIGHT', 'ROW_3_LEFT', 'ROW_3_CENTER', 'ROW_3_RIGHT')",
            name=op.f("ck_plan_car_seat_seat_code_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plan.plan_id"],
            name=op.f("fk_plan_car_seat_plan_id_plan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_id", "seat_code", name=op.f("pk_plan_car_seat")),
    )
    op.create_table(
        "plan_item",
        sa.Column(
            "item_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("width_cm", sa.Numeric(), nullable=False),
        sa.Column("depth_cm", sa.Numeric(), nullable=False),
        sa.Column("height_cm", sa.Numeric(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_plan_item_name_not_blank")),
        sa.CheckConstraint(
            "depth_cm > 0 AND depth_cm < 'Infinity'::numeric", name=op.f("ck_plan_item_depth_valid")
        ),
        sa.CheckConstraint(
            "height_cm > 0 AND height_cm < 'Infinity'::numeric",
            name=op.f("ck_plan_item_height_valid"),
        ),
        sa.CheckConstraint(
            "width_cm > 0 AND width_cm < 'Infinity'::numeric", name=op.f("ck_plan_item_width_valid")
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_plan_item_position_positive")),
        sa.CheckConstraint("quantity >= 1", name=op.f("ck_plan_item_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["plan.plan_id"],
            name=op.f("fk_plan_item_plan_id_plan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("item_id", name=op.f("pk_plan_item")),
        sa.UniqueConstraint("plan_id", "item_id", name="uq_plan_item_plan_item"),
        sa.UniqueConstraint("plan_id", "position", name="uq_plan_item_plan_position"),
    )
    op.create_table(
        "arrangement",
        sa.Column(
            "arrangement_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("calculation_id", sa.Uuid(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("passenger_seats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "target_folded_segments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("score", sa.Numeric(), nullable=True),
        sa.CheckConstraint(
            "score > '-Infinity'::numeric AND score < 'Infinity'::numeric",
            name=op.f("ck_arrangement_score_finite"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(passenger_seats) = 'array' AND passenger_seats @> "
            '\'["ROW_1_LEFT"]\'::jsonb AND passenger_seats <@ \'["ROW_1_LEFT", '
            '"ROW_1_CENTER", "ROW_1_RIGHT", "ROW_2_LEFT", "ROW_2_CENTER", "ROW_2_RIGHT",'
            ' "ROW_3_LEFT", "ROW_3_CENTER", "ROW_3_RIGHT"]\'::jsonb',
            name=op.f("ck_arrangement_passenger_seats_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_folded_segments) = 'array' AND NOT "
            "jsonb_path_exists(target_folded_segments, '$[*] ? (@.type() != \"string\")')",
            name=op.f("ck_arrangement_target_folded_segments_array"),
        ),
        sa.CheckConstraint("rank >= 1", name=op.f("ck_arrangement_rank_positive")),
        sa.ForeignKeyConstraint(
            ["plan_id", "calculation_id"],
            ["calculation.plan_id", "calculation.calculation_id"],
            name=op.f("fk_arrangement_plan_id_calculation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("arrangement_id", name=op.f("pk_arrangement")),
        sa.UniqueConstraint("calculation_id", "rank", name="uq_arrangement_calculation_rank"),
        sa.UniqueConstraint("plan_id", "arrangement_id", name="uq_arrangement_plan_arrangement"),
    )
    op.create_table(
        "placement",
        sa.Column(
            "placement_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("arrangement_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("x_cm", sa.Numeric(), nullable=False),
        sa.Column("y_cm", sa.Numeric(), nullable=False),
        sa.Column("z_cm", sa.Numeric(), nullable=False),
        sa.Column("orientation", sa.Text(), nullable=False),
        sa.Column("load_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "orientation IN ('WDH', 'WHD', 'DWH', 'DHW', 'HWD', 'HDW')",
            name=op.f("ck_placement_orientation_valid"),
        ),
        sa.CheckConstraint(
            "x_cm >= 0 AND x_cm < 'Infinity'::numeric", name=op.f("ck_placement_x_valid")
        ),
        sa.CheckConstraint(
            "y_cm >= 0 AND y_cm < 'Infinity'::numeric", name=op.f("ck_placement_y_valid")
        ),
        sa.CheckConstraint(
            "z_cm >= 0 AND z_cm < 'Infinity'::numeric", name=op.f("ck_placement_z_valid")
        ),
        sa.CheckConstraint("load_order >= 1", name=op.f("ck_placement_load_order_positive")),
        sa.ForeignKeyConstraint(
            ["plan_id", "arrangement_id"],
            ["arrangement.plan_id", "arrangement.arrangement_id"],
            name=op.f("fk_placement_plan_id_arrangement"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "item_id"],
            ["plan_item.plan_id", "plan_item.item_id"],
            name=op.f("fk_placement_plan_id_plan_item"),
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("placement_id", name=op.f("pk_placement")),
        sa.UniqueConstraint(
            "arrangement_id", "load_order", name="uq_placement_arrangement_load_order"
        ),
    )
    op.create_index("ix_placement_plan_item", "placement", ["plan_id", "item_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_placement_plan_item", table_name="placement")
    op.drop_table("placement")
    op.drop_table("arrangement")
    op.drop_table("plan_item")
    op.drop_table("plan_car_seat")
    op.drop_index("ix_calculation_plan_created", table_name="calculation")
    op.drop_table("calculation")
    op.drop_index(op.f("ix_plan_vehicle_id"), table_name="plan")
    op.drop_index("ix_plan_user_created", table_name="plan")
    op.drop_table("plan")
    op.drop_table("vehicle")
