"""삭발봇 설정 — 서버마다 바뀌는 값은 전부 여기(또는 .env)에서 관리"""
import os

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int = 0) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else default


# ── 필수 ──────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
# 봇은 서버에서만 돌아가므로 service_role 키를 쓴다 (봇 전용 DB 함수는 이 키로만 실행 가능)
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY 환경변수가 설정되지 않았습니다.")

PREFIX = os.getenv("DEFAULT_PREFIX", "!")

# ── 서버 / 권한 ───────────────────────────────────────
GUILD_ID = _int_env("GUILD_ID")  # 비우면 봇이 들어가 있는 첫 서버 기준
OWNER_IDS = {
    449924737561395201,
    658181149973282837,
}
LOG_CHANNEL_ID = _int_env("LOG_CHANNEL_ID")  # 멤버 동기화/삭제 알림 (비우면 콘솔에만)

# ── 채널 ──────────────────────────────────────────────
BIRTHDAY_CHANNEL_ID = 1557866992512409650
VOICE_CHANNEL_A_ID = 1558806907706282004
VOICE_CHANNEL_B_ID = 1558807423983292579

# ── 종족 역할 → team_id ───────────────────────────────
ROLE_TEAM_MAP = {
    1556323926693642312: "chiikawa",
    1556324021979713536: "hachiware",
    1556324340784431217: "usagi",
    1556324066326216704: "kurimanju",
    1556324215563493437: "momonga",
    1556324296731660350: "shisa",
    1558124747487973436: "rakko",
}

# ── 일정 포럼 태그 ────────────────────────────────────
STATE_TAG_IDS = {
    "모집중": 1559905662320906345,
    "정원마감": 1559905662320906346,
    "모집마감": 1559905662320906347,
    "종료": 1559905662320906348,
}
GAME_TAG_IDS = {
    "발로란트": 1559905959185350727,
    "오버워치": 1559915462404612228,
    "종합게임": 1559912489053978717,
    "술캠": 1559924340961710141,
    "공구": 1559926535748853932,
}

# ── 멤버 동기화 ───────────────────────────────────────
# 서버를 나간 사람은 바로 지우지 않고 이 기간이 지나면 DB에서 삭제 (0 = 다음 정리 때 바로 삭제)
LEAVE_PURGE_DAYS = _int_env("LEAVE_PURGE_DAYS", 7)
CHAT_FLUSH_SECONDS = 15  # 채팅 수를 모아서 DB에 반영하는 주기

# ── 내전 ──────────────────────────────────────────────
OW_MMR_STEP = 20            # 옵치 경기 1회당 포지션 MMR ±
RESHUFFLE_TOLERANCE = 30    # 다시 섞기 때 허용하는 팀 평균 MMR 차이 여유
LOBBY_EXPIRE_HOURS = 48     # 이 시간이 지난 대기실/결과 화면은 정리

# ── 오버워치 인게임 랭크 (OverFast API) ───────────────
OVERFAST_URL = os.getenv("OVERFAST_URL", "https://overfast-api.tekrop.fr").rstrip("/")
OW_RANK_REFRESH_HOUR = 5    # 매일 이 시각(KST)에 연동된 배틀태그 랭크 갱신
