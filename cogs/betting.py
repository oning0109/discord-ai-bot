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
    
    embed.add_field(
        name="🅰️ A팀", 
        value="\n".join(team_a) if team_a else "선수 없음", 
        inline=True
    )
    embed.add_field(
        name="🅱️ B팀", 
        value="\n".join(team_b) if team_b else "선수 없음", 
        inline=True
    )
    return embed


class Betting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # 1. /배팅 (열린 베팅 판 조회 및 베팅 진행)
    @app_commands.command(name="배팅", description="현재 열린 내전 경기에 베팅을 진행합니다.")
    @app_commands.describe(
        market_id="베팅할 경기 ID",
        side="베팅할 팀 (A 또는 B)",
        amount="베팅할 칩 금액"
    )
    async def bet_command(
        self, 
        interaction: discord.Interaction, 
        market_id: int, 
        side: str, 
        amount: int
    ):
        await interaction.response.defer(ephemeral=True)
        
        side_upper = side.upper()
        if side_upper not in ["A", "B"]:
            await interaction.followup.send("❌ 베팅 팀은 'A' 또는 'B'로 입력해 주세요.", ephemeral=True)
            return

        try:
            payload = {
                "p_market_id": market_id,
                "p_discord_id": str(interaction.user.id),
                "p_side": side_upper,
                "p_amount": amount,
                "p_min": MIN_BET,
                "p_max": MAX_BET
            }
            res = await rpc("bet_place", payload)
            
            if isinstance(res, dict) and not res.get("success", False):
                err_msg = explain_bet_failure(res)
                await interaction.followup.send(err_msg, ephemeral=True)
            else:
                await interaction.followup.send(
                    f"✅ **{market_id}번 경기** {side_upper}팀에 **{amount:,} 칩** 베팅이 완료되었습니다!", 
                    ephemeral=True
                )
        except Exception as e:
            await interaction.followup.send(f"❌ 베팅 처리 중 오류가 발생했습니다: {e}", ephemeral=True)

    # 2. /칩 (보유 칩 잔액 확인)
    @app_commands.command(name="칩", description="현재 보유 중인 칩 잔액을 조회합니다.")
    async def chip_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            # DBManager를 통한 칩 조회 (main.py의 bot.db 활용)
            chips = await self.bot.db.get_user_chips(interaction.user.id)
            await interaction.followup.send(
                f"🪙 {interaction.user.mention} 님의 현재 보유 칩: **{chips:,}개**", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 칩 조회 중 오류가 발생했습니다: {e}", ephemeral=True)

    # 3. /배팅시작 (새로운 베팅 판 생성 - 관리자/호스트용)
    @app_commands.command(name="배팅시작", description="현재 진행 중인 내전의 베팅 판을 개설합니다.")
    async def start_bet_command(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            guild_id = str(interaction.guild_id) if interaction.guild_id else ""
            markets = await rpc("bet_list_open_markets", {"p_guild_id": guild_id})
            
            if not markets:
                await interaction.followup.send("⚠️ 현재 개설 가능한 내전 경기가 없습니다.")
                return

            latest_market = markets[0]
            embed = build_market_embed(latest_market)
            await interaction.followup.send(
                content="🎰 **새로운 내전 베팅이 개설되었습니다!** `/배팅` 커맨드로 참여해 주세요.", 
                embed=embed
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 베팅 시작 처리 중 오류가 발생했습니다: {e}")

    # 4. /출금요청 (칩 출금 요청)
    @app_commands.command(name="출금요청", description="보유한 칩 출금을 요청합니다.")
    @app_commands.describe(amount="출금 요청할 칩 수량")
    async def withdraw_command(self, interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ 1개 이상의 칩을 입력해 주세요.", ephemeral=True)
            return

        try:
            user_chips = await self.bot.db.get_user_chips(interaction.user.id)
            if user_chips < amount:
                await interaction.followup.send("❌ 보유한 칩보다 많은 금액은 출금할 수 없습니다.", ephemeral=True)
                return

            # DBManager의 출금 요청 처리 함수 호출
            await self.bot.db.request_withdrawal(interaction.user.id, amount)
            await interaction.followup.send(
                f"💸 **{amount:,} 칩** 출금 요청이 정상적으로 접수되었습니다. 관리자 승인을 기다려주세요.", 
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 출금 요청 처리 중 오류가 발생했습니다: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Betting(bot))
