from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    backend_database_url: str
    enterprise_database_url: str = "postgresql://insightflow:insightflow@postgres:5432/insightflow"
    semantic_dir: Path = Path("/semantic")
    evals_dir: Path = Path("/evals")
    sql_statement_timeout_ms: int = 5_000
    insightflow_llm_mode: str = "offline_rules"
    insightflow_runtime_version: str = "v17"
    insightflow_v16_fallback_owner: str = ""
    insightflow_v16_fallback_reason: str = ""
    insightflow_v16_fallback_until: str = ""
    insightflow_runtime_ports: str = "memory"
    insightflow_redis_url: str = "redis://localhost:6379/0"
    insightflow_redis_namespace: str = "insightflow"
    insightflow_runtime_ttl_seconds: int = 3600
    insightflow_telemetry: str = "noop"
    insightflow_temporal_target: str = "localhost:7233"
    insightflow_temporal_namespace: str = "default"
    insightflow_temporal_task_queue: str = "clinical-v20"
    insightflow_temporal_enabled: bool = False
    insightflow_approval_resume_secret: str = ""
    insightflow_approval_resume_ttl_seconds: int = 900
    insightflow_temporal_outbox_max_attempts: int = 5
    insightflow_temporal_outbox_poll_seconds: float = 5.0
    insightflow_temporal_outbox_batch_size: int = 20
    insightflow_ingestion_provider: str = "rules"
    insightflow_auth_mode: str = "development"
    insightflow_llm_timeout_seconds: float = 60
    insightflow_llm_max_retries: int = 2
    insightflow_llm_max_output_tokens: int = 2000
    insightflow_llm_max_total_tokens: int = 30000
    openai_api_key: str = ""
    openai_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    zhipu_api_key: str = ""
    glm_model: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    moonshot_api_key: str = ""
    kimi_model: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    custom_llm_api_key: str = ""
    custom_llm_model: str = ""
    custom_llm_base_url: str = ""
    cdisc_pseudonym_salt: str = "local-development-change-me"
    clinical_inline_jobs: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

