"""Allow synchronous verification to persist a claim without creating a run."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("claims", "run_id", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # The database will refuse the downgrade if standalone claims exist.
    op.alter_column("claims", "run_id", existing_type=sa.Text(), nullable=False)
