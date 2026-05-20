from digitorn.modules.database.adapters.base import (
    ColumnInfo,
    DatabaseAdapter,
    ExecuteResult,
    FetchResult,
    ForeignKey,
    IndexInfo,
    ItemChecksum,
    SchemaInfo,
    TableInfo,
    TableStats,
    WatchItem,
)

__all__ = [
    "ColumnInfo",
    "DatabaseAdapter",
    "ExecuteResult",
    "FetchResult",
    "ForeignKey",
    "IndexInfo",
    "ItemChecksum",
    "SchemaInfo",
    "TableInfo",
    "TableStats",
    "WatchItem",
]

def __getattr__(name: str):
    if name == "MongoAdapter":
        from digitorn.modules.database.adapters.mongo import MongoAdapter
        return MongoAdapter
    if name == "RedisAdapter":
        from digitorn.modules.database.adapters.redis import RedisAdapter
        return RedisAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
