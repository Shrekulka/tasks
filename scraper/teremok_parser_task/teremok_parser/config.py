from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфігурація середовища виконання парсера."""

    app_name: str = "teremok-parser"
    log_level: str = "INFO"
    debug: bool = False

    base_url: str = "https://teremok.org.ua"
    source_timezone: str = "Europe/Kyiv"

    request_timeout: float = 15.0
    request_delay_base: float = 0.5
    request_delay_jitter: float = 0.3

    retry_total: int = 3
    retry_backoff_factor: float = 0.8
    max_consecutive_errors: int = 8

    target_total: int = 200
    min_per_category: int = 10
    min_categories_required: int = 5
    candidates_per_category: int = 120
    max_pages_per_category: int = 10

    output_dir: Path = Path("output")
    log_dir: Path = Path("logs")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TEREMOK_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_quota_target_invariants(self) -> "Settings":
        required_quota_sum = self.min_per_category * self.min_categories_required
        if required_quota_sum > self.target_total:
            raise ValueError(
                f"Некоректна конфігурація: min_per_category ({self.min_per_category}) * "
                f"min_categories_required ({self.min_categories_required}) = {required_quota_sum} "
                f"перевищує target_total ({self.target_total})."
            )
        return self


settings = Settings()
