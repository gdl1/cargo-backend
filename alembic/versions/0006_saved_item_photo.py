"""저장한 짐에 연결된 사진의 S3 객체 키."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("saved_item", sa.Column("photo_object_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("saved_item", "photo_object_key")
