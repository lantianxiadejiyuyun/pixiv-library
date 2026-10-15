from logging.config import fileConfig
from alembic import context
from sqlalchemy import create_engine, pool
from app.config import settings
from app.models import Base

config = context.config
if config.config_file_name:
    # Development migrations may run after application modules have imported
    # their loggers. Keep those loggers enabled so worker diagnostics survive.
    fileConfig(config.config_file_name, disable_existing_loggers=False)
target_metadata = Base.metadata
database_url = config.attributes.get("database_url", settings.database_url)


def run_migrations_offline():
    context.configure(url=database_url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
