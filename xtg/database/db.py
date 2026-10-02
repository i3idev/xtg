"""Database engine, session management and maintenance helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Table, create_engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from xtg.core.config import Settings, get_settings
from xtg.core.errors import DatabaseError
from xtg.database.models import Base

#: Reddit tables, reported by ``xtg reddit status`` and ``xtg db stats``.
REDDIT_TABLES: tuple[str, ...] = (
    "reddit_users",
    "reddit_subreddits",
    "reddit_posts",
    "reddit_comments",
)

#: Tables in a stable order, used by ``xtg db stats`` and ``xtg status``.
TABLE_NAMES: tuple[str, ...] = (
    "users",
    "chats",
    "memberships",
    "messages",
    "media",
    "entities",
    "relations",
    *REDDIT_TABLES,
)


class Database:
    """Thin wrapper around a SQLite engine.

    The class owns the engine lifecycle: :meth:`create_all` is idempotent, so
    every command can simply ensure the schema exists before running.
    """

    def __init__(self, settings: Settings | None = None, *, echo: bool = False) -> None:
        self.settings = settings or get_settings()
        self.path: Path = self.settings.database_file
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._echo = echo

    # --- Engine ------------------------------------------------------------
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = self._build_engine()
            self._session_factory = sessionmaker(
                bind=self._engine, expire_on_commit=False, future=True
            )
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            _ = self.engine
        if self._session_factory is None:  # pragma: no cover - defensive
            raise DatabaseError("Failed to create the database session factory.")
        return self._session_factory

    def _build_engine(self) -> Engine:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            engine = create_engine(
                self.settings.database_url,
                echo=self._echo,
                future=True,
                connect_args={"check_same_thread": False, "timeout": 30},
            )
        except Exception as exc:  # pragma: no cover - depends on the filesystem
            raise DatabaseError(
                f"Cannot open database {self.path}: {exc}",
                hint="Check the path in XTG_DATABASE_PATH and the directory permissions.",
            ) from exc
        _apply_sqlite_pragmas(engine)
        return engine

    # --- Sessions ----------------------------------------------------------
    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session scope: commit on success, rollback on error."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --- Schema ------------------------------------------------------------
    def create_all(self) -> None:
        """Create missing tables. Safe to call on every command."""
        try:
            Base.metadata.create_all(self.engine)
        except Exception as exc:
            raise DatabaseError(f"Cannot initialise the database schema: {exc}") from exc

    def drop_all(self) -> None:
        """Drop every table. Destructive: the caller must confirm first."""
        Base.metadata.drop_all(self.engine)

    def reset(self) -> None:
        """Drop and recreate the schema (used by ``xtg db reset``)."""
        self.drop_all()
        self.create_all()

    # --- Introspection -----------------------------------------------------
    @property
    def exists(self) -> bool:
        return self.path.exists()

    def table_names(self) -> list[str]:
        return sorted(Base.metadata.tables)

    def counts(self) -> dict[str, int]:
        """Row count per known table."""
        result: dict[str, int] = dict.fromkeys(TABLE_NAMES, 0)
        inspector_tables = set(self._existing_tables())
        with self.session() as session:
            for name in TABLE_NAMES:
                if name in inspector_tables:
                    result[name] = (
                        session.scalar(select(func.count()).select_from(_table(name))) or 0
                    )
        return result

    def total_rows(self) -> int:
        return sum(self.counts().values())

    def size_bytes(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def vacuum(self) -> None:
        """Reclaim unused pages."""
        with self.session() as session:
            session.execute(text("VACUUM"))

    def _existing_tables(self) -> list[str]:
        from sqlalchemy import inspect

        return inspect(self.engine).get_table_names()

    def dispose(self) -> None:
        """Release the engine. Called on CLI shutdown."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None


def _apply_sqlite_pragmas(engine: Engine) -> None:
    """WAL + foreign keys: durability and cascade support on SQLite."""

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def get_database(settings: Settings | None = None) -> Database:
    """Factory kept for symmetry with :func:`xtg.core.get_settings`."""
    return Database(settings)
