"""계획 수정, 계산 이력, 미리보기와 AI 요청 저장."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_conversation",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_ai_conversation_version_positive")),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_ai_conversation")),
    )
    op.create_index("ix_ai_conversation_user", "ai_conversation", ["user_id"], unique=False)
    op.create_table(
        "calculation_preview",
        sa.Column(
            "preview_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("source_plan_id", sa.Uuid(), nullable=True),
        sa.Column("source_plan_revision", sa.Integer(), nullable=True),
        sa.Column("vehicle_data_version", sa.Text(), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("preview_id", name=op.f("pk_calculation_preview")),
    )
    op.create_index(
        "ix_calculation_preview_user_expiry",
        "calculation_preview",
        ["user_id", "expires_at"],
        unique=False,
    )
    op.create_table(
        "operation_receipt",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "scope", "key", name=op.f("pk_operation_receipt")),
    )
    op.create_table(
        "product_candidate",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("candidate_id", name=op.f("pk_product_candidate")),
    )
    op.create_index("ix_product_candidate_user", "product_candidate", ["user_id"], unique=False)
    op.create_table(
        "saved_item",
        sa.Column(
            "saved_item_id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("width_cm", sa.Numeric(), nullable=False),
        sa.Column("depth_cm", sa.Numeric(), nullable=False),
        sa.Column("height_cm", sa.Numeric(), nullable=False),
        sa.Column("default_quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("allowed_orientations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("transport_state", sa.Text(), nullable=False),
        sa.Column("dimension_source", sa.Text(), nullable=False),
        sa.Column("material", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_saved_item_name_not_blank")),
        sa.CheckConstraint("btrim(user_id) <> ''", name=op.f("ck_saved_item_user_not_blank")),
        sa.CheckConstraint(
            "depth_cm > 0 AND depth_cm < 'Infinity'::numeric",
            name=op.f("ck_saved_item_depth_valid"),
        ),
        sa.CheckConstraint(
            "dimension_source IN ('AR', 'MANUAL', 'PRODUCT_SOURCE', 'UNSPECIFIED')",
            name=op.f("ck_saved_item_dimension_source_valid"),
        ),
        sa.CheckConstraint(
            "height_cm > 0 AND height_cm < 'Infinity'::numeric",
            name=op.f("ck_saved_item_height_valid"),
        ),
        sa.CheckConstraint(
            "transport_state IN ('PACKAGED', 'ASSEMBLED', 'FOLDED', 'OTHER', 'UNKNOWN')",
            name=op.f("ck_saved_item_transport_state_valid"),
        ),
        sa.CheckConstraint(
            "width_cm > 0 AND width_cm < 'Infinity'::numeric",
            name=op.f("ck_saved_item_width_valid"),
        ),
        sa.CheckConstraint("default_quantity >= 1", name=op.f("ck_saved_item_quantity_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_orientations) = 'array' "
            "AND jsonb_array_length(allowed_orientations) > 0 "
            'AND allowed_orientations <@ \'["WDH","WHD","DWH","DHW","HWD","HDW"]\'::jsonb',
            name=op.f("ck_saved_item_orientations_valid"),
        ),
        sa.PrimaryKeyConstraint("saved_item_id", name=op.f("pk_saved_item")),
    )
    op.create_index(
        "ix_saved_item_user_created",
        "saved_item",
        ["user_id", "created_at", "saved_item_id"],
        unique=False,
    )
    op.add_column(
        "calculation",
        sa.Column("plan_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "calculation",
        sa.Column(
            "result_payload",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "plan",
        sa.Column(
            "passenger_seats",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("plan", sa.Column("revision", sa.Integer(), server_default="1", nullable=False))
    op.add_column(
        "plan",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column("plan", sa.Column("selected_calculation_id", sa.Uuid(), nullable=True))
    op.add_column("plan", sa.Column("selected_arrangement_id", sa.Uuid(), nullable=True))
    op.add_column("plan", sa.Column("name", sa.Text(), nullable=True))
    op.add_column("plan", sa.Column("category", sa.Text(), nullable=True))
    op.add_column("plan", sa.Column("memo", sa.Text(), nullable=True))
    op.add_column(
        "plan",
        sa.Column("is_favorite", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("plan", sa.Column("favorited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "plan_item",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "plan_item",
        sa.Column("required_quantity", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "plan_item",
        sa.Column(
            "allowed_orientations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text('\'["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"]\'::jsonb'),
            nullable=False,
        ),
    )
    op.add_column(
        "plan_item",
        sa.Column("transport_state", sa.Text(), server_default="UNKNOWN", nullable=False),
    )
    op.add_column(
        "plan_item",
        sa.Column("dimension_source", sa.Text(), server_default="UNSPECIFIED", nullable=False),
    )
    op.add_column("plan_item", sa.Column("material", sa.Text(), nullable=True))
    op.add_column("plan_item", sa.Column("saved_item_id", sa.Uuid(), nullable=True))
    op.drop_constraint(op.f("uq_plan_item_plan_position"), "plan_item", type_="unique")
    op.create_index(
        "uq_plan_item_active_position",
        "plan_item",
        ["plan_id", "position"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_foreign_key(
        op.f("fk_plan_item_saved_item_id_saved_item"),
        "plan_item",
        "saved_item",
        ["saved_item_id"],
        ["saved_item_id"],
        ondelete="SET NULL",
    )

    op.create_check_constraint(
        op.f("ck_plan_category_valid"),
        "plan",
        "category IS NULL OR category IN ('TRAVEL', 'LEISURE', 'MOVING')",
    )
    op.create_check_constraint(op.f("ck_plan_revision_positive"), "plan", "revision >= 1")
    op.create_check_constraint(
        op.f("ck_plan_selection_pair"),
        "plan",
        "(selected_calculation_id IS NULL) = (selected_arrangement_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_plan_item_dimension_source_valid"),
        "plan_item",
        "dimension_source IN ('AR', 'MANUAL', 'SAVED_ITEM', 'PRODUCT_SOURCE', 'UNSPECIFIED')",
    )
    op.create_check_constraint(
        op.f("ck_plan_item_orientations_valid"),
        "plan_item",
        "jsonb_typeof(allowed_orientations) = 'array' "
        "AND jsonb_array_length(allowed_orientations) > 0 "
        'AND allowed_orientations <@ \'["WDH","WHD","DWH","DHW","HWD","HDW"]\'::jsonb',
    )
    op.create_check_constraint(
        op.f("ck_plan_item_required_quantity_valid"),
        "plan_item",
        "required_quantity >= 0 AND required_quantity <= quantity",
    )
    op.create_check_constraint(
        op.f("ck_plan_item_transport_state_valid"),
        "plan_item",
        "transport_state IN ('PACKAGED', 'ASSEMBLED', 'FOLDED', 'OTHER', 'UNKNOWN')",
    )


def downgrade() -> None:
    # 이력 항목을 구버전의 현재 항목으로 되살리지 않도록 되돌리기를 중단합니다.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM plan_item WHERE NOT is_active) THEN
            RAISE EXCEPTION 'Inactive historical items exist; restore the pre-upgrade backup';
        END IF;
    END $$""")
    op.drop_constraint(op.f("ck_plan_category_valid"), "plan", type_="check")
    op.drop_constraint(op.f("ck_plan_revision_positive"), "plan", type_="check")
    op.drop_constraint(op.f("ck_plan_selection_pair"), "plan", type_="check")
    op.drop_constraint(op.f("ck_plan_item_dimension_source_valid"), "plan_item", type_="check")
    op.drop_constraint(op.f("ck_plan_item_orientations_valid"), "plan_item", type_="check")
    op.drop_constraint(op.f("ck_plan_item_required_quantity_valid"), "plan_item", type_="check")
    op.drop_constraint(op.f("ck_plan_item_transport_state_valid"), "plan_item", type_="check")
    op.drop_constraint(
        op.f("fk_plan_item_saved_item_id_saved_item"), "plan_item", type_="foreignkey"
    )
    op.drop_index(
        "uq_plan_item_active_position",
        table_name="plan_item",
        postgresql_where=sa.text("is_active"),
    )
    op.create_unique_constraint(
        op.f("uq_plan_item_plan_position"),
        "plan_item",
        ["plan_id", "position"],
        postgresql_nulls_not_distinct=False,
    )
    op.drop_column("plan_item", "saved_item_id")
    op.drop_column("plan_item", "material")
    op.drop_column("plan_item", "dimension_source")
    op.drop_column("plan_item", "transport_state")
    op.drop_column("plan_item", "allowed_orientations")
    op.drop_column("plan_item", "required_quantity")
    op.drop_column("plan_item", "is_active")
    op.drop_column("plan", "favorited_at")
    op.drop_column("plan", "is_favorite")
    op.drop_column("plan", "memo")
    op.drop_column("plan", "category")
    op.drop_column("plan", "name")
    op.drop_column("plan", "selected_arrangement_id")
    op.drop_column("plan", "selected_calculation_id")
    op.drop_column("plan", "updated_at")
    op.drop_column("plan", "revision")
    op.drop_column("plan", "passenger_seats")
    op.drop_column("calculation", "result_payload")
    op.drop_column("calculation", "plan_revision")
    op.drop_index("ix_saved_item_user_created", table_name="saved_item")
    op.drop_table("saved_item")
    op.drop_index("ix_product_candidate_user", table_name="product_candidate")
    op.drop_table("product_candidate")
    op.drop_table("operation_receipt")
    op.drop_index("ix_calculation_preview_user_expiry", table_name="calculation_preview")
    op.drop_table("calculation_preview")
    op.drop_index("ix_ai_conversation_user", table_name="ai_conversation")
    op.drop_table("ai_conversation")
