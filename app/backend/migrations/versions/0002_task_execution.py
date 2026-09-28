"""Durable task execution ownership and material provenance."""
from alembic import op
import sqlalchemy as sa
from app.schema_v1 import identifier

revision = '0002_task_execution'
down_revision = '0001_existing_api'
branch_labels = depends_on = None

def upgrade():
    for name, column in [
        ('tasks', sa.Column('config_snapshot', sa.JSON)),
        ('tasks', sa.Column('current_attempt_id', identifier)),
        ('tasks', sa.Column('error_reason', sa.Text)),
        ('task_attempts', sa.Column('execution_id', sa.String(64))),
        ('materials', sa.Column('attempt_id', identifier)),
        ('materials', sa.Column('creative_content_id', identifier)),
        ('materials', sa.Column('file_path', sa.Text)),
        ('materials', sa.Column('version', identifier, nullable=False, server_default='1')),
        ('materials', sa.Column('review_history', sa.JSON)),
    ]:
        op.add_column(name, column)
    op.create_index('uq_materials_attempt_id', 'materials', ['attempt_id'], unique=True)

def downgrade():
    raise RuntimeError('Destructive downgrade is intentionally unavailable')
