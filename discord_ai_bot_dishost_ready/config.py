import os

# Discord bot token is supplied through the DISCORD_TOKEN environment variable.
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")

DB_PATH = os.getenv("DB_PATH", "mmr_data.db")
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", "!")
