import json
import logging
import os
from pathlib import Path

from ..utils import ts_now

logger = logging.getLogger(__name__)

class AgentStateRepository:

    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _get_state_file(self, user_id: str) -> Path:
        return self.storage_dir / f"{user_id}_state.json"

    def save_state(self, user_id: str, state: dict):
        file_path = self._get_state_file(user_id)

        state_copy = {**state, 'last_updated': ts_now()}

        try:
            temp_path = file_path.with_suffix('.json.tmp')
            with open(temp_path, 'w') as f:
                json.dump(state_copy, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            temp_path.replace(file_path)
            logger.debug(f"Saved agent state for {user_id}")
        except Exception as e:
            logger.error(f"Failed to save agent state: {e}")

    def save_state_json(self, user_id: str, state_json: str) -> None:
        file_path = self._get_state_file(user_id)
        try:
            temp_path = file_path.with_suffix('.json.tmp')
            with open(temp_path, 'w') as f:
                f.write(state_json)
                f.flush()
                os.fsync(f.fileno())
            temp_path.replace(file_path)
        except Exception as e:
            logger.error(f"Failed to save agent state: {e}")

    def load_state(self, user_id: str) -> dict | None:
        file_path = self._get_state_file(user_id)

        if not file_path.exists():
            return None

        try:
            with open(file_path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load agent state: {e}")
            return None

    def clear_state(self, user_id: str):
        file_path = self._get_state_file(user_id)
        if file_path.exists():
            file_path.unlink()
            logger.info(f"Cleared agent state for {user_id}")
