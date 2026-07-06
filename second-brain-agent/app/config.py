# app/config.py
import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # Gmail configurations
    GMAIL_EMAIL = os.getenv("GMAIL_EMAIL", "")
    GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "")  # Needs to be an App Password

    # Database configurations (Turso or local fallback)
    TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
    TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
    LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", "second_brain.db")

    # Gemini credentials and models
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-004")

    # Server port
    PORT = int(os.getenv("PORT", "8080"))

    @classmethod
    def validate(cls):
        """Validates that minimum required variables are configured."""
        warnings = []
        if not cls.GMAIL_EMAIL or not cls.GMAIL_PASSWORD:
            warnings.append(
                "GMAIL_EMAIL and GMAIL_PASSWORD are not fully configured. Email ingestion will be disabled or run in dry-run mode."
            )
        if not cls.TURSO_DATABASE_URL:
            warnings.append(
                "TURSO_DATABASE_URL is not set. Falling back to local SQLite database."
            )
        if not cls.GEMINI_API_KEY:
            warnings.append(
                "GEMINI_API_KEY is not set. Vertex AI Application Default Credentials (ADC) will be used if available."
            )
        return warnings
