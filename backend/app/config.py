from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    google_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    database_url: str = "sqlite:///./doctor_assistant.db"
    frontend_origin: str = "http://localhost:5173"
    voice_mode: str = "demo"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
