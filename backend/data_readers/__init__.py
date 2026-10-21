import logging
from pathlib import Path

from .base_reader import BaseDataReader
from .data_store_reader import DataStoreReader
from .watch_db_reader import WatchDBReader

logger = logging.getLogger(__name__)

__all__ = [
    'BaseDataReader',
    'WatchDBReader',
    'DataStoreReader',
    'create_reader',
]


def create_reader(
    data_source: str = "datastore",
    data_path: Path | None = None,
    **kwargs,
) -> BaseDataReader:
    src = data_source.lower()
    if src in ("datastore", "live"):
        from ..config import settings
        logger.info("Creating DataStoreReader for: %s", settings.DATA_STORE_PATH)
        return DataStoreReader(settings.DATA_STORE_PATH)

    raise ValueError(
        f"Unsupported data source: {data_source}. "
        f"Use 'datastore' (or legacy alias 'live')."
    )
