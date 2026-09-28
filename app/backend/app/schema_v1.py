"""Immutable initial schema for the existing API. Extend with a new migration.

The scaffold's public/physical names are preserved. Later business stages add
attempt ownership, experiment groups and import rows via separate revisions.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

metadata = sa.MetaData(naming_convention={
    'pk': 'pk_%(table_name)s', 'uq': 'uq_%(table_name)s_%(column_0_name)s',
    'fk': 'fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s',
    'ix': 'ix_%(table_name)s_%(column_0_name)s', 'ck': 'ck_%(table_name)s_%(constraint_name)s',
})
identifier = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')
timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), 'mysql')


class utc_timestamp(FunctionElement):
    type = sa.DateTime()
    inherit_cache = True


@compiles(utc_timestamp)
def timestamp_default(element, compiler, **kwargs):
    return 'CURRENT_TIMESTAMP'


@compiles(utc_timestamp, 'mysql')
def mysql_timestamp_default(element, compiler, **kwargs):
    return 'CURRENT_TIMESTAMP(6)'


def pk():
    return sa.Column('id', identifier, primary_key=True, autoincrement=True)


def ref(name, target, nullable=False):
    return sa.Column(name, identifier, sa.ForeignKey(target), nullable=nullable)


def code(name, default=None, unique=False):
    return sa.Column(name, sa.String(128), nullable=False, unique=unique, server_default=default)


def state(name, default):
    return sa.Column(name, sa.String(32), nullable=False, server_default=default)


def text(name, default=None, nullable=True):
    return sa.Column(name, sa.Text, nullable=nullable, server_default=sa.text(f'({default})') if default is not None else None)


def integer(name, default=None, nullable=False):
    return sa.Column(name, identifier, nullable=nullable, server_default=str(default) if default is not None else None)


def moment(name, current=False):
    return sa.Column(name, timestamp, server_default=utc_timestamp() if current else None)


def structured(name, default=None, nullable=False):
    return sa.Column(name, sa.JSON, nullable=nullable,
                     server_default=sa.text(f"('{default}')") if default is not None else None)


def money(name):
    return sa.Column(name, sa.Numeric(18, 2).with_variant(sa.DECIMAL(18, 2), 'mysql'), nullable=False)


def table(name, *columns):
    return sa.Table(name, metadata, pk(), *columns, mysql_engine='InnoDB', mysql_charset='utf8mb4', mysql_collate='utf8mb4_bin')


users = table('users', code('username', unique=True), sa.Column('password_hash', sa.String(255), nullable=False),
              state('role', 'operator'), sa.Column('enabled', sa.Boolean, nullable=False, server_default='1'))
shops = table('shops', code('shop_code', unique=True), code('platform'), text('name', nullable=False),
              code('platform_shop_id'), state('authorization_status', 'not_configured'))
products = table('products', code('product_code', unique=True), text('name', nullable=False), text('category', nullable=False),
                 money('base_price'), structured('attributes', '{}'), integer('main_sku_id', nullable=True))
skus = table('skus', ref('product_id', 'products.id'), code('sku_code'), structured('attributes', '{}'), sa.UniqueConstraint('product_id', 'sku_code'))
listings = table('listings', ref('product_id', 'products.id'), ref('shop_id', 'shops.id'), code('platform_product_id'),
                 sa.Column('on_sale', sa.Boolean, nullable=False, server_default='1'), sa.UniqueConstraint('product_id', 'shop_id'))
sku_mappings = table('sku_mappings', ref('listing_id', 'listings.id'), ref('sku_id', 'skus.id'), code('platform_sku_id'), money('sale_price'),
                     integer('stock_quantity'), integer('warning_threshold', 0), sa.UniqueConstraint('listing_id', 'sku_id'))
stock_movements = table('stock_movements', ref('mapping_id', 'sku_mappings.id'), integer('before_quantity'), integer('after_quantity'),
                        integer('delta'), text('reason', nullable=False), state('source', 'manual'), ref('actor_id', 'users.id', True), moment('created_at', True))
competitors = table('competitors', ref('product_id', 'products.id'), code('competitor_code'), text('name', nullable=False), money('price'),
                    code('platform', ''), structured('attributes', '{}'), structured('selling_points', '[]'), text('source_url', "''"),
                    text('source_text', "''"), state('verification_status', 'unverified'), sa.UniqueConstraint('product_id', 'competitor_code'))
rounds = table('rounds', ref('product_id', 'products.id'), integer('round_no'), state('phase', 'setup'), integer('source_report_id', nullable=True),
               structured('selected_actions', '[]'), sa.UniqueConstraint('product_id', 'round_no'))
contents = table('contents', ref('product_id', 'products.id'), ref('round_id', 'rounds.id'), state('kind', 'diagnosis'), integer('version'),
                 state('status', 'draft'), structured('content'), structured('edited_content', nullable=True), ref('source_content_id', 'contents.id', True),
                 ref('confirmed_by', 'users.id', True), moment('confirmed_at'), moment('archived_at'), sa.UniqueConstraint('round_id', 'kind', 'version'))
tasks = table('tasks', ref('product_id', 'products.id'), ref('round_id', 'rounds.id'), ref('creative_content_id', 'contents.id'), code('selection_id'),
              state('task_type', 'image'), state('status', 'pending'), state('scenario', 'success'), integer('attempts', 0), integer('max_auto_retries', 2),
              integer('poll_interval_seconds', 2), ref('original_task_id', 'tasks.id', True), integer('material_id', nullable=True), text('cancel_reason'),
              moment('cancelled_at'), moment('queued_at', True), moment('started_at'), moment('heartbeat_at'), moment('deadline_at'),
              moment('finished_at'), moment('created_at', True), moment('updated_at', True))
task_attempts = table('task_attempts', ref('task_id', 'tasks.id'), integer('attempt_no'), state('status', 'running'), state('scenario', 'success'),
                      moment('queued_at', True), moment('started_at'), moment('heartbeat_at'), moment('finished_at'), moment('deadline_at'),
                      text('error_reason', "''"), integer('process_id', nullable=True), sa.UniqueConstraint('task_id', 'attempt_no'))
materials = table('materials', ref('task_id', 'tasks.id'), ref('product_id', 'products.id'), ref('round_id', 'rounds.id'), state('material_type', 'image'),
                  code('selection_id'), text('file_url', nullable=False), sa.Column('is_demo', sa.Boolean, nullable=False, server_default='1'),
                  state('review_status', 'pending'), structured('tags', '[]'), moment('archived_at'), ref('reviewed_by', 'users.id', True), moment('reviewed_at'))
proposals = table('proposals', ref('product_id', 'products.id'), ref('round_id', 'rounds.id'), ref('shop_id', 'shops.id'), code('proposal_key'), integer('version'),
                  state('status', 'draft'), structured('strategy'), structured('groups_data'), structured('targets'), structured('material_ids'),
                  sa.Column('date_from', sa.Date, nullable=False), sa.Column('date_to', sa.Date, nullable=False), ref('content_confirmed_by', 'users.id', True),
                  moment('content_confirmed_at'), moment('archived_at'), sa.UniqueConstraint('proposal_key', 'version'))
approvals = table('approvals', ref('proposal_id', 'proposals.id'), state('decision', 'rejected'), text('comment', "''"), ref('actor_id', 'users.id'), moment('created_at', True))
experiments = table('experiments', ref('proposal_id', 'proposals.id'), ref('shop_id', 'shops.id'), ref('product_id', 'products.id'),
                    sa.Column('date_from', sa.Date, nullable=False), sa.Column('date_to', sa.Date, nullable=False), structured('targets'),
                    text('execution_note', "''"), state('status', 'approved'), sa.UniqueConstraint('proposal_id'))
metrics = table('metrics', ref('experiment_id', 'experiments.id'), sa.Column('date', sa.Date, nullable=False), code('group_code'), integer('impressions'),
                integer('clicks'), integer('orders'), money('gmv'), money('ad_spend'), integer('detail_visits', nullable=True), integer('detail_bounces', nullable=True),
                sa.UniqueConstraint('experiment_id', 'date', 'group_code'))
reviews = table('reviews', ref('product_id', 'products.id'), ref('round_id', 'rounds.id'), ref('experiment_id', 'experiments.id'), integer('version'), state('status', 'draft'),
                structured('sections'), structured('actions'), structured('snapshot'), moment('confirmed_at'), moment('archived_at'), sa.UniqueConstraint('round_id', 'version'))
imports = table('imports', code('import_type'), text('filename', nullable=False), state('status', 'previewed'), structured('rows_data'), structured('summary'),
                structured('field_mapping', '{}'), text('sheet_name'), moment('created_at', True))
service_configs = table('service_configs', text('base_url', "''"), text('model', "''"), text('api_key', "''"), text('api_key_ciphertext', "''"),
                        sa.Column('temperature', sa.Float, server_default='0.2'), integer('request_timeout_seconds', 60), state('multimodal_mode', 'simulation'),
                        ref('updated_by', 'users.id', True), moment('updated_at', True), sa.CheckConstraint('id=1', name='singleton'))
queue_configs = table('queue_configs', integer('concurrency', 2), integer('execution_timeout_seconds', 60), integer('max_auto_retries', 2),
                      integer('poll_interval_seconds', 2), integer('heartbeat_seconds', 10), integer('heartbeat_interval_seconds', 10),
                      integer('dispatch_interval_seconds', 1), ref('updated_by', 'users.id', True), moment('updated_at', True), sa.CheckConstraint('id=1', name='singleton'))
service_configs.c.id.autoincrement = False
queue_configs.c.id.autoincrement = False
sa.Index('ix_tasks_status_id', tasks.c.status, tasks.c.id)
sa.Index('ix_materials_product_round', materials.c.product_id, materials.c.round_id)
sa.Index('ix_stock_movements_mapping_id', stock_movements.c.mapping_id, stock_movements.c.id)
