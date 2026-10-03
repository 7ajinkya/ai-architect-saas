from contextlib import asynccontextmanager
from typing import AsyncIterator

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


CHECKPOINT_DB_PATH = "ai_architect_checkpoints.sqlite"


@asynccontextmanager
async def create_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    async with AsyncSqliteSaver.from_conn_string(
        CHECKPOINT_DB_PATH
    ) as checkpointer:
        yield checkpointer