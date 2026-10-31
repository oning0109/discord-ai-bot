"""
🎰 내전 베팅 시스템 (관전자용)
"""
import asyncio
import os
import re
from typing import Optional

import discord
import requests
from discord.ext import commands, tasks
from discord import app_commands
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

SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

# 칩 금액 : LeaderBoard 경험치샵 상품에 연결한 "칩 역할"의 ID (0 = 미설정)
CHIP_ROLE_IDS = {
    1000: 1560617436892504084,
    5000: 1560617503212834859,
    10000: 1560617589665829005,
    100000: 1560617544337858571,
}

BET_WINDOW_MINUTES = 10
WIN_MULTIPLIER = 1.5
MIN_BET = 100
MAX_BET = 50000
MIN_WITHDRAW = 1000

# 🧾 관리자 전용 채널 (출금 신청 알림이 전송되는 곳)
BET_ADMIN_CHANNEL_ID = 1556370974130184313

# 📢 일반 사람들에게 베팅 시작 안내와 @everyone 태그를 보낼 공개 채널 ID
BET_PUBLIC_ANNOUNCEMENT_CHANNEL_ID = 1557756931597402123

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

def compute_multipliers(team_a: list, team_b: list):
    return WIN_MULTIPLIER, WIN_MULTIPLIER

# ---------------------------------------------------------------
# 🖥️ /배팅 UI 컴포넌트
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
            else:
                res = await rpc("bet_place", {
                    "p_market_id": self.market_id,
                    "p_discord_id": str(interaction.user.id),
                    "p_side": self.side,
                    "p_amount": int(digits),
                    "p_min": MIN_BET,
                    "p_max": MAX_BET,
                })
                if res.get("ok"):
                    label = "🔵 A팀" if self.side == "A" else "🔴 B팀"
                    result_text = (
                        f"✅ {label}에 **{int(digits):,}**칩을 걸었어요! "
                        f"(누적 {int(res['my_amount']):,}칩 → 적중 시 **{int(res['payout_if_win']):,}**칩)"
                    )
                else:
                    result_text = explain_bet_failure(res)

            market = await rpc("bet_get_market", {"p_market_id": self.market_id, "p_discord_id": str(interaction.user.id)})
            await interaction.edit_original_response(
                content=result_text,
                embed=build_market_embed(market),
                view=BetPanelView(self.user_id, market),
            )
        except Exception as e:
            print(f"❌ [BetAmountModal 에러] {e}", flush=True)
            await interaction.followup.send("❌ 베팅 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)

class BetPanelView(discord.ui.View):
    def __init__(self, user_id: int, market: dict):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.market_id = int(market["id"])
        is_open = bool(market.get("open"))
        self.bet_a.disabled = not is_open
        self.bet_b.disabled = not is_open

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인이 연 베팅 화면만 조작할 수 있어요. `/배팅`을 직접 입력해 주세요.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="A팀에 걸기", style=discord.ButtonStyle.primary, emoji="🔵", row=0)
    async def bet_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetAmountModal(self.user_id, self.market_id, "A"))

    @discord.ui.button(label="B팀에 걸기", style=discord.ButtonStyle.danger, emoji="🔴", row=0)
    async def bet_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetAmountModal(self.user_id, self.market_id, "B"))

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            market = await rpc("bet_get_market", {"p_market_id": self.market_id, "p_discord_id": str(interaction.user.id)})
            await interaction.edit_original_response(content=None, embed=build_market_embed(market), view=BetPanelView(self.user_id, market))
        except Exception as e:
            print(f"❌ [BetPanelView.refresh 에러] {e}", flush=True)
            await interaction.followup.send("❌ 새로고침 중 오류가 발생했어요.", ephemeral=True)

    @discord.ui.button(label="경기 목록", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def back_to_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            markets = await rpc("bet_list_open_markets", {"p_guild_id": str(interaction.guild_id) if interaction.guild_id else None})
            if not markets:
                await interaction.edit_original_response(content="지금 베팅할 수 있는 경기가 없어요.", embed=None, view=None)
                return
            await interaction.edit_original_response(
                content="🎰 베팅할 경기를 선택하세요.", embed=None, view=MarketSelectView(self.user_id, markets)
            )
        except Exception as e:
            print(f"❌ [BetPanelView.back 에러] {e}", flush=True)
            await interaction.followup.send("❌ 경기 목록을 불러오는 중 오류가 발생했습니다.", ephemeral=True)
