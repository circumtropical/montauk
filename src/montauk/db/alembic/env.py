"""Alembic environment for the Montauk PostgreSQL canonical store.

The DSN is resolved from (in order): ``-x db_url=...`` on the command line,
then ``MONTAUK_DATABASE_URL`` / ``DATABASE_URL``. There is no default.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from montauk.db.engine import resolve_url
from montauk.db.models import Base

config = context.config

# Only reconfigure logging when Alembic is driven from its own CLI. When it
# runs in-process (schema_ops.upgrade_to_head, the first-run wizard, tests)
# the host owns logging and fileConfig()'s disable_existing_loggers would
# silently detach every logger already created.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _db_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("db_url"):
        return resolve_url(x_args["db_url"])
    configured = config.get_main_option("sqlalchemy.url")
    return resolve_url(configured or None)


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
