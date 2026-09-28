"""Initial schema of the existing API; names preserved for compatibility.

Revision ID: 0001_existing_api
"""
from alembic import context, op
from app.schema_v1 import metadata
import sqlalchemy as sa

revision = '0001_existing_api'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode() and op.get_bind().dialect.name == 'mysql':
        existing = set(sa.inspect(op.get_bind()).get_table_names())
        if existing.intersection(metadata.tables):
            raise RuntimeError('初始 MySQL 迁移需要空的专用数据库，不能直接接管既有业务表')
    for table in metadata.sorted_tables:
        table.create(op.get_bind(), checkfirst=not context.is_offline_mode())


def downgrade():
    # A destructive production rollback requires a separate reviewed migration.
    raise RuntimeError('初始业务表不可自动删除；请使用备份恢复或经确认的数据迁移')
