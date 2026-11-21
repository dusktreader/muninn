from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, AnyUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from muninn.constants import LogLevel, Environment


class ServiceSettings(BaseSettings):
    """Load service configuration from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="MUNINN_", env_file=".env", extra="ignore", validate_default=True
    )

    environment: Annotated[
        Environment,
        Field(description="Deployment environment name."),
    ] = Environment.LOCAL

    auth_mode: Annotated[
        Literal["oidc", "local"],
        Field(description="Authentication mode used by the API."),
    ] = "oidc"

    database_url: Annotated[
        str,
        Field(description="URL for the service database."),
    ] = "postgresql+psycopg://muninn@localhost:5432/muninn"

    object_store_endpoint: Annotated[
        AnyHttpUrl,
        Field(description="Endpoint URL for the object store."),
    ] = AnyHttpUrl("http://minio:9000")

    object_store_bucket: Annotated[
        str,
        Field(description="Bucket name used for object storage."),
    ] = "muninn"

    object_store_access_key: Annotated[
        str | None,
        Field(description="Access key for the object store."),
    ] = None

    object_store_secret_key: Annotated[
        str | None,
        Field(description="Secret key for the object store."),
    ] = None

    artifact_grant_secret: Annotated[
        str | None,
        Field(description="Signing secret for short-lived artifact access grants."),
    ] = None

    oidc_issuer_url: Annotated[
        AnyHttpUrl | None,
        Field(description="Expected issuer URL in OIDC tokens."),
    ] = None

    oidc_client_id: Annotated[
        str,
        Field(description="Client identifier used with the OIDC provider."),
    ] = "muninn"

    oidc_audience: Annotated[
        str,
        Field(description="Expected audience claim in OIDC tokens."),
    ] = "muninn-api"

    oidc_jwks_url: Annotated[
        AnyHttpUrl | None,
        Field(description="JWKS URL used to verify OIDC tokens."),
    ] = None

    provider_mode: Annotated[
        Literal["assemblyai", "fake"],
        Field(description="Processing provider selected for worker jobs."),
    ] = "assemblyai"

    assemblyai_api_base_url: Annotated[
        AnyHttpUrl,
        Field(description="Base URL for AssemblyAI speech-to-text requests."),
    ] = AnyHttpUrl("https://api.assemblyai.com")

    assemblyai_llm_base_url: Annotated[
        AnyHttpUrl,
        Field(description="Base URL for AssemblyAI LLM Gateway requests."),
    ] = AnyHttpUrl("https://llm-gateway.assemblyai.com/v1")

    assemblyai_api_key: Annotated[
        str | None,
        Field(description="API key used to access AssemblyAI."),
    ] = None

    assemblyai_transcription_model: Annotated[
        str,
        Field(description="AssemblyAI speech model used for transcription."),
    ] = "universal-3-5-pro"

    assemblyai_summary_model: Annotated[
        str,
        Field(description="AssemblyAI LLM Gateway model used for summarization."),
    ] = "gemma-4-31b"

    assemblyai_poll_interval_seconds: Annotated[
        float,
        Field(gt=0, description="Seconds between AssemblyAI transcript status checks."),
    ] = 3.0

    assemblyai_poll_timeout_seconds: Annotated[
        float,
        Field(gt=0, description="Maximum seconds to wait for an AssemblyAI transcript."),
    ] = 1800.0

    nats_url: Annotated[
        AnyUrl,
        Field(description="URL for the NATS messaging server."),
    ] = AnyUrl("nats://localhost:4222")

    log_level: Annotated[
        LogLevel,
        Field(description="Minimum log level emitted by the service."),
    ] = LogLevel.INFO

    @model_validator(mode="after")
    def validate_auth_mode(self) -> Self:
        """Validate authentication settings for the selected mode."""
        if self.auth_mode == "local" and self.environment != Environment.LOCAL:
            raise ValueError("Local authentication is only available in the local environment.")
        if self.auth_mode == "oidc":
            missing = [
                name
                for name, value in (
                    ("MUNINN_OIDC_ISSUER_URL", self.oidc_issuer_url),
                    ("MUNINN_OIDC_JWKS_URL", self.oidc_jwks_url),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"OIDC authentication requires {', '.join(missing)}.")
        return self


service_settings = ServiceSettings()
