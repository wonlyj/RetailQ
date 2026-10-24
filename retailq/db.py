"""Synchronous short-lived sessions; callers run database work in a worker thread."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from retailq.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.engine = create_engine(
            settings.database_url, pool_pre_ping=True, hide_parameters=True,
            connect_args={"connect_timeout": 3, "read_timeout": 3, "write_timeout": 3},
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def close(self) -> None:
        self.engine.dispose()
