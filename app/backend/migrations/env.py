from alembic import context
from app.db import get_engine
from app.schema_v1 import metadata

if context.is_offline_mode():
    from app.db import configured_url
    context.configure(url=configured_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    # CLI and application upgrades can share a caller-owned connection.
    connection = context.config.attributes.get('connection')
    if connection is not None:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        with get_engine().begin() as connection:
            context.configure(connection=connection, target_metadata=metadata)
            with context.begin_transaction():
                context.run_migrations()
