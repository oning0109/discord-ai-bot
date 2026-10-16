"""
🎰 내전 베팅 시스템 (관전자용)

- 칩 충전: LeaderBoard 경험치샵에서 "칩 역할"을 구매 → 이 봇이 역할 부여를 감지해 칩을 입금하고 역할을 회수
- 경기 확정 시 베팅 마켓이 자동으로 열리고(custom_game.py 연동), 관전자는 /배팅 으로 경기를 골라 참여
- 이기면 건 칩 x 배율(기본 2.0~3.0배) 지급, 지면 건 칩은 소멸
- 출금: /출금 으로 신청 → 관리자가 LeaderBoard /경험치추가 로 지급한 뒤 관리자 페이지에서 완료 처리

⚠️ 이 모듈은 Supabase service_role 키(SUPABASE_SERVICE_KEY)가 필요합니다.
   - 서버(봇) 환경변수에만 넣고, GitHub·웹사이트 코드에는 절대 넣지 마세요.
   - 키가 없으면 베팅 기능만 꺼진 채로 봇은 정상 동작합니다.
"""
import asyncio
import os
import re
from typing import Optional

import discord
import requests
from discord.ext import commands, tasks
from dotenv import load_dotenv

# .env 파일에서 환경변수 로드
load_dotenv()

# ---------------------------------------------------------------
# ⚙️ 설정
# ---------------------------------------------------------------
SUPABASE_URL = os.getenv(
    "SUPABASE_URL",
    "https://tdatedpggtutuqrywguk.supabase.co",
)

# .env 파일의 SUPABASE_SERVICE_KEY를 가져옵니다.
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRkYXRlZHBnZ3R1dHVxcnl3Z3VrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTE3NzY3NjUsImV4cCI6MjEwNzM1Mjc2NX0.QuXGuebYHDGc-2x7rZG-NZSw8Xz7Y-qU4D18HJQH2gg").strip()

# 칩 금액 : LeaderBoard 경험치샵 상품에 연결한 "칩 역할"의 ID (0 = 미설정)
CHIP_ROLE_IDS = {
    1000: 1560617436892504084,
    5000: 1560617503212834859,
    10000: 1560617589665829005,
    100000: 1560617544337858571,
}

BET_WINDOW_MINUTES = 10
WIN_MULTIPLIER = 3.0
MIN_BET = 100
MAX_BET = 100_000
MIN_WITHDRAW = 1000
BET_ADMIN_CHANNEL_ID = 0

ROLE_AMOUNTS = {rid: amount for amount, rid in CHIP_ROLE_IDS.items() if rid}
ENABLED = bool(SUPABASE_SERVICE_KEY)

# ---------------------------------------------------------------
# 🛠️ 베팅 헬퍼 함수
# ---------------------------------------------------------------
BET_REASON_TEXT = {
    "bad_amount": "❌ 올바른 숫자를 입력해 주세요.",
    "market_closed": "❌ 이미 베팅이 마감된 경기예요.",
    "insufficient_balance": "❌ 보유한 칩이 부족해요.",
    "blocked_user": "❌ 경기 참가자나 호스트는 이 경기에 베팅할 수 없어요.",
}

def explain_bet_failure(res: dict) -> str:
    reason = res.get("reason", "")
    return BET_REASON_TEXT.get(reason, f"❌ 베팅 실패: {reason}")

def team_to_json(team: list) -> list:
    return [{"id": str(p["member"].id), "name": p["member"].display_name} for p in team if "member" in p]

def build_market_embed(market: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎰 {market.get('game', '내전')} #{market.get('id')} 베팅",
        color=discord.Color.blue()
    )
    names_a = [x["name"] for x in market.get("team_a", [])]
    names_b = [x["name"] for x in market.get("team_b", [])]
    embed.add_field(name="🔵 A팀", value=", ".join(names_a) if names_a else "없음", inline=True)
    embed.add_field(name="🔴 B팀", value=", ".join(names_b) if names_b else "없음", inline=True)
    return embed


# ---------------------------------------------------------------
# 🔌 Supabase RPC (service_role)
# ---------------------------------------------------------------
class BetError(Exception):
    pass


def _rpc_sync(name: str, payload: dict):
    res = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        json=payload,
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    if res.status_code >= 400:
        raise BetError(f"{name}: HTTP {res.status_code} {res.text[:300]}")
    return res.json() if res.text else None


async def rpc(name: str, payload: dict):
    return await asyncio.to_thread(_rpc_sync, name, payload)


# ---------------------------------------------------------------
# 🧮 배율 계산 / 표시 헬퍼
# ---------------------------------------------------------------
def compute_multipliers(team_a: list, team_b: list):
    """양 팀 모두 적중 시 배팅액의 3배를 지급합니다."""
    return WIN_MULTIPLIER, WIN_MULTIPLIER

# ---------------------------------------------------------------
# 🖥️ /배팅 UI
# ---------------------------------------------------------------
class BetAmountModal(discord.ui.Modal):
    def __init__(self, user_id: int, market_id: int, side: str):
        side_label = "A팀" if side == "A" else "B팀"
        super().__init__(title=f"{side_label}에 베팅하기")
        self.user_id = user_id
        self.market_id = market_id
        self.side = side
        self.amount = discord.ui.TextInput(
            label="베팅 금액 (칩)",
            placeholder=f"예: 5000  (최소 {MIN_BET:,} / 최대 {MAX_BET:,})",
            min_length=1,
            max_length=12,
            required=True,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        digits = re.sub(r"[^\d]", "", str(self.amount.value))
        try:
            if not digits:
                result_text = BET_REASON_TEXT["bad_amount"]
            else
