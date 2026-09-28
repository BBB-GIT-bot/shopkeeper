"""Keep the exact diagnosis and creative versions behind each proposal."""
from alembic import op
import sqlalchemy as sa
from app.schema_v1 import identifier
revision='0003_proposal_snapshot'
down_revision='0002_task_execution'
branch_labels=depends_on=None

def upgrade():
    op.add_column('proposals',sa.Column('diagnosis_content_id',identifier,nullable=True))
    op.add_column('proposals',sa.Column('creative_content_id',identifier,nullable=True))
    op.add_column('proposals',sa.Column('content_snapshot',sa.JSON,nullable=True))

def downgrade():
    raise RuntimeError('Destructive downgrade is intentionally unavailable')
