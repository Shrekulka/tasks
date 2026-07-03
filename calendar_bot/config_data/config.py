# config_data/config.py

from dataclasses import dataclass, field
from typing import Optional

from environs import Env


@dataclass
class TgBot:
    """Конфигурация Telegram-бота."""
    token: str                # Токен от @BotFather
    admin_ids: list[int]      # ID администраторов, которые модерируют заявки


@dataclass
class DatabaseConfig:
    """Конфигурация PostgreSQL."""
    db_name: str
    db_host: str
    db_port: int
    db_user: str
    db_password: str

    @property
    def dsn(self) -> str:
        """
        DSN для асинхронного движка SQLAlchemy (asyncpg).
        Пример: postgresql+asyncpg://user:pass@host:5432/dbname
        """
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@dataclass
class GoogleCalendarConfig:
    """Конфигурация интеграции с Google Calendar (Service Account)."""
    credentials_file: str          # Путь к JSON-ключу сервисного аккаунта
    calendar_id: str               # ID целевого календаря
    timezone: str                  # Например, "Europe/Kyiv"
    reminder_offsets_minutes: list[int]   # За сколько минут напоминать, напр. [60, 15]
    reminder_check_interval: int   # Интервал опроса календаря (сек.)


@dataclass
class WebhookConfig:
    """Конфигурация веб-сервера и вебхука Telegram."""
    web_server_host: str
    web_server_port: int
    webhook_path: str
    webhook_base_url: str
    webhook_secret: str

    @property
    def webhook_url(self) -> str:
        """Полный URL, который нужно зарегистрировать в Telegram через set_webhook."""
        return f"{self.webhook_base_url.rstrip('/')}{self.webhook_path}"


@dataclass
class Config:
    """Общая конфигурация приложения."""
    tg_bot: TgBot
    db: DatabaseConfig
    google_calendar: GoogleCalendarConfig
    webhook: WebhookConfig

    _env: Env = field(default_factory=Env)

    def __init__(self, env_path: Optional[str] = None) -> None:
        self._env = Env()
        self._env.read_env(env_path)
        env = self._env

        self.tg_bot = TgBot(
            token=env("BOT_TOKEN"),
            admin_ids=list(map(int, env.list("ADMIN_IDS"))),
        )

        self.db = DatabaseConfig(
            db_name=env("DB_NAME"),
            db_host=env("DB_HOST"),
            db_port=env.int("DB_PORT", 5432),
            db_user=env("DB_USER"),
            db_password=env("DB_PASSWORD"),
        )

        self.google_calendar = GoogleCalendarConfig(
            credentials_file=env("GOOGLE_CREDENTIALS_FILE"),
            calendar_id=env("GOOGLE_CALENDAR_ID"),
            timezone=env("TIMEZONE", "Europe/Kyiv"),
            reminder_offsets_minutes=list(
                map(int, env.list("REMINDER_OFFSETS_MINUTES", ["60", "15"]))
            ),
            reminder_check_interval=env.int("REMINDER_CHECK_INTERVAL", 300),
        )

        self.webhook = WebhookConfig(
            web_server_host=env("WEB_SERVER_HOST", "0.0.0.0"),
            web_server_port=env.int("WEB_SERVER_PORT", 8080),
            webhook_path=env("WEBHOOK_PATH", "/webhook"),
            webhook_base_url=env("WEBHOOK_BASE_URL"),
            webhook_secret=env("WEBHOOK_SECRET"),
        )

    def __str__(self) -> str:
        return (
            f"TgBot: {self.tg_bot}\n"
            f"DatabaseConfig: {self.db}\n"
            f"GoogleCalendarConfig: {self.google_calendar}\n"
            f"WebhookConfig: {self.webhook}"
        )


config: Config = Config()