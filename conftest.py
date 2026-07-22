"""Root conftest: set environment defaults before any project module is imported."""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_PATH", "data/test.db")
os.environ.setdefault("WHATSAPP_TOKEN", "test-token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("GOOGLE_SHEETS_ID", "test-sheet-id")
os.environ.setdefault("GOOGLE_PRICES_SHEET_ID", "test-prices-id")
os.environ.setdefault("GOOGLE_CREDENTIALS_PATH", "credentials/service_account.json")
os.environ.setdefault("CHATWOOT_URL", "http://localhost:3000")
os.environ.setdefault("CHATWOOT_BOT_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "test@test.com")
os.environ.setdefault("GOOGLE_CALENDAR_SUBJECT", "test@test.com")
