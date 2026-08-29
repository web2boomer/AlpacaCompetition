import os
import shlex
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from money_machine.domain.enums import AccountRole, AppEnvironment, RunMode

ALLOWED_LOCAL_ENV_FILES = {".env.development.local", ".env.competition.local"}
PAPER_HOST = "paper-api.alpaca.markets"


def load_local_environment(path: Path) -> None:
    if path.name not in ALLOWED_LOCAL_ENV_FILES:
        raise ValueError("only role-specific .env*.local files may be loaded")
    if not path.is_file():
        raise FileNotFoundError(path)
    load_dotenv(path, override=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    account_role: AccountRole = AccountRole.DEVELOPMENT
    run_mode: RunMode = RunMode.REPLAY
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_paper_trade: bool = True
    alpaca_expected_account_id: SecretStr | None = None
    apca_api_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_toolsets: str = "account,trading,assets,stock-data,options-data,news"
    client_order_prefix: str = Field(default="mm-dev", pattern=r"^[a-zA-Z0-9_-]{2,12}$")
    database_url: str = "sqlite:///./money_machine.db"
    model_provider: str = "replay"
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6"
    mcp_command: str = "alpaca-mcp-server"
    mcp_args: str = ""
    log_level: str = "INFO"
    mission_control_url: str | None = None
    mission_control_project: str = "alpaca-competition"
    mission_control_token: SecretStr | None = None
    mission_control_reporting_interval_minutes: int = Field(default=60, ge=1)
    mission_control_environment: str | None = None

    @model_validator(mode="after")
    def fail_closed_configuration(self) -> "Settings":
        if "EXECUTION_ENABLED" in os.environ:
            raise ValueError(
                "EXECUTION_ENABLED is forbidden; authority is derived from safety state"
            )
        if "MISSION_CONTROL_REPORTING_ENABLED" in os.environ:
            raise ValueError(
                "MISSION_CONTROL_REPORTING_ENABLED is forbidden; scheduling controls reporting"
            )
        valid_pair = {
            AppEnvironment.DEVELOPMENT: AccountRole.DEVELOPMENT,
            AppEnvironment.PRODUCTION: AccountRole.COMPETITION,
        }
        if valid_pair[self.app_env] is not self.account_role:
            raise ValueError("APP_ENV and ACCOUNT_ROLE mapping is invalid")
        if not self.alpaca_paper_trade:
            raise ValueError("live Alpaca trading is forbidden")
        parsed = urlparse(self.apca_api_base_url)
        if parsed.scheme != "https" or parsed.hostname != PAPER_HOST:
            raise ValueError("only the Alpaca paper API endpoint is permitted")
        return self

    @property
    def mcp_arguments(self) -> list[str]:
        return shlex.split(self.mcp_args)

    def assert_live_credentials_present(self) -> None:
        missing = [
            name
            for name, value in (
                ("ALPACA_API_KEY", self.alpaca_api_key),
                ("ALPACA_SECRET_KEY", self.alpaca_secret_key),
                ("ALPACA_EXPECTED_ACCOUNT_ID", self.alpaca_expected_account_id),
            )
            if value is None or not value.get_secret_value()
        ]
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))

    def redacted_status(self) -> dict[str, str]:
        return {
            "APP_ENV": self.app_env,
            "ACCOUNT_ROLE": self.account_role,
            "ALPACA_API_KEY": "present" if self.alpaca_api_key else "missing",
            "ALPACA_SECRET_KEY": "present" if self.alpaca_secret_key else "missing",
            "ALPACA_EXPECTED_ACCOUNT_ID": (
                "present" if self.alpaca_expected_account_id else "missing"
            ),
            "ALPACA_PAPER_TRADE": "true" if self.alpaca_paper_trade else "false",
            "APCA_API_BASE_URL": PAPER_HOST,
        }
