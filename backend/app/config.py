from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/allocator"
    csv_root_path: str = "csv"
    # Planning copilot: set OPENAI_API_KEY in env so the backend can use the LLM for intent parsing.
    openai_api_key: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # Ensure OPENAI_API_KEY (common env name) is read into openai_api_key
        extra = "ignore"


settings = Settings()
