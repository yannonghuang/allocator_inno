from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/allocator"
    csv_root_path: str = "csv"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
