"""Current schema; migration snapshots remain immutable."""
import sqlalchemy as sa
from .schema_v1 import metadata as initial, identifier, timestamp

metadata = sa.MetaData(naming_convention=initial.naming_convention)
for table in initial.sorted_tables:
    table.to_metadata(metadata)
metadata.tables['tasks'].append_column(sa.Column('config_snapshot', sa.JSON, nullable=True))
metadata.tables['tasks'].append_column(sa.Column('current_attempt_id', identifier, nullable=True))
metadata.tables['tasks'].append_column(sa.Column('error_reason', sa.Text, nullable=True))
metadata.tables['task_attempts'].append_column(sa.Column('execution_id', sa.String(64), nullable=True))
metadata.tables['materials'].append_column(sa.Column('attempt_id', identifier, nullable=True))
metadata.tables['materials'].append_column(sa.Column('creative_content_id', identifier, nullable=True))
metadata.tables['materials'].append_column(sa.Column('file_path', sa.Text, nullable=True))
metadata.tables['materials'].append_column(sa.Column('version', identifier, nullable=False, server_default='1'))
metadata.tables['materials'].append_column(sa.Column('review_history', sa.JSON, nullable=True))
metadata.tables['materials'].append_constraint(sa.UniqueConstraint('attempt_id', name='uq_materials_attempt_id'))
metadata.tables['proposals'].append_column(sa.Column('diagnosis_content_id', identifier, nullable=True))
metadata.tables['proposals'].append_column(sa.Column('creative_content_id', identifier, nullable=True))
metadata.tables['proposals'].append_column(sa.Column('content_snapshot', sa.JSON, nullable=True))
