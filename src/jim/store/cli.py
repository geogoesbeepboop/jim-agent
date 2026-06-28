"""``jim-initdb`` — create the pgvector extension and tables.

docker compose up -d
DATABASE_URL=postgresql+asyncpg://jim:jim@localhost:5432/jim uv run jim-initdb
"""

from __future__ import annotations

import asyncio
import sys

from jim.config import get_settings
from jim.store.db import init_db


def main() -> int:
    settings = get_settings()
    if not settings.database_url:
        print(
            "DATABASE_URL is not set — jim will use the in-memory store "
            "(nothing to initialize). Set it to use Postgres+pgvector.",
            file=sys.stderr,
        )
        return 1
    asyncio.run(init_db(settings.database_url))
    print(f"Initialized pgvector + tables at {settings.database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
