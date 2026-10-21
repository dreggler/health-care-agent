import logging
import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):

    OPENAI_API_KEY:           str | None = None
    ANTHROPIC_API_KEY:        str | None = None
    GOOGLE_API_KEY:           str | None = None
    GEMINI_API_KEY:           str | None = None
    DEEPSEEK_API_KEY:         str | None = None
    GROQ_API_KEY:             str | None = None
    XAI_API_KEY:              str | None = None
    OPENROUTER_API_KEY:       str | None = None
    PERPLEXITY_API_KEY:       str | None = None
    MISTRAL_API_KEY:          str | None = None
    MINIMAX_API_KEY:          str | None = None
    ZHIPUAI_API_KEY:          str | None = None
    CEREBRAS_API_KEY:         str | None = None
    AI_GATEWAY_API_KEY:       str | None = None
    ZAI_API_KEY:              str | None = None
    MINIMAX_CN_API_KEY:       str | None = None
    HF_TOKEN:                 str | None = None
    OPENCODE_API_KEY:         str | None = None
    KIMI_API_KEY:             str | None = None
    ANTHROPIC_OAUTH_TOKEN:    str | None = None
    VLLM_API_KEY:             str | None = None

    GOOGLE_CLOUD_API_KEY:     str | None = None
    GOOGLE_CLOUD_PROJECT:     str | None = None
    GOOGLE_CLOUD_LOCATION:    str           = "us-central1"

    AWS_ACCESS_KEY_ID:        str | None = None
    AWS_SECRET_ACCESS_KEY:    str | None = None
    AWS_REGION:               str           = "us-east-1"

    AZURE_OPENAI_ENDPOINT:    str | None = None
    AZURE_OPENAI_API_KEY:     str | None = None
    AZURE_OPENAI_API_VERSION: str           = "2024-12-01-preview"

    VLLM_BASE_URL:            str           = "http://localhost:8421/v1"
    VLLM_MODEL:               str           = "gemma-4-26b-a4b"

    DEFAULT_LLM_PROVIDER: str = "gemini"
    DEFAULT_MODEL:        str = "gemini-3-flash-preview"
    FALLBACK_LLM_PROVIDER: str | None = None
    FALLBACK_LLM_MODEL:    str | None = None

    DEFAULT_MODEL_GEMINI:         str = "gemini-3-flash-preview"
    DEFAULT_MODEL_OPENAI:         str = "gpt-5.4-mini"
    DEFAULT_MODEL_AZURE_OPENAI:   str = "gpt-5-mini"
    DEFAULT_MODEL_ANTHROPIC:      str = "claude-sonnet-4-6"
    DEFAULT_MODEL_MISTRAL:        str = "mistral-medium-latest"
    DEFAULT_MODEL_GROQ:           str = "llama-3.3-70b-versatile"
    DEFAULT_MODEL_DEEPSEEK:       str = "deepseek-flash"
    DEFAULT_MODEL_XAI:            str = "grok-4-1-fast-reasoning"
    DEFAULT_MODEL_OPENROUTER:     str = "anthropic/claude-sonnet-4-6"
    DEFAULT_MODEL_PERPLEXITY:     str = "sonar-pro"
    DEFAULT_MODEL_GOOGLE_VERTEX:  str = "gemini-3-flash-preview"
    DEFAULT_MODEL_AMAZON_BEDROCK: str = "amazon.nova-pro-v1:0"
    DEFAULT_MODEL_MINIMAX:        str = "MiniMax-M2"
    DEFAULT_MODEL_ZHIPUAI:        str = "glm-4.7"

    OPENAI_REASONING_EFFORT: str | None = "minimal"

    DEEPSEEK_THINKING:        str | None = "disabled"
    DEEPSEEK_REASONING_EFFORT: str | None = None
    DEEPSEEK_MAX_TOKENS: int = 32768

    LLM_STREAM_STALL_TIMEOUT: float = 90.0
    LLM_FALLBACK_COOLDOWN_SECONDS: float = 600.0

    AUTO_RESTORE_AGENT:   bool = False
    AGENT_MAX_ITERATIONS: int  = 100
    AGENT_CONTEXT_WINDOW_SIZE: int = 20
    AGENT_MAX_TOKENS:     int  = 0
    AGENT_MAX_CONTEXT_TOKENS: int = 180_000
    CHAT_MAX_TURNS:       int  = 20
    CHAT_HISTORY_SIZE:    int  = 20
    AGENT_CONTEXT_WINDOW_HOURS: int = 24
    FACT_VERIFY_LLM_PROVIDER: str = ""
    FACT_VERIFY_LLM_MODEL:    str = ""

    CODE_TOOL_DOCKER_SANDBOX: bool = False
    CODE_TOOL_DOCKER_IMAGE:   str  = "python:3.12-slim"

    DATA_SOURCE: str = "live"

    DEFAULT_USER_LANGUAGE: str = "en"

    DATA_RETENTION_DAYS: int = 30

    DATA_STORE_PATH:        Path = Path("./data/data_stores")
    AGENT_LOGS_PATH:        Path = Path("./logs")
    LLM_USAGE_CSV_PATH:     Path | None = None
    MEMORY_DB_PATH:         Path = Path("./memory")
    AGENT_STATES_PATH:      Path = Path("./memory/agent_states")
    AGENT_LAST_CONFIG_PATH: Path = Path("./memory/agent_last_config.json")
    APP_STATE_PATH:         Path = Path("./memory/app_state.json")

    API_HOST: str   = "0.0.0.0"
    API_PORT: int   = 8000
    TIMEZONE: str   = "UTC"
    SCHEDULED_FRESHNESS_MAX_AGE_MIN: float = 45
    SCHEDULED_FRESHNESS_RECHECK_MIN: float = 10
    SCHEDULED_FRESHNESS_MAX_WAIT_MIN: float = 120
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
    ]
    API_AUTH_TOKEN: str | None = None
    TRUST_PROXY_HEADERS: bool = False
    DASHBOARD_URL: str | None = None
    API_URL:       str | None = None
    WATCH_URL:     str | None = None

    TUNNEL_DIR:    str | None = None
    TUNNEL_SCRIPT: str | None = None

    CHAT_ID:        str | None = None
    TELEGRAM_TOKEN: str | None = None

    TELEGRAM_GATEWAY_ENABLED: bool = False
    TELEGRAM_POLL_TIMEOUT:    int  = 30
    TELEGRAM_ALLOWED_CHAT_IDS: str = ""
    TELEGRAM_GROUP_LINK: str | None = None

    FEISHU_GATEWAY_ENABLED: bool = False
    FEISHU_APP_ID:          str  = ""
    FEISHU_APP_SECRET:      str  = ""
    FEISHU_TRANSPORT:       str  = "ws"
    FEISHU_WEBHOOK_PATH:    str  = "/api/feishu/webhook"
    FEISHU_VERIFICATION_TOKEN: str = ""
    FEISHU_ENCRYPT_KEY:     str  = ""
    FEISHU_DEFAULT_CHAT_ID: str  = ""
    FEISHU_ALLOWED_CHAT_IDS: str = ""

    WEIXIN_GATEWAY_ENABLED: bool = False
    WEIXIN_BOT_TOKEN_PATH:  str  = "./data/weixin_bot_token.json"
    WEIXIN_DEFAULT_USER_ID: str  = ""
    WEIXIN_ALLOWED_USER_IDS: str = ""

    IOS_GATEWAY_ENABLED: bool = True
    IOS_VISION_ENABLED: bool = False
    IOS_MAX_IMAGE_BYTES: int = 5 * 1024 * 1024

    APNS_ENABLED:   bool = False
    APNS_KEY_PATH:  str  = ""
    APNS_KEY_ID:    str  = ""
    APNS_TEAM_ID:   str  = ""
    APNS_BUNDLE_ID: str  = ""
    APNS_ENV:       str  = "production"

    HIME_SKILLS_DIR: str = "./skills"


    def validate_llm_keys(self) -> bool:
        keys = [
            self.OPENAI_API_KEY,
            self.ANTHROPIC_API_KEY,
            self.GEMINI_API_KEY,
            self.GOOGLE_API_KEY,
            self.DEEPSEEK_API_KEY,
            self.GROQ_API_KEY,
            self.XAI_API_KEY,
            self.OPENROUTER_API_KEY,
            self.PERPLEXITY_API_KEY,
            self.MISTRAL_API_KEY,
            self.MINIMAX_API_KEY,
            self.ZHIPUAI_API_KEY,
            self.AZURE_OPENAI_API_KEY,
            self.AWS_ACCESS_KEY_ID,
        ]
        if not any(keys):
            logger.warning(
                "No LLM API keys configured. Agent features will not work. "
                "Set at least one key in .env (e.g. GEMINI_API_KEY, OPENAI_API_KEY)."
            )
            return False
        return True

    def validate_timezone(self) -> bool:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(self.TIMEZONE)
            return True
        except Exception as e:
            logger.warning(
                "TIMEZONE=%r is not a valid IANA timezone (%s); falling back to UTC. "
                "Set TIMEZONE in .env to e.g. Europe/London, America/New_York, Asia/Shanghai.",
                self.TIMEZONE, e,
            )
            return False

    @field_validator("TELEGRAM_POLL_TIMEOUT")
    @classmethod
    def _clamp_poll_timeout(cls, v: int) -> int:
        return max(1, v)

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )


def _export_dotenv_to_environ(path: str = ".env") -> int:
    try:
        from dotenv import dotenv_values
    except ImportError:
        return 0
    try:
        values = dotenv_values(path)
    except Exception as exc:
        logger.warning("Could not read %s for environment export: %s", path, exc)
        return 0
    exported = 0
    for key, val in values.items():
        if key and val and key not in os.environ:
            os.environ[key] = val
            exported += 1
    return exported


settings = Settings()
_export_dotenv_to_environ()
