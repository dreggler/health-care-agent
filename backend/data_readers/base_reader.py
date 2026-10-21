from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd


class BaseDataReader(ABC):

    def __init__(self, data_path: Path):
        self.data_path = Path(data_path)
        if not self.data_path.exists():
            raise ValueError(f"Data path does not exist: {data_path}")

    @abstractmethod
    def get_available_users(self, datasets: list[str] | None = None) -> list[str]:
        pass

    @abstractmethod
    def load_feature_data(
        self,
        pids: list[str],
        feature_type: str,
        datasets: list[str] | None = None
    ) -> pd.DataFrame:
        pass

    @abstractmethod
    def get_feature_types(self) -> list[str]:
        pass

    @abstractmethod
    def get_feature_columns(self, feature_type: str) -> list[str]:
        pass

    @abstractmethod
    def get_date_range(self, pids: list[str]) -> tuple:
        pass

    def get_datasets(self) -> list[str]:
        return []

    def get_user_info(
        self,
        pids: list[str],
        datasets: list[str] | None = None
    ) -> pd.DataFrame:
        return pd.DataFrame()

    def load_survey_data(
        self,
        pids: list[str],
        survey_type: str,
        datasets: list[str] | None = None
    ) -> pd.DataFrame:
        return pd.DataFrame()
