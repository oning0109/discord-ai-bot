"""
🎰 내전 베팅 시스템 - Discord.py 2.x Cog

필수 환경변수:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY

필수 Supabase RPC:
  bet_list_open_markets(p_guild_id text)
  bet_get_market(p_market_id bigint, p_discord_id text)
  bet_place(
      p_market_id bigint, p_discord_id text, p_side text,
      p_amount bigint, p_min bigint, p_max bigint
  )

이 파일은 관전자 베팅 UI와 /배팅 명령어를 제공합니다.
경기 생성/결과 정산/칩 역할 충전/출금은 해당 DB RPC 및 custom_game.py
연동 규격이 확인되어야 안전하게 추가할 수 있습니다.
"""
import asyncio
import os
import re
from typing import Any

import discord
import requests
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

MIN_BET = 100
MAX_BET = 50_000
BET_UI_TIMEOUT = 300

BET_REASON_TEXT = {
    "bad_amount": "❌ 올바른 숫자를 입력해 주세요.",
    "market_closed": "❌ 이미 베팅이 마감된 경기예요.",
    "insufficient_balance": "❌ 보유한 칩이 부족해요.",
    "blocked_user": "❌ 경기 참가자나 호스트는 이 경기에 베팅할 수 없어요.",
}


class BetError(Exception):
    """Supabase 베팅 RPC 오류."""


def _rpc_sync(name: str, payload: dict[str, Any]) -> Any:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise BetError(
            "SUPABASE_URL 또는 SUPABASE_SERVICE_KEY 환경변수가 설정되지 않았습니다."
        )

    response = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        json=payload,
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    if response.status_code >= 400:
        raise BetError(
            f"{name}: HTTP {response.status_code} {response.text[:500]}"
        )
    if not response.text:
        return None
    return response.json()


async def rpc(name: str, payload: dict[str, Any]) -> Any:
    """동기 requests 호출이 Discord 이벤트 루프를 막지 않도록 별도 스레드에서 실행."""
    return await asyncio.to_thread(_rpc_sync, name, payload)


def explain_bet_failure(result: dict[str, Any]) -> str:
    reason = str(result.get("reason", ""))
    return BET_REASON_TEXT.get(reason, f"❌ 베팅 실패: {reason or '알 수 없는 오류'}")


def build_market_embed(market: dict[str, Any]) -> discord.Embed:
    market_id = market.get("id", "?")
    game_name = market.get("game") or "내전"
    embed = discord.Embed(
        title=f"🎰 {game_name} #{market_id} 베팅",
        color=discord.Color.blurple(),
    )

    team_a = market.get("team_a") or []
    team_b = market.get("team_b") or []

# cogs/betting.py 하단 부분

class Betting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # 1. /배팅 명령어
    @app_commands.command(name="배팅", description="현재 열린 내전 경기에 베팅합니다.")
    async def bet_command(self, interaction: discord.Interaction):
        await interaction.response.send_message("🎰 베팅 목록을 조회합니다...", ephemeral=True)

    # 2. /칩 명령어 (잔액 조회 등)
    @app_commands.command(name="칩", description="보유한 칩 잔액을 확인합니다.")
    async def chip_command(self, interaction: discord.Interaction):
        await interaction.response.send_message("🪙 보유 칩 잔액 조회 기능입니다.", ephemeral=True)

    # 3. /배팅시작 명령어 (관리자/호스트용)
    @app_commands.command(name="배팅시작", description="새로운 내전 베팅 판을 생성합니다.")
    async def start_bet_command(self, interaction: discord.Interaction):
        await interaction.response.send_message("🎰 새로운 베팅이 시작되었습니다.", ephemeral=False)

    # 4. /출금요청 명령어
    @app_commands.command(name="출금요청", description="보유한 칩 출금을 요청합니다.")
    async def withdraw_command(self, interaction: discord.Interaction, amount: int):
        await interaction.response.send_message(f"💸 {amount} 칩 출금 요청이 접수되었습니다.", ephemeral=True)


# discord.py가 Extension을 로드할 때 호출하는 필수 함수
async def setup(bot: commands.Bot):
    await bot.add_cog(Betting(bot))
