"""
🎰 내전 베팅 및 🎃 사탕뽑기 통합 시스템 (관전자용)
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

# 🎃 사탕뽑기 확률은 DB RPC bet_candy_draw 에서 처리
# 일반 40% / 달콤 30% / 황금 15% / 박쥐 10% / 대박 2.5% / 꽝 2.5%

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
            await interaction.followup.send("❌ 목록을 불러오지 못했어요.", ephemeral=True)

class MarketSelectView(discord.ui.View):
    def __init__(self, user_id: int, markets: list):
        super().__init__(timeout=300)
        self.user_id = user_id

        options = []
        for m in markets[:25]:
            names_a = [x["name"] for x in m.get("team_a", [])]
            names_b = [x["name"] for x in m.get("team_b", [])]
            desc = f"🔵 {', '.join(names_a[:3])} vs 🔴 {', '.join(names_b[:3])}"[:100]
            options.append(discord.SelectOption(
                label=f"{m.get('game', '내전')} #{m.get('id')} · {len(names_a)}v{len(names_b)}"[:100],
                description=desc,
                value=str(m["id"]),
            ))

        self.select = discord.ui.Select(placeholder="🎮 베팅할 경기를 선택하세요", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인이 연 화면만 조작할 수 있어요. `/배팅`을 직접 입력해 주세요.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            market_id = int(self.select.values[0])
            market = await rpc("bet_get_market", {"p_market_id": market_id, "p_discord_id": str(interaction.user.id)})
            if not market:
                await interaction.edit_original_response(content="❌ 경기를 찾을 수 없어요.", embed=None, view=None)
                return
            await interaction.edit_original_response(
                content=None,
                embed=build_market_embed(market),
                view=BetPanelView(self.user_id, market),
            )
        except Exception as e:
            print(f"❌ [MarketSelectView 에러] {e}", flush=True)
            await interaction.followup.send("❌ 경기 정보를 불러오지 못했어요.", ephemeral=True)


# ---------------------------------------------------------------
# 🔗 custom_game.py 에서 호출하는 연동 함수
# ---------------------------------------------------------------
_cog: Optional["BettingCog"] = None


async def on_match_started(guild_id, channel_id, message_id, game, host_id, team_a, team_b):
    if _cog and _cog.enabled:
        await _cog.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=False)


async def on_teams_changed(guild_id, channel_id, message_id, game, host_id, team_a, team_b):
    if _cog and _cog.enabled:
        await _cog.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=True)


async def on_match_finished(channel_id, message_id, winner_side):
    if _cog and _cog.enabled:
        await _cog.settle_market(channel_id, message_id, winner_side)


async def on_match_cancelled(channel_id, message_id):
    if _cog and _cog.enabled:
        await _cog.cancel_market(channel_id, message_id)


# ---------------------------------------------------------------
# 🧩 Cog
# ---------------------------------------------------------------
class BettingCog(commands.Cog):
    def __init__(self, bot):
        global _cog
        self.bot = bot
        self.enabled = ENABLED
        self._locks: dict = {}
        self._reconciled_once = False
        _cog = self

        if not self.enabled:
            print("⚠️ [베팅] SUPABASE_SERVICE_KEY 가 없어 베팅 기능이 꺼져 있어요.", flush=True)
        elif not ROLE_AMOUNTS:
            print("⚠️ [베팅] CHIP_ROLE_IDS 에 칩 역할 ID가 설정되지 않아 입금 감지가 꺼져 있어요.", flush=True)

    async def cog_load(self):
        if self.enabled and ROLE_AMOUNTS:
            self.reconcile_loop.start()

    async def cog_unload(self):
        global _cog
        if self.reconcile_loop.is_running():
            self.reconcile_loop.cancel()
        if _cog is self:
            _cog = None

    async def hook_match_started(self, guild_id, channel_id, message_id, game, host_id, team_a, team_b):
        await self.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=False)

    async def hook_teams_changed(self, guild_id, channel_id, message_id, game, host_id, team_a, team_b):
        await self.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=True)

    async def hook_match_finished(self, channel_id, message_id, winner_side):
        await self.settle_market(channel_id, message_id, winner_side)

    async def hook_match_cancelled(self, channel_id, message_id):
        await self.cancel_market(channel_id, message_id)

    async def _channel(self, channel_id):
        if not channel_id:
            return None
        ch = self.bot.get_channel(int(channel_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                ch = None
        return ch

    async def open_market(self, guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed: bool):
        if not message_id:
            return
        try:
            mult_a, mult_b = compute_multipliers(team_a, team_b)
            blocked = sorted({str(p["member"].id) for p in team_a + team_b} | {str(host_id)})
            res = await rpc("bet_open_market", {
                "p_source_message_id": str(message_id),
                "p_channel_id": str(channel_id) if channel_id else None,
                "p_guild_id": str(guild_id) if guild_id else None,
                "p_game": game,
                "p_host_id": str(host_id),
                "p_team_a": team_to_json(team_a),
                "p_team_b": team_to_json(team_b),
                "p_blocked": blocked,
                "p_mult_a": mult_a,
                "p_mult_b": mult_b,
                "p_window_seconds": BET_WINDOW_MINUTES * 60,
                "p_title": None,
            })
            print(f"🎰 [베팅 마켓 열림] #{(res or {}).get('market_id')} · 메시지 {message_id} · {game} · 배율 A×{mult_a} / B×{mult_b} · {BET_WINDOW_MINUTES}분", flush=True)
            refunded = (res or {}).get("refunded") or []
            if refunded:
                ch = await self._channel(channel_id)
                if ch:
                    mentions = " ".join(f"<@{r['discord_id']}>" for r in refunded[:20])
                    await ch.send(
                        f"🔄 팀이 변경되어 이 경기의 베팅이 **전액 환불**됐어요. 다시 `/배팅`으로 걸 수 있어요.\n{mentions}",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
            if not changed and BET_PUBLIC_ANNOUNCEMENT_CHANNEL_ID:
                pub = await self._channel(BET_PUBLIC_ANNOUNCEMENT_CHANNEL_ID)
                if pub:
                    names_a = ", ".join(p["member"].display_name for p in team_a if "member" in p) or "미정"
                    names_b = ", ".join(p["member"].display_name for p in team_b if "member" in p) or "미정"
                    await pub.send(
                        f"@everyone\n🎰 **{game}** 베팅이 열렸어요! {BET_WINDOW_MINUTES}분 동안 `/배팅`으로 참여하세요.\n"
                        f"🔵 A팀: {names_a}\n🔴 B팀: {names_b}",
                        allowed_mentions=discord.AllowedMentions(everyone=True),
                    )
        except Exception as e:
            print(f"❌ [베팅 마켓 열기 에러] {e}", flush=True)

    async def settle_market(self, channel_id, message_id, winner_side):
        if not message_id:
            return
        try:
            res = await rpc("bet_settle_by_source", {"p_source_message_id": str(message_id), "p_winner": winner_side})
            if not res or not res.get("settled"):
                return
            if res["winners"] + res["losers"] == 0:
                return

            ch = await self._channel(channel_id)
            if not ch:
                return
            side_label = "🔵 A팀" if winner_side == "A" else "🔴 B팀"
            embed = discord.Embed(
                title=f"🎰 베팅 정산 — {side_label} 승리 (×{float(res['mult']):.1f})",
                color=discord.Color.gold(),
            )
            embed.add_field(name="🎯 적중", value=f"{res['winners']}명", inline=True)
            embed.add_field(name="💥 실패", value=f"{res['losers']}명", inline=True)
            embed.add_field(name="💰 지급 / 🔥 소멸", value=f"{int(res['paid']):,} / {int(res['burned']):,}칩", inline=True)
            top = res.get("top") or []
            if top:
                embed.add_field(
                    name="🏅 이번 판 수익 TOP",
                    value="\n".join(f"<@{t['discord_id']}> +{int(t['net']):,}칩" for t in top),
                    inline=False,
                )
            await ch.send(embed=embed)
        except Exception as e:
            print(f"❌ [베팅 정산 에러] {e}", flush=True)

    async def cancel_market(self, channel_id, message_id):
        if not message_id:
            return
        try:
            res = await rpc("bet_cancel_by_source", {"p_source_message_id": str(message_id)})
            refunded = (res or {}).get("refunded") or []
            if refunded:
                ch = await self._channel(channel_id)
                if ch:
                    mentions = " ".join(f"<@{r['discord_id']}>" for r in refunded[:20])
                    await ch.send(
                        f"↩️ 경기가 무효 처리되어 베팅이 **전액 환불**됐어요.\n{mentions}",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
        except Exception as e:
            print(f"❌ [베팅 취소 에러] {e}", flush=True)

    async def process_receipt(self, member: discord.Member, role: discord.Role, amount: int):
        key = (member.id, role.id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                res = await rpc("bet_claim_receipt", {
                    "p_discord_id": str(member.id),
                    "p_role_id": str(role.id),
                    "p_amount": amount,
                })
                receipt_id = res["receipt_id"]

                removed = False
                try:
                    await member.remove_roles(role, reason="베팅 칩 입금 처리 완료")
                    removed = True
                except discord.HTTPException as e:
                    print(f"⚠️ [베팅] 칩 역할 회수 실패({member.id}/{role.id}): {e} — 봇 역할이 칩 역할보다 위에 있는지, 역할 관리 권한이 있는지 확인하세요.", flush=True)

                if removed:
                    await rpc("bet_receipt_done", {"p_receipt_id": receipt_id})

                if res.get("credited"):
                    print(f"💰 [베팅 입금] {member} +{amount:,}칩 (잔액 {int(res['balance']):,})", flush=True)
                    try:
                        await member.send(f"💰 베팅 칩 **{amount:,}**개가 충전됐어요! 현재 잔액 **{int(res['balance']):,}**칩\n`/배팅`으로 진행 중인 경기에 참여해 보세요.")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            except Exception as e:
                print(f"❌ [베팅 입금 처리 에러] {member.id}/{role.id}: {e}", flush=True)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not (self.enabled and ROLE_AMOUNTS):
            return
        for role in set(after.roles) - set(before.roles):
            amount = ROLE_AMOUNTS.get(role.id)
            if amount:
                await self.process_receipt(after, role, amount)

    async def reconcile(self):
        for guild in self.bot.guilds:
            for role_id, amount in ROLE_AMOUNTS.items():
                role = guild.get_role(role_id)
                if not role:
                    continue
                for member in list(role.members):
                    if not member.bot:
                        await self.process_receipt(member, role, amount)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.enabled and ROLE_AMOUNTS and not self._reconciled_once:
            self._reconciled_once = True
            await self.reconcile()

    @tasks.loop(minutes=5)
    async def reconcile_loop(self):
        await self.reconcile()

    @reconcile_loop.before_loop
    async def _before_reconcile(self):
        await self.bot.wait_until_ready()

    async def _ensure_enabled(self, ctx: commands.Context) -> bool:
        if not self.enabled:
            await ctx.send("⚠️ 베팅 시스템이 아직 설정되지 않았어요. (관리자에게 문의해 주세요)", ephemeral=True)
            return False
        return True

    @commands.hybrid_command(name="배팅", description="진행 중인 내전 경기를 골라 칩을 걸어요.")
    async def bet(self, ctx: commands.Context):
        if not await self._ensure_enabled(ctx):
            return
        try:
            markets = await rpc("bet_list_open_markets", {"p_guild_id": str(ctx.guild.id) if ctx.guild else None})
        except Exception as e:
            print(f"❌ [/배팅 에러] {e}", flush=True)
            await ctx.send("❌ 경기 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return

        if not markets:
            await ctx.send(
                f"지금 베팅할 수 있는 경기가 없어요.\n방장이 대기실에서 **팀 짜기 시작**을 눌러 팀이 확정된 뒤 {BET_WINDOW_MINUTES}분 동안만 베팅할 수 있어요.",
                ephemeral=True,
            )
            return

        await ctx.send("🎰 베팅할 경기를 선택하세요.", view=MarketSelectView(ctx.author.id, markets), ephemeral=True)

    @commands.hybrid_command(name="칩", description="내 베팅 칩 잔액과 최근 내역, 충전 방법을 확인해요.")
    async def chips(self, ctx: commands.Context):
        if not await self._ensure_enabled(ctx):
            return
        try:
            w = await rpc("bet_get_wallet", {"p_discord_id": str(ctx.author.id)})
        except Exception as e:
            print(f"❌ [/칩 에러] {e}", flush=True)
            await ctx.send("❌ 잔액을 불러오지 못했어요.", ephemeral=True)
            return

        kind_label = {
            "deposit": "충전", "bet": "베팅", "payout": "적중 지급", "refund": "베팅 환불",
            "withdraw_request": "출금 신청", "withdraw_refund": "출금 거절 환불", "admin_adjust": "관리자 조정",
            "candy": "사탕뽑기",
        }
        recent = w.get("recent") or []
        lines = [f"`{r['delta']:+,}` · {kind_label.get(r['kind'], r['kind'])} · <t:{r['at']}:R>" for r in recent]

        embed = discord.Embed(title="💰 내 베팅 칩", color=discord.Color.gold())
        embed.add_field(name="잔액", value=f"**{int(w['balance']):,}** 칩", inline=True)
        embed.add_field(name="누적 충전 / 출금", value=f"{int(w['total_deposited']):,} / {int(w['total_withdrawn']):,}", inline=True)
        embed.add_field(name="최근 내역", value="\n".join(lines) if lines else "내역이 없어요.", inline=False)
        amounts = " / ".join(f"{a:,}" for a in sorted(CHIP_ROLE_IDS))
        embed.set_footer(text=f"충전: /경험치샵에서 칩 역할({amounts}) 구매 → 자동 입금 · 출금: /출금 · 사탕: /사탕뽑기")
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="사탕뽑기", description="🎃 할로윈 사탕을 뽑아 칩을 받아요. (유저당 하루 1회)")
    async def candy_draw(self, ctx: commands.Context):
        if not await self._ensure_enabled(ctx):
            return
        try:
            res = await rpc("bet_candy_draw", {"p_discord_id": str(ctx.author.id)})
        except Exception as e:
            print(f"❌ [사탕뽑기 에러] {e}", flush=True)
            await ctx.send("❌ 사탕뽑기 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return

        name = res.get("name") or "사탕"
        chips = int(res.get("chips") or 0)
        balance = int(res.get("balance") or 0)

        if not res.get("ok"):
            if res.get("reason") == "already_drawn":
                await ctx.send(
                    f"⏳ 오늘은 이미 사탕을 뽑았어요. (**{name}**, {chips:+,}칩)\n"
                    f"자정(한국시간) 이후에 다시 시도해 주세요. 현재 잔액 **{balance:,}**칩",
                    ephemeral=True,
                )
                return
            await ctx.send("❌ 사탕뽑기에 실패했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎃 사탕뽑기",
            description=f"{ctx.author.mention} 님이 **{name}** 을(를) 뽑았어요!",
            color=discord.Color.orange(),
        )
        if chips > 0:
            embed.add_field(name="획득 칩", value=f"**+{chips:,}**칩\n현재 잔액 **{balance:,}**칩", inline=False)
        else:
            embed.add_field(name="결과", value=f"💀 꽝! 칩은 없어요.\n현재 잔액 **{balance:,}**칩", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="출금", description="베팅 칩을 경험치로 돌려받기 위해 출금을 신청해요. (관리자가 확인 후 지급)")
    async def withdraw(self, ctx: commands.Context, amount: int):
        if not await self._ensure_enabled(ctx):
            return
        try:
            res = await rpc("bet_request_withdraw", {
                "p_discord_id": str(ctx.author.id),
                "p_amount": int(amount),
                "p_min": MIN_WITHDRAW,
            })
        except Exception as e:
            print(f"❌ [/출금 에러] {e}", flush=True)
            await ctx.send("❌ 출금 신청 중 오류가 발생했어요.", ephemeral=True)
            return

        if not res.get("ok"):
            reason = res.get("reason")
            if reason == "below_min":
                msg = f"❌ 최소 **{int(res.get('min', MIN_WITHDRAW)):,}**칩부터 출금할 수 있어요."
            elif reason == "insufficient":
                msg = f"❌ 칩이 부족해요. (보유 {int(res.get('balance', 0)):,}칩)"
            else:
                msg = "❌ 출금 금액을 확인해 주세요."
            await ctx.send(msg, ephemeral=True)
            return

        await ctx.send(
            f"📤 출금 신청 **#{res['request_id']}** ({int(amount):,}칩)이 접수됐어요.\n"
            f"관리자가 확인한 뒤 LeaderBoard 경험치로 지급해 드려요. 남은 칩: **{int(res['balance']):,}**",
            ephemeral=True,
        )

        if BET_ADMIN_CHANNEL_ID:
            ch = await self._channel(BET_ADMIN_CHANNEL_ID)
            if ch:
                await ch.send(
                    f"📤 **출금 신청 #{res['request_id']}** — {ctx.author.mention} 님이 **{int(amount):,}**칩 출금을 신청했어요.\n"
                    f"LeaderBoard `/경험치추가`로 지급한 뒤 웹 관리자 페이지 → 💰 칩 에서 **지급 완료**를 눌러 주세요."
                )


async def setup(bot):
    await bot.add_cog(BettingCog(bot))

"""
🎰 내전 베팅 및 🎃 사탕뽑기 통합 시스템 (관전자용)
"""
import asyncio
import os
import re
import random
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

# 🎃 사탕 설정 및 가중치(확률 %) 설정
CANDY_TYPES = [
    {"name": "🍬 일반사탕", "chips": 1000, "weight": 40},
    {"name": "🍭 달콤한 사탕", "chips": 3000, "weight": 30},
    {"name": "🎃 황금호박", "chips": 10000, "weight": 15},
    {"name": "🦇 박쥐사탕", "chips": 15000, "weight": 10},
    {"name": "👻 저주받은 사탕 (대박)", "chips": 20000, "weight": 2.5},
    {"name": "💀 저주받은 사탕 (꽝)", "chips": 0, "weight": 2.5},
]

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
            await interaction.followup.send("❌ 목록을 불러오지 못했어요.", ephemeral=True)

class MarketSelectView(discord.ui.View):
    def __init__(self, user_id: int, markets: list):
        super().__init__(timeout=300)
        self.user_id = user_id

        options = []
        for m in markets[:25]:
            names_a = [x["name"] for x in m.get("team_a", [])]
            names_b = [x["name"] for x in m.get("team_b", [])]
            desc = f"🔵 {', '.join(names_a[:3])} vs 🔴 {', '.join(names_b[:3])}"[:100]
            options.append(discord.SelectOption(
                label=f"{m.get('game', '내전')} #{m.get('id')} · {len(names_a)}v{len(names_b)}"[:100],
                description=desc,
                value=str(m["id"]),
            ))

        self.select = discord.ui.Select(placeholder="🎮 베팅할 경기를 선택하세요", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인이 연 화면만 조작할 수 있어요. `/배팅`을 직접 입력해 주세요.", ephemeral=True)
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            market_id = int(self.select.values[0])
            market = await rpc("bet_get_market", {"p_market_id": market_id, "p_discord_id": str(interaction.user.id)})
            if not market:
                await interaction.edit_original_response(content="❌ 경기를 찾을 수 없어요.", embed=None, view=None)
                return
            await interaction.edit_original_response(
                content=None,
                embed=build_market_embed(market),
                view=BetPanelView(self.user_id, market),
            )
        except Exception as e:
            print(f"❌ [MarketSelectView 에러] {e}", flush=True)
            await interaction.followup.send("❌ 경기 정보를 불러오지 못했어요.", ephemeral=True)


def draw_candy() -> dict:
    return random.choices(CANDY_TYPES, weights=[c["weight"] for c in CANDY_TYPES], k=1)[0]


# ---------------------------------------------------------------
# 🔗 custom_game.py 에서 호출하는 연동 함수
# ---------------------------------------------------------------
_cog: Optional["BettingCog"] = None


async def on_match_started(guild_id, channel_id, message_id, game, host_id, team_a, team_b):
    if _cog and _cog.enabled:
        await _cog.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=False)


async def on_teams_changed(guild_id, channel_id, message_id, game, host_id, team_a, team_b):
    if _cog and _cog.enabled:
        await _cog.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=True)


async def on_match_finished(channel_id, message_id, winner_side):
    if _cog and _cog.enabled:
        await _cog.settle_market(channel_id, message_id, winner_side)


async def on_match_cancelled(channel_id, message_id):
    if _cog and _cog.enabled:
        await _cog.cancel_market(channel_id, message_id)


# ---------------------------------------------------------------
# 🧩 Cog
# ---------------------------------------------------------------
class BettingCog(commands.Cog):
    def __init__(self, bot):
        global _cog
        self.bot = bot
        self.enabled = ENABLED
        self._locks: dict = {}
        self._reconciled_once = False
        _cog = self

        if not self.enabled:
            print("⚠️ [베팅] SUPABASE_SERVICE_KEY 가 없어 베팅 기능이 꺼져 있어요.", flush=True)
        elif not ROLE_AMOUNTS:
            print("⚠️ [베팅] CHIP_ROLE_IDS 에 칩 역할 ID가 설정되지 않아 입금 감지가 꺼져 있어요.", flush=True)

    async def cog_load(self):
        if self.enabled and ROLE_AMOUNTS:
            self.reconcile_loop.start()

    async def cog_unload(self):
        global _cog
        if self.reconcile_loop.is_running():
            self.reconcile_loop.cancel()
        if _cog is self:
            _cog = None

    async def hook_match_started(self, guild_id, channel_id, message_id, game, host_id, team_a, team_b):
        await self.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=False)

    async def hook_teams_changed(self, guild_id, channel_id, message_id, game, host_id, team_a, team_b):
        await self.open_market(guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed=True)

    async def hook_match_finished(self, channel_id, message_id, winner_side):
        await self.settle_market(channel_id, message_id, winner_side)

    async def hook_match_cancelled(self, channel_id, message_id):
        await self.cancel_market(channel_id, message_id)

    async def _channel(self, channel_id):
        if not channel_id:
            return None
        ch = self.bot.get_channel(int(channel_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                ch = None
        return ch

    async def open_market(self, guild_id, channel_id, message_id, game, host_id, team_a, team_b, changed: bool):
        if not message_id:
            return
        try:
            mult_a, mult_b = compute_multipliers(team_a, team_b)
            blocked = sorted({str(p["member"].id) for p in team_a + team_b} | {str(host_id)})
            res = await rpc("bet_open_market", {
                "p_source_message_id": str(message_id),
                "p_channel_id": str(channel_id) if channel_id else None,
                "p_guild_id": str(guild_id) if guild_id else None,
                "p_game": game,
                "p_host_id": str(host_id),
                "p_team_a": team_to_json(team_a),
                "p_team_b": team_to_json(team_b),
                "p_blocked": blocked,
                "p_mult_a": mult_a,
                "p_mult_b": mult_b,
                "p_window_seconds": BET_WINDOW_MINUTES * 60,
                "p_title": None,
            })
            print(f"🎰 [베팅 마켓 열림] #{(res or {}).get('market_id')} · 메시지 {message_id} · {game} · 배율 A×{mult_a} / B×{mult_b} · {BET_WINDOW_MINUTES}분", flush=True)
            refunded = (res or {}).get("refunded") or []
            if refunded:
                ch = await self._channel(channel_id)
                if ch:
                    mentions = " ".join(f"<@{r['discord_id']}>" for r in refunded[:20])
                    await ch.send(
                        f"🔄 팀이 변경되어 이 경기의 베팅이 **전액 환불**됐어요. 다시 `/배팅`으로 걸 수 있어요.\n{mentions}",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
            if not changed and BET_PUBLIC_ANNOUNCEMENT_CHANNEL_ID:
                pub = await self._channel(BET_PUBLIC_ANNOUNCEMENT_CHANNEL_ID)
                if pub:
                    names_a = ", ".join(p["member"].display_name for p in team_a if "member" in p) or "미정"
                    names_b = ", ".join(p["member"].display_name for p in team_b if "member" in p) or "미정"
                    await pub.send(
                        f"@everyone\n🎰 **{game}** 베팅이 열렸어요! {BET_WINDOW_MINUTES}분 동안 `/배팅`으로 참여하세요.\n"
                        f"🔵 A팀: {names_a}\n🔴 B팀: {names_b}",
                        allowed_mentions=discord.AllowedMentions(everyone=True),
                    )
        except Exception as e:
            print(f"❌ [베팅 마켓 열기 에러] {e}", flush=True)

    async def settle_market(self, channel_id, message_id, winner_side):
        if not message_id:
            return
        try:
            res = await rpc("bet_settle_by_source", {"p_source_message_id": str(message_id), "p_winner": winner_side})
            if not res or not res.get("settled"):
                return
            if res["winners"] + res["losers"] == 0:
                return

            ch = await self._channel(channel_id)
            if not ch:
                return
            side_label = "🔵 A팀" if winner_side == "A" else "🔴 B팀"
            embed = discord.Embed(
                title=f"🎰 베팅 정산 — {side_label} 승리 (×{float(res['mult']):.1f})",
                color=discord.Color.gold(),
            )
            embed.add_field(name="🎯 적중", value=f"{res['winners']}명", inline=True)
            embed.add_field(name="💥 실패", value=f"{res['losers']}명", inline=True)
            embed.add_field(name="💰 지급 / 🔥 소멸", value=f"{int(res['paid']):,} / {int(res['burned']):,}칩", inline=True)
            top = res.get("top") or []
            if top:
                embed.add_field(
                    name="🏅 이번 판 수익 TOP",
                    value="\n".join(f"<@{t['discord_id']}> +{int(t['net']):,}칩" for t in top),
                    inline=False,
                )
            await ch.send(embed=embed)
        except Exception as e:
            print(f"❌ [베팅 정산 에러] {e}", flush=True)

    async def cancel_market(self, channel_id, message_id):
        if not message_id:
            return
        try:
            res = await rpc("bet_cancel_by_source", {"p_source_message_id": str(message_id)})
            refunded = (res or {}).get("refunded") or []
            if refunded:
                ch = await self._channel(channel_id)
                if ch:
                    mentions = " ".join(f"<@{r['discord_id']}>" for r in refunded[:20])
                    await ch.send(
                        f"↩️ 경기가 무효 처리되어 베팅이 **전액 환불**됐어요.\n{mentions}",
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
        except Exception as e:
            print(f"❌ [베팅 취소 에러] {e}", flush=True)

    async def process_receipt(self, member: discord.Member, role: discord.Role, amount: int):
        key = (member.id, role.id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                res = await rpc("bet_claim_receipt", {
                    "p_discord_id": str(member.id),
                    "p_role_id": str(role.id),
                    "p_amount": amount,
                })
                receipt_id = res["receipt_id"]

                removed = False
                try:
                    await member.remove_roles(role, reason="베팅 칩 입금 처리 완료")
                    removed = True
                except discord.HTTPException as e:
                    print(f"⚠️ [베팅] 칩 역할 회수 실패({member.id}/{role.id}): {e} — 봇 역할이 칩 역할보다 위에 있는지, 역할 관리 권한이 있는지 확인하세요.", flush=True)

                if removed:
                    await rpc("bet_receipt_done", {"p_receipt_id": receipt_id})

                if res.get("credited"):
                    print(f"💰 [베팅 입금] {member} +{amount:,}칩 (잔액 {int(res['balance']):,})", flush=True)
                    try:
                        await member.send(f"💰 베팅 칩 **{amount:,}**개가 충전됐어요! 현재 잔액 **{int(res['balance']):,}**칩\n`/배팅`으로 진행 중인 경기에 참여해 보세요.")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            except Exception as e:
                print(f"❌ [베팅 입금 처리 에러] {member.id}/{role.id}: {e}", flush=True)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not (self.enabled and ROLE_AMOUNTS):
            return
        for role in set(after.roles) - set(before.roles):
            amount = ROLE_AMOUNTS.get(role.id)
            if amount:
                await self.process_receipt(after, role, amount)

    async def reconcile(self):
        for guild in self.bot.guilds:
            for role_id, amount in ROLE_AMOUNTS.items():
                role = guild.get_role(role_id)
                if not role:
                    continue
                for member in list(role.members):
                    if not member.bot:
                        await self.process_receipt(member, role, amount)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.enabled and ROLE_AMOUNTS and not self._reconciled_once:
            self._reconciled_once = True
            await self.reconcile()

    @tasks.loop(minutes=5)
    async def reconcile_loop(self):
        await self.reconcile()

    @reconcile_loop.before_loop
    async def _before_reconcile(self):
        await self.bot.wait_until_ready()

    async def _ensure_enabled(self, ctx: commands.Context) -> bool:
        if not self.enabled:
            await ctx.send("⚠️ 베팅 시스템이 아직 설정되지 않았어요. (관리자에게 문의해 주세요)", ephemeral=True)
            return False
        return True

    @commands.hybrid_command(name="배팅", description="진행 중인 내전 경기를 골라 칩을 걸어요.")
    async def bet(self, ctx: commands.Context):
        if not await self._ensure_enabled(ctx):
            return
        try:
            markets = await rpc("bet_list_open_markets", {"p_guild_id": str(ctx.guild.id) if ctx.guild else None})
        except Exception as e:
            print(f"❌ [/배팅 에러] {e}", flush=True)
            await ctx.send("❌ 경기 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            return

        if not markets:
            await ctx.send(
                f"지금 베팅할 수 있는 경기가 없어요.\n방장이 대기실에서 **팀 짜기 시작**을 눌러 팀이 확정된 뒤 {BET_WINDOW_MINUTES}분 동안만 베팅할 수 있어요.",
                ephemeral=True,
            )
            return

        await ctx.send("🎰 베팅할 경기를 선택하세요.", view=MarketSelectView(ctx.author.id, markets), ephemeral=True)

    @commands.hybrid_command(name="칩", description="내 베팅 칩 잔액과 최근 내역, 충전 방법을 확인해요.")
    async def chips(self, ctx: commands.Context):
        if not await self._ensure_enabled(ctx):
            return
        try:
            w = await rpc("bet_get_wallet", {"p_discord_id": str(ctx.author.id)})
        except Exception as e:
            print(f"❌ [/칩 에러] {e}", flush=True)
            await ctx.send("❌ 잔액을 불러오지 못했어요.", ephemeral=True)
            return

        kind_label = {
            "deposit": "충전", "bet": "베팅", "payout": "적중 지급", "refund": "베팅 환불",
            "withdraw_request": "출금 신청", "withdraw_refund": "출금 거절 환불", "admin_adjust": "관리자 조정",
            "candy": "사탕뽑기",
        }
        recent = w.get("recent") or []
        lines = [f"`{r['delta']:+,}` · {kind_label.get(r['kind'], r['kind'])} · <t:{r['at']}:R>" for r in recent]

        embed = discord.Embed(title="💰 내 베팅 칩", color=discord.Color.gold())
        embed.add_field(name="잔액", value=f"**{int(w['balance']):,}** 칩", inline=True)
        embed.add_field(name="누적 충전 / 출금", value=f"{int(w['total_deposited']):,} / {int(w['total_withdrawn']):,}", inline=True)
        embed.add_field(name="최근 내역", value="\n".join(lines) if lines else "내역이 없어요.", inline=False)
        amounts = " / ".join(f"{a:,}" for a in sorted(CHIP_ROLE_IDS))
        embed.set_footer(text=f"충전: /경험치샵에서 칩 역할({amounts}) 구매 → 자동 입금 · 출금: /출금 · 사탕: /사탕뽑기")
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="사탕뽑기", description="🎃 할로윈 사탕을 뽑아 칩을 받아요. (유저당 하루 1회)")
    @commands.cooldown(1, 60 * 60 * 24, commands.BucketType.user)
    async def candy_draw(self, ctx: commands.Context):
        if not await self._ensure_enabled(ctx):
            return
        candy = draw_candy()
        chips = int(candy["chips"])
        balance_text = ""
        if chips > 0:
            try:
                res = await rpc("bet_admin_adjust", {
                    "p_discord_id": str(ctx.author.id),
                    "p_delta": chips,
                    "p_note": f"사탕뽑기:{candy['name']}",
                })
                if isinstance(res, dict) and res.get("balance") is not None:
                    balance_text = f"\n현재 잔액 **{int(res['balance']):,}**칩"
            except Exception as e:
                print(f"❌ [사탕뽑기 지급 에러] {e}", flush=True)
                await ctx.send("❌ 사탕은 뽑았지만 칩 지급에 실패했어요. 관리자에게 문의해 주세요.", ephemeral=True)
                return

        embed = discord.Embed(
            title="🎃 사탕뽑기",
            description=f"{ctx.author.mention} 님이 **{candy['name']}** 을(를) 뽑았어요!",
            color=discord.Color.orange(),
        )
        if chips > 0:
            embed.add_field(name="획득 칩", value=f"**+{chips:,}**칩{balance_text}", inline=False)
        else:
            embed.add_field(name="결과", value="💀 꽝! 칩은 없어요.", inline=False)
        await ctx.send(embed=embed)

    @candy_draw.error
    async def candy_draw_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandOnCooldown):
            hours = int(error.retry_after // 3600)
            minutes = int((error.retry_after % 3600) // 60)
            await ctx.send(f"⏳ 사탕뽑기는 하루 1회예요. **{hours}시간 {minutes}분** 뒤에 다시 시도해 주세요.", ephemeral=True)
            return
        print(f"❌ [사탕뽑기 에러] {error}", flush=True)

    @commands.hybrid_command(name="출금", description="베팅 칩을 경험치로 돌려받기 위해 출금을 신청해요. (관리자가 확인 후 지급)")
    async def withdraw(self, ctx: commands.Context, amount: int):
        if not await self._ensure_enabled(ctx):
            return
        try:
            res = await rpc("bet_request_withdraw", {
                "p_discord_id": str(ctx.author.id),
                "p_amount": int(amount),
                "p_min": MIN_WITHDRAW,
            })
        except Exception as e:
            print(f"❌ [/출금 에러] {e}", flush=True)
            await ctx.send("❌ 출금 신청 중 오류가 발생했어요.", ephemeral=True)
            return

        if not res.get("ok"):
            reason = res.get("reason")
            if reason == "below_min":
                msg = f"❌ 최소 **{int(res.get('min', MIN_WITHDRAW)):,}**칩부터 출금할 수 있어요."
            elif reason == "insufficient":
                msg = f"❌ 칩이 부족해요. (보유 {int(res.get('balance', 0)):,}칩)"
            else:
                msg = "❌ 출금 금액을 확인해 주세요."
            await ctx.send(msg, ephemeral=True)
            return

        await ctx.send(
            f"📤 출금 신청 **#{res['request_id']}** ({int(amount):,}칩)이 접수됐어요.\n"
            f"관리자가 확인한 뒤 LeaderBoard 경험치로 지급해 드려요. 남은 칩: **{int(res['balance']):,}**",
            ephemeral=True,
        )

        if BET_ADMIN_CHANNEL_ID:
            ch = await self._channel(BET_ADMIN_CHANNEL_ID)
            if ch:
                await ch.send(
                    f"📤 **출금 신청 #{res['request_id']}** — {ctx.author.mention} 님이 **{int(amount):,}**칩 출금을 신청했어요.\n"
                    f"LeaderBoard `/경험치추가`로 지급한 뒤 웹 관리자 페이지 → 💰 칩 에서 **지급 완료**를 눌러 주세요."
                )


async def setup(bot):
    await bot.add_cog(BettingCog(bot))

