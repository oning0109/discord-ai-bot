"""내전: /모집 → 대기실 → 팀 편성 → 결과 → (한 판 더)

* 대기실·팀·결과는 DB(bot_lobbies)에 저장 + 영구 버튼 → 봇을 재시작해도 그대로 동작
* 옵치는 포지션별 맞대결로 보여주고, '자리 바꾸기'로 두 사람의 팀·포지션을 한 번에 맞바꾼다
* 결과를 기록하면 '같은 팀으로 한 판 더 / 다시 밸런싱 / 명단 수정' 버튼이 뜬다
"""
import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from core.balancer import balance, pair_counts_from_matches, reannotate_ow, split_key
from core.ranks import LOBBY_GAMES, OW_ROLE_EMOJI, OW_ROLE_NAME, OW_ROLES, ow_tier, parse_profile, player_from_row
from core.recording import record_full_match

log = logging.getLogger("rakko.lobby")

GAME_OPTIONS = [
    ("오버워치", "옵치", "🛡️", "포지션 선호 + 예상 승률 기반 밸런싱"),
    ("발로란트", "발로란트", "🎯", "티어/승률 기반 밸런싱"),
    ("리그 오브 레전드", "롤", "⚔️", "랜덤 팀 (기록 없음)"),
]
GAME_LABEL = {"옵치": "오버워치", "발로란트": "발로란트", "롤": "롤"}
EXPIRED = "❌ 만료되었거나 이미 끝난 대기실입니다."
SLOT_ORDER = {"tank": 0, "deal": 1, "heal": 2}


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


# ═══════════════════════ 임베드 ═══════════════════════
def build_lobby_embed(lobby: dict, guild: Optional[discord.Guild]) -> discord.Embed:
    ids = lobby["participants"]
    host = guild.get_member(int(lobby["host_id"])) if guild else None
    e = discord.Embed(title=f"📢 내전 대기실 · {GAME_LABEL[lobby['game']]}",
                      description="아래 버튼으로 참가하거나 취소하세요!", color=discord.Color.blue())
    cap = " / 최대 10명" if lobby["game"] == "옵치" else ""
    e.add_field(name=f"👥 참가자 ({len(ids)}명{cap})",
                value="\n".join(f"{i}. <@{u}>" for i, u in enumerate(ids, 1)) or "아직 참가자가 없습니다.", inline=False)
    e.set_footer(text=f"방장: {host.display_name if host else lobby['host_id']}")
    return e


def _ow_tag(p: dict) -> str:
    if p["pref_on_role"] == "no":
        return " ⚠️"
    return " ·부" if p["pref_on_role"] == "sub" else ""


def build_teams_embed(game: str, teams: dict, footer: str | None = None) -> discord.Embed:
    a, b, info = teams["a"], teams["b"], teams.get("info") or {}
    rnd = teams.get("round", 1)
    e = discord.Embed(title=f"⚔️ {GAME_LABEL[game]} 내전 · {rnd}판째", color=discord.Color.gold())

    if game == "옵치":
        lines = [f"📊 **예상 승률** 🔵 A `{pct(info['expected_a'])}` : `{pct(1 - info['expected_a'])}` B 🔴"
                 f"  ·  전력 🔵 {info['rating_a']:,.0f} / 🔴 {info['rating_b']:,.0f}", ""]
        for r in OW_ROLES:
            pa = [p for p in a if p["assigned_role"] == r]
            pb = [p for p in b if p["assigned_role"] == r]
            if not pa and not pb:
                continue
            lines.append(f"{OW_ROLE_EMOJI[r]} **{OW_ROLE_NAME[r]}**")
            for i in range(max(len(pa), len(pb))):
                left = (f"🔵 {pa[i]['member'].mention} `{ow_tier(pa[i]['eff_mmr'])}`{_ow_tag(pa[i])}" if i < len(pa) else "🔵 —")
                right = (f"`{ow_tier(pb[i]['eff_mmr'])}`{_ow_tag(pb[i])} {pb[i]['member'].mention} 🔴" if i < len(pb) else "— 🔴")
                lines.append(f"{left}  **vs**  {right}")
        e.description = "\n".join(lines)
        notes = []
        if info.get("off"):
            notes.append("⚠️ = '안 함' 포지션 배정 (인원 구성상 불가피)")
        if info.get("sub"):
            notes.append("·부 = 부 포지션")
        if notes:
            e.add_field(name="표시", value=" / ".join(notes), inline=False)
    else:
        if info.get("expected_a") is not None:
            e.description = f"📊 **예상 승률** 🔵 A `{pct(info['expected_a'])}` : `{pct(1 - info['expected_a'])}` B 🔴"
        else:
            e.description = "🎲 랜덤으로 나눈 팀이에요. (기록 없음)"

        def line(p):
            if game == "발로란트":
                return f"{p['member'].mention} `{p['tier']}` {p['wins']}승{p['losses']}패"
            return p["member"].mention
        e.add_field(name="🔵 A팀", value="\n".join(line(p) for p in a) or "—", inline=True)
        e.add_field(name="🔴 B팀", value="\n".join(line(p) for p in b) or "—", inline=True)
    e.set_footer(text=footer or "🔁 자리 바꾸기로 두 사람의 팀·포지션을 맞바꿀 수 있어요. 끝나면 승리 팀을 눌러 주세요.")
    return e


# ═══════════════════════ 상태 관리 ═══════════════════════
class LobbyStore:
    """메모리 캐시 + DB 저장 (봇은 1개 프로세스라 메모리가 기준, DB는 재시작 복구용)"""

    def __init__(self, db):
        self.db = db
        self.data: dict[int, dict] = {}
        self.teams: dict[int, dict] = {}   # 메시지별 참가자 상세 (재시작 후엔 DB에서 다시 구성)
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, mid: int) -> asyncio.Lock:
        return self._locks.setdefault(int(mid), asyncio.Lock())

    async def load(self):
        for row in await self.db.load_lobbies():
            row["participants"] = list(row.get("participants") or [])
            self.data[int(row["message_id"])] = row

    def get(self, mid: int, status: str | None = None) -> dict | None:
        lobby = self.data.get(int(mid))
        return lobby if lobby and (status is None or lobby["status"] == status) else None

    async def save(self, lobby: dict):
        self.data[int(lobby["message_id"])] = lobby
        await self.db.save_lobby(lobby)

    async def drop(self, mid: int):
        self.data.pop(int(mid), None)
        self.teams.pop(int(mid), None)
        self._locks.pop(int(mid), None)
        await self.db.delete_lobby(mid)


async def add_members(cog, lobby: dict, members) -> dict:
    res = {"added": [], "duplicated": [], "no_profile": [], "bots": [], "full": []}
    humans = [m for m in members if not m.bot]
    res["bots"] = [m for m in members if m.bot]
    rows = await cog.db.get_profiles(m.id for m in humans) if lobby["game"] != "롤" else {}
    for m in humans:
        if str(m.id) in lobby["participants"]:
            res["duplicated"].append(m)
        elif lobby["game"] != "롤" and lobby["game"] not in parse_profile(rows.get(m.id)):
            res["no_profile"].append(m)
        elif lobby["game"] == "옵치" and len(lobby["participants"]) >= 10:
            res["full"].append(m)
        else:
            lobby["participants"].append(str(m.id))
            res["added"].append(m)
    return res


def add_result_summary(res: dict, game: str) -> str:
    lines = []
    if res["added"]:
        lines.append(f"✅ 추가됨 ({len(res['added'])}명): " + ", ".join(m.mention for m in res["added"]))
    if res["duplicated"]:
        lines.append("ℹ️ 이미 참가 중: " + ", ".join(m.mention for m in res["duplicated"]))
    if res["no_profile"]:
        lines.append(f"⚠️ **{GAME_LABEL[game]}** 등록이 없어 제외 (`/등록` 필요): " + ", ".join(m.mention for m in res["no_profile"]))
    if res["full"]:
        lines.append("⚠️ 10명이 넘어 제외: " + ", ".join(m.mention for m in res["full"]))
    if res["bots"]:
        lines.append("🤖 봇은 추가할 수 없어요.")
    return "\n".join(lines) or "추가할 인원이 없습니다."


# ═══════════════════════ /모집 첫 화면 ═══════════════════════
class LobbySetupView(discord.ui.View):
    def __init__(self, cog, host: discord.Member, channel, preset: list):
        super().__init__(timeout=300)
        self.cog, self.host, self.channel, self.preset = cog, host, channel, preset
        self.game = "옵치"
        self.game_select = discord.ui.Select(
            placeholder="🎮 내전할 게임 (기본: 오버워치)", row=0,
            options=[discord.SelectOption(label=l, value=v, emoji=e, description=d, default=(v == "옵치")) for l, v, e, d in GAME_OPTIONS],
        )
        self.game_select.callback = self.on_game
        self.add_item(self.game_select)
        self.user_select = discord.ui.UserSelect(placeholder="👥 미리 참여할 인원 (선택 사항)", min_values=0, max_values=25, row=1)
        self.user_select.callback = self.on_users
        self.add_item(self.user_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ 모집을 시작한 방장만 조작할 수 있습니다!", ephemeral=True)
            return False
        return True

    async def on_game(self, interaction: discord.Interaction):
        self.game = self.game_select.values[0]
        await interaction.response.defer()

    async def on_users(self, interaction: discord.Interaction):
        await interaction.response.defer()

    @discord.ui.button(label="확인 (대기실 만들기)", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        members = list({m.id: m for m in self.preset + list(self.user_select.values)}.values())
        lobby, res = await self.cog.open_lobby(interaction.guild, self.channel, self.host.id, self.game, members=members)
        if lobby is None:
            await interaction.edit_original_response(content="❌ 대기실을 저장하지 못했어요. 잠시 후 다시 시도해 주세요.", view=None)
            return
        text = "✅ 내전 대기실을 만들었어요!"
        if members:
            text += "\n\n" + add_result_summary(res, self.game)
        await interaction.edit_original_response(content=text, view=None)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❎ 대기실 만들기를 취소했습니다.", view=None)
        self.stop()


# ═══════════════════════ 방장 전용 임시 메뉴 ═══════════════════════
class AddMembersView(discord.ui.View):
    def __init__(self, cog, lobby_msg: discord.Message):
        super().__init__(timeout=120)
        self.cog, self.lobby_msg = cog, lobby_msg
        self.sel = discord.ui.UserSelect(placeholder="➕ 추가할 인원 선택", min_values=1, max_values=25)
        self.sel.callback = self.on_select
        self.add_item(self.sel)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with self.cog.store.lock(self.lobby_msg.id):
            lobby = self.cog.store.get(self.lobby_msg.id, "waiting")
            if not lobby or interaction.user.id != int(lobby["host_id"]):
                await interaction.edit_original_response(content=EXPIRED if not lobby else "❌ 방장만 추가할 수 있어요.", view=None)
                return
            res = await add_members(self.cog, lobby, list(self.sel.values))
            await self.cog.store.save(lobby)
        await self.lobby_msg.edit(embed=build_lobby_embed(lobby, interaction.guild))
        await interaction.edit_original_response(content=add_result_summary(res, lobby["game"]), view=None)
        self.stop()


class RemoveMembersView(discord.ui.View):
    def __init__(self, cog, lobby_msg: discord.Message, options: list):
        super().__init__(timeout=120)
        self.cog, self.lobby_msg = cog, lobby_msg
        self.sel = discord.ui.Select(placeholder="➖ 제외할 대기자 선택", min_values=1, max_values=len(options), options=options)
        self.sel.callback = self.on_select
        self.add_item(self.sel)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with self.cog.store.lock(self.lobby_msg.id):
            lobby = self.cog.store.get(self.lobby_msg.id, "waiting")
            if not lobby or interaction.user.id != int(lobby["host_id"]):
                await interaction.edit_original_response(content=EXPIRED if not lobby else "❌ 방장만 제외할 수 있어요.", view=None)
                return
            removed = [u for u in self.sel.values if u in lobby["participants"]]
            lobby["participants"] = [u for u in lobby["participants"] if u not in removed]
            await self.cog.store.save(lobby)
        await self.lobby_msg.edit(embed=build_lobby_embed(lobby, interaction.guild))
        await interaction.edit_original_response(
            content=("✅ 제외됨: " + ", ".join(f"<@{u}>" for u in removed)) if removed else "제외할 인원이 없습니다.", view=None)
        self.stop()


def slot_label(game: str, side: str, p: dict) -> str:
    team = "🔵A" if side == "a" else "🔴B"
    if game == "옵치":
        return f"{team} {OW_ROLE_NAME[p['assigned_role']]} · {p['member'].display_name}"[:100]
    return f"{team} · {p['member'].display_name}"[:100]


class SlotSwapView(discord.ui.View):
    """두 사람을 골라 자리(팀 + 포지션)를 맞바꾼다. 같은 팀끼리 고르면 포지션만 바뀐다."""

    def __init__(self, cog, msg: discord.Message, game: str, teams: dict, host_id: int):
        super().__init__(timeout=180)
        self.cog, self.msg, self.game, self.host_id = cog, msg, game, host_id
        self.first = self.second = None
        opts = [discord.SelectOption(label=slot_label(game, side, p), value=f"{side}:{i}")
                for side in ("a", "b") for i, p in enumerate(teams[side])]
        self.s1 = discord.ui.Select(placeholder="① 첫 번째 사람", options=opts, row=0)
        self.s2 = discord.ui.Select(placeholder="② 두 번째 사람 (서로 자리 교환)", options=opts, row=1)
        self.s1.callback = self.pick1
        self.s2.callback = self.pick2
        self.add_item(self.s1)
        self.add_item(self.s2)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.host_id

    async def pick1(self, interaction: discord.Interaction):
        self.first = self.s1.values[0]
        await interaction.response.defer()

    async def pick2(self, interaction: discord.Interaction):
        self.second = self.s2.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="자리 바꾸기 🔁", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.first or not self.second or self.first == self.second:
            await interaction.response.send_message("❌ 서로 다른 두 사람을 골라 주세요.", ephemeral=True)
            return
        async with self.cog.store.lock(self.msg.id):
            lobby = self.cog.store.get(self.msg.id, "teams")
            teams = self.cog.store.teams.get(self.msg.id)
            if not lobby or not teams:
                await interaction.response.edit_message(content=EXPIRED, view=None)
                return
            (s1, i1), (s2, i2) = [(x.split(":")[0], int(x.split(":")[1])) for x in (self.first, self.second)]
            p1, p2 = teams[s1][i1], teams[s2][i2]
            teams[s1][i1], teams[s2][i2] = p2, p1
            if self.game == "옵치":  # 자리를 바꾸면 포지션도 그 자리 것을 이어받는다
                p1["assigned_role"], p2["assigned_role"] = p2["assigned_role"], p1["assigned_role"]
            await self.cog.refresh_info(lobby, teams)
            await self.cog.save_teams(lobby, teams)
        await self.msg.edit(embed=build_teams_embed(self.game, teams))
        await interaction.response.edit_message(content=f"🔁 {p1['member'].mention} ↔ {p2['member'].mention} 자리를 바꿨어요.", view=None)
        self.stop()


# ═══════════════════════ 영구 버튼: 대기실 ═══════════════════════
class LobbyView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _host_lobby(self, interaction: discord.Interaction) -> dict | None:
        lobby = self.cog.store.get(interaction.message.id, "waiting")
        if not lobby:
            await interaction.response.send_message(EXPIRED, ephemeral=True)
            return None
        if interaction.user.id != int(lobby["host_id"]):
            await interaction.response.send_message("❌ 방장만 할 수 있어요.", ephemeral=True)
            return None
        return lobby

    @discord.ui.select(custom_id="lobby:game", placeholder="🎮 게임 변경 (방장)", row=0,
                       options=[discord.SelectOption(label=l, value=v, emoji=e, description=d) for l, v, e, d in GAME_OPTIONS])
    async def game(self, interaction: discord.Interaction, select: discord.ui.Select):
        lobby = await self._host_lobby(interaction)
        if not lobby:
            return
        async with self.cog.store.lock(interaction.message.id):
            lobby["game"] = select.values[0]
            await self.cog.store.save(lobby)
        await interaction.response.edit_message(embed=build_lobby_embed(lobby, interaction.guild), view=self)

    @discord.ui.button(custom_id="lobby:join", label="참가", style=discord.ButtonStyle.success, emoji="✋", row=1)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = interaction.message.id
        lobby = self.cog.store.get(mid, "waiting")
        if not lobby:
            await interaction.response.send_message(EXPIRED, ephemeral=True)
            return
        res = None
        async with self.cog.store.lock(mid):
            res = await add_members(self.cog, lobby, [interaction.user])
            if res["added"]:
                await self.cog.store.save(lobby)
        if res["added"]:
            await interaction.response.edit_message(embed=build_lobby_embed(lobby, interaction.guild), view=self)
        elif res["duplicated"]:
            await interaction.response.send_message("이미 참가 명단에 있습니다!", ephemeral=True)
        elif res["full"]:
            await interaction.response.send_message("❌ 오버워치는 10명까지예요.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ **{GAME_LABEL[lobby['game']]}** 등록이 없어요. `/등록`으로 먼저 등록해 주세요.", ephemeral=True)

    @discord.ui.button(custom_id="lobby:leave", label="취소", style=discord.ButtonStyle.secondary, emoji="🚪", row=1)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = interaction.message.id
        lobby = self.cog.store.get(mid, "waiting")
        uid = str(interaction.user.id)
        if not lobby or uid not in lobby["participants"]:
            await interaction.response.send_message(EXPIRED if not lobby else "참가 명단에 없습니다!", ephemeral=True)
            return
        async with self.cog.store.lock(mid):
            lobby["participants"] = [u for u in lobby["participants"] if u != uid]
            await self.cog.store.save(lobby)
        await interaction.response.edit_message(embed=build_lobby_embed(lobby, interaction.guild), view=self)

    @discord.ui.button(custom_id="lobby:add", label="인원 추가", style=discord.ButtonStyle.secondary, emoji="➕", row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._host_lobby(interaction)
        if lobby:
            await interaction.response.send_message("➕ 추가할 인원을 선택하세요.", view=AddMembersView(self.cog, interaction.message), ephemeral=True)

    @discord.ui.button(custom_id="lobby:remove", label="인원 제외", style=discord.ButtonStyle.secondary, emoji="➖", row=1)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._host_lobby(interaction)
        if not lobby:
            return
        if not lobby["participants"]:
            await interaction.response.send_message("제외할 대기자가 없습니다.", ephemeral=True)
            return
        opts = []
        for u in lobby["participants"][:25]:
            m = interaction.guild.get_member(int(u)) if interaction.guild else None
            opts.append(discord.SelectOption(label=(m.display_name if m else u)[:100], value=u))
        await interaction.response.send_message("➖ 뺄 인원을 선택하세요.", view=RemoveMembersView(self.cog, interaction.message, opts), ephemeral=True)

    @discord.ui.button(custom_id="lobby:start", label="팀 짜기", style=discord.ButtonStyle.primary, emoji="🎲", row=2)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._host_lobby(interaction)
        if not lobby:
            return
        if len(lobby["participants"]) < 2:
            await interaction.response.send_message(f"❌ 최소 2명이 필요해요. (현재 {len(lobby['participants'])}명)", ephemeral=True)
            return
        await interaction.response.defer()
        mid = interaction.message.id
        async with self.cog.store.lock(mid):
            if not self.cog.store.get(mid, "waiting"):
                return
            try:
                teams = await self.cog.make_teams(interaction.guild, lobby["participants"], lobby["game"])
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            lobby["status"] = "teams"
            await self.cog.save_teams(lobby, teams)
        await interaction.edit_original_response(embed=build_teams_embed(lobby["game"], teams), view=self.cog.result_view)

    @discord.ui.button(custom_id="lobby:cancel", label="대기실 폭파", style=discord.ButtonStyle.danger, emoji="💥", row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await self._host_lobby(interaction)
        if not lobby:
            return
        await self.cog.store.drop(interaction.message.id)
        await interaction.response.edit_message(embed=discord.Embed(
            title=f"💥 {GAME_LABEL[lobby['game']]} 대기실이 폭파되었습니다.",
            description=f"방장 <@{lobby['host_id']}>님이 모집을 취소했습니다.", color=discord.Color.red()), view=None)


# ═══════════════════════ 영구 버튼: 팀 화면 ═══════════════════════
class ResultView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _ctx(self, interaction: discord.Interaction, status: str = "teams"):
        lobby = self.cog.store.get(interaction.message.id, status)
        if not lobby:
            await interaction.response.send_message("이미 끝났거나 만료된 경기예요.", ephemeral=True)
            return None
        if interaction.user.id != int(lobby["host_id"]):
            await interaction.response.send_message("❌ 방장만 조작할 수 있어요.", ephemeral=True)
            return None
        teams = await self.cog.get_teams(interaction.guild, lobby)
        if not teams:
            await interaction.response.send_message("❌ 팀 정보를 복구하지 못했어요.", ephemeral=True)
            return None
        return lobby, teams

    async def _record(self, interaction: discord.Interaction, side: str):
        got = await self._ctx(interaction)
        if not got:
            return
        lobby, teams = got
        mid = interaction.message.id
        await interaction.response.defer()
        async with self.cog.store.lock(mid):
            if not self.cog.store.get(mid, "teams"):
                return  # 동시에 두 번 눌림
            lobby["status"] = "done"
            await self.cog.store.save(lobby)
        game = lobby["game"]
        try:
            res = await record_full_match(self.cog.db, game, teams["a"], teams["b"], side, host_id=lobby["host_id"],
                                          recorded_by=interaction.user.id, source="lobby", step=config.OW_MMR_STEP)
        except Exception as e:
            log.error("경기 기록 실패: %s", e)
            lobby["status"] = "teams"
            await self.cog.store.save(lobby)
            await interaction.followup.send("❌ 기록 중 오류가 발생했어요. 다시 눌러 주세요.", ephemeral=True)
            return
        await interaction.edit_original_response(embed=self.cog.result_embed(game, teams, side, res), view=self.cog.done_view)

    @discord.ui.button(custom_id="result:a", label="A팀 승리", style=discord.ButtonStyle.primary, emoji="🔵", row=0)
    async def a_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, "A")

    @discord.ui.button(custom_id="result:b", label="B팀 승리", style=discord.ButtonStyle.danger, emoji="🔴", row=0)
    async def b_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, "B")

    @discord.ui.button(custom_id="result:void", label="무효 / 종료", style=discord.ButtonStyle.secondary, emoji="❌", row=0)
    async def void(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._ctx(interaction):
            return
        await self.cog.store.drop(interaction.message.id)
        await interaction.response.edit_message(content="⚠️ 이 판은 기록 없이 종료했어요.", view=None)

    @discord.ui.button(custom_id="result:swap", label="자리 바꾸기", style=discord.ButtonStyle.secondary, emoji="🔁", row=1)
    async def swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        got = await self._ctx(interaction)
        if got:
            lobby, teams = got
            await interaction.response.send_message(
                "🔁 **두 사람을 고르면 서로의 자리(팀 + 포지션)를 맞바꿔요.** 같은 팀 두 명을 고르면 포지션만 바뀌어요.",
                view=SlotSwapView(self.cog, interaction.message, lobby["game"], teams, int(lobby["host_id"])), ephemeral=True)

    @discord.ui.button(custom_id="result:shuffle", label="다시 섞기", style=discord.ButtonStyle.secondary, emoji="🔀", row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        got = await self._ctx(interaction)
        if not got:
            return
        lobby, teams = got
        await interaction.response.defer()
        async with self.cog.store.lock(interaction.message.id):
            try:
                new = await self.cog.make_teams(interaction.guild, None, lobby["game"], prev=teams, reshuffle=True)
            except ValueError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            await self.cog.save_teams(lobby, new)
        await interaction.edit_original_response(embed=build_teams_embed(lobby["game"], new, new.get("footer")), view=self)

    @discord.ui.button(custom_id="result:voice", label="통화방 이동", style=discord.ButtonStyle.success, emoji="🔊", row=1)
    async def voice(self, interaction: discord.Interaction, button: discord.ui.Button):
        got = await self._ctx(interaction)
        if got:
            await self.cog.move_voice(interaction, got[1])


# ═══════════════════════ 영구 버튼: 결과 후 ═══════════════════════
class DoneView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _next(self, interaction: discord.Interaction, mode: str):
        lobby = self.cog.store.get(interaction.message.id, "done")
        if not lobby:
            await interaction.response.send_message("이미 다음 판으로 넘어갔거나 만료됐어요.", ephemeral=True)
            return
        if interaction.user.id != int(lobby["host_id"]):
            await interaction.response.send_message("❌ 방장만 할 수 있어요.", ephemeral=True)
            return
        await interaction.response.defer()
        mid = interaction.message.id
        async with self.cog.store.lock(mid):
            if not self.cog.store.get(mid, "done"):
                return
            teams = await self.cog.get_teams(interaction.guild, lobby)
            await self.cog.store.drop(mid)
        await interaction.edit_original_response(view=None)  # 이 메시지는 결과 기록으로 남김
        if not teams:
            await interaction.followup.send("❌ 팀 정보를 복구하지 못했어요. `/모집`으로 다시 시작해 주세요.", ephemeral=True)
            return
        ids = [str(p["member"].id) for p in teams["a"] + teams["b"]]
        if mode == "lobby":
            await self.cog.open_lobby(interaction.guild, interaction.channel, int(lobby["host_id"]), lobby["game"], ids=ids)
            return
        try:
            if mode == "same":   # 같은 자리 그대로, 방금 결과가 반영된 점수로 다시 계산
                new = await self.cog.make_teams(interaction.guild, None, lobby["game"], prev=teams, keep_slots=True)
            else:                # 방금 결과 반영해서 처음부터 다시 밸런싱
                new = await self.cog.make_teams(interaction.guild, ids, lobby["game"])
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        new["round"] = teams.get("round", 1) + 1
        msg = await interaction.channel.send(embed=build_teams_embed(lobby["game"], new), view=self.cog.result_view)
        nxt = {"message_id": str(msg.id), "guild_id": str(interaction.guild_id), "channel_id": str(interaction.channel_id),
               "host_id": lobby["host_id"], "game": lobby["game"], "status": "teams", "participants": ids, "teams": None}
        await self.cog.save_teams(nxt, new)

    @discord.ui.button(custom_id="done:same", label="같은 팀으로 한 판 더", style=discord.ButtonStyle.primary, emoji="🔁")
    async def same(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._next(interaction, "same")

    @discord.ui.button(custom_id="done:rebalance", label="다시 밸런싱해서 한 판 더", style=discord.ButtonStyle.success, emoji="🎲")
    async def rebalance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._next(interaction, "rebalance")

    @discord.ui.button(custom_id="done:lobby", label="명단 수정 (대기실로)", style=discord.ButtonStyle.secondary, emoji="📝")
    async def to_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._next(interaction, "lobby")


# ═══════════════════════ Cog ═══════════════════════
class Lobby(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.store = LobbyStore(bot.db)
        self.lobby_view = LobbyView(self)
        self.result_view = ResultView(self)
        self.done_view = DoneView(self)

    async def cog_load(self):
        try:
            await self.db.delete_old_lobbies(config.LOBBY_EXPIRE_HOURS)
            await self.store.load()
            log.info("대기실/경기 %d개 복구", len(self.store.data))
        except Exception as e:
            log.error("대기실 복구 실패: %s", e)
        for v in (self.lobby_view, self.result_view, self.done_view):
            self.bot.add_view(v)

    # ── 대기실 열기 ───────────────────────────────────
    async def open_lobby(self, guild, channel, host_id: int, game: str, members=None, ids: list | None = None):
        msg = await channel.send(embed=discord.Embed(title="⏳ 대기실 준비 중..."), view=self.lobby_view)
        lobby = {"message_id": str(msg.id), "guild_id": str(guild.id), "channel_id": str(channel.id),
                 "host_id": str(host_id), "game": game, "status": "waiting", "participants": list(ids or []), "teams": None}
        res = await add_members(self, lobby, members) if members else None
        try:
            await self.store.save(lobby)
        except Exception as e:
            log.error("대기실 저장 실패: %s", e)
            await msg.delete()
            return None, res
        await msg.edit(embed=build_lobby_embed(lobby, guild))
        return lobby, res

    # ── 팀 구성 ───────────────────────────────────────
    async def build_players(self, guild: discord.Guild, ids: list[str], game: str) -> list:
        rows = await self.db.get_profiles(int(u) for u in ids) if game != "롤" else {}
        players = []
        for u in ids:
            m = guild.get_member(int(u))
            if m is None:
                try:
                    m = await guild.fetch_member(int(u))
                except discord.HTTPException:
                    continue
            if game == "롤" or int(u) in rows:
                players.append(player_from_row(m, rows.get(int(u)), game))
        return players

    async def pair_counts(self, game: str) -> dict:
        if game != "옵치":
            return {}
        try:
            return pair_counts_from_matches(await self.db.recent_matches("옵치", limit=8))
        except Exception:
            return {}

    async def make_teams(self, guild, ids: list | None, game: str, prev: dict | None = None,
                         reshuffle: bool = False, keep_slots: bool = False) -> dict:
        """ids 로 새로 짜거나, prev(이전 팀)를 기준으로 다시 섞기/같은 자리 유지"""
        if prev and keep_slots:
            # 같은 자리 유지: 최신 점수로 참가자 정보만 새로 불러온다
            order = [(s, str(p["member"].id), p.get("assigned_role")) for s in ("a", "b") for p in prev[s]]
            fresh = {str(p["member"].id): p for p in await self.build_players(guild, [x[1] for x in order], game)}
            teams = {"a": [], "b": [], "round": prev.get("round", 1)}
            for side, uid, role in order:
                if uid in fresh:
                    if role:
                        fresh[uid]["assigned_role"] = role
                    teams[side].append(fresh[uid])
            teams["seen"] = {split_key(teams["a"], teams["b"])}
            await self.refresh_info({"game": game}, teams)
            return teams

        players = prev["a"] + prev["b"] if prev else await self.build_players(guild, ids, game)
        if len(players) < 2:
            raise ValueError("참가자 정보를 불러올 수 없어요.")
        seen = set(prev["seen"]) if prev and reshuffle else set()
        a, b, info = balance(players, game, avoid=seen, reshuffle=reshuffle, pair_counts=await self.pair_counts(game))
        key = split_key(a, b)
        footer = None
        if key in seen:  # 가능한 조합을 다 보여줌
            seen = set()
            footer = "가능한 조합을 모두 보여드려서 처음부터 다시 섞었어요."
        seen.add(key)
        return {"a": a, "b": b, "info": info, "seen": seen, "round": prev.get("round", 1) if prev else 1, "footer": footer}

    async def refresh_info(self, lobby: dict, teams: dict):
        """수동 변경 후 예상 승률 등 다시 계산"""
        game = lobby["game"]
        if game == "옵치":
            teams["info"] = reannotate_ow(teams["a"], teams["b"], await self.pair_counts(game))
        elif game == "발로란트":
            from core.balancer import calculate_adjusted_mmr, expected_win
            avg = lambda t: sum(calculate_adjusted_mmr(p) for p in t) / max(len(t), 1)
            teams["info"] = {"expected_a": expected_win(avg(teams["a"]), avg(teams["b"]))}
        else:
            teams["info"] = {"expected_a": None}

    async def save_teams(self, lobby: dict, teams: dict):
        self.store.teams[int(lobby["message_id"])] = teams
        lobby["teams"] = {
            "round": teams.get("round", 1),
            **{k: [{"id": str(p["member"].id), "role": p.get("assigned_role")} for p in teams[k]] for k in ("a", "b")},
        }
        await self.store.save(lobby)

    async def get_teams(self, guild: discord.Guild, lobby: dict) -> dict | None:
        mid = int(lobby["message_id"])
        if mid in self.store.teams:
            return self.store.teams[mid]
        saved = lobby.get("teams") or {}
        if not saved.get("a"):
            return None
        prev = {s: [{"member": discord.Object(int(x["id"])), "assigned_role": x.get("role")} for x in saved[s]] for s in ("a", "b")}
        prev["round"] = saved.get("round", 1)
        teams = await self.make_teams(guild, None, lobby["game"], prev=prev, keep_slots=True)
        self.store.teams[mid] = teams
        return teams

    # ── 결과 화면 ─────────────────────────────────────
    def result_embed(self, game: str, teams: dict, side: str, res: dict) -> discord.Embed:
        title = "🔵 A팀 승리!" if side == "A" else "🔴 B팀 승리!"
        e = build_teams_embed(game, teams, footer="아래 버튼으로 다음 판을 바로 시작할 수 있어요.")
        e.title = f"🏆 {title}  ·  {GAME_LABEL[game]} {teams.get('round', 1)}판째"
        e.color = discord.Color.blue() if side == "A" else discord.Color.red()
        if game == "롤":
            e.add_field(name="기록", value="롤은 기록하지 않아요.", inline=False)
            return e
        exp = res.get("expected_a")
        lines = []
        if exp is not None:
            fav = "A" if exp >= 0.5 else "B"
            upset = (fav != side) and abs(exp - 0.5) >= 0.05
            lines.append(f"경기 전 예상: 🔵 {pct(exp)} : {pct(1 - exp)} 🔴" + (" → **이변!** 🎉" if upset else ""))
        if game == "옵치":
            ups = [f"{e_['name']} {e_['mmr_delta']:+d}" for e_ in res.get("entries", []) if e_.get("mmr_delta")]
            if ups:
                lines.append("점수 변동: " + ", ".join(ups))
        if res.get("match_id"):
            lines.append(f"🧾 경기 **#{res['match_id']}** — 잘못 눌렀다면 `/되돌리기 {res['match_id']}`")
        if res.get("failed"):
            lines.append(f"⚠️ 기록 실패(프로필 없음): {', '.join(res['failed'])}")
        e.add_field(name="기록", value="\n".join(lines)[:1024] or "-", inline=False)
        return e

    async def move_voice(self, interaction: discord.Interaction, teams: dict):
        await interaction.response.defer()
        guild = interaction.guild
        ch_a, ch_b = guild.get_channel(config.VOICE_CHANNEL_A_ID), guild.get_channel(config.VOICE_CHANNEL_B_ID)
        if not ch_a or not ch_b:
            await interaction.followup.send("❌ 통화방을 찾을 수 없어요. config.py의 VOICE_CHANNEL_A_ID / B_ID를 확인해 주세요.", ephemeral=True)
            return
        moved = {"a": 0, "b": 0}
        for key, ch in (("a", ch_a), ("b", ch_b)):
            for p in teams[key]:
                m = guild.get_member(p["member"].id)
                if m and m.voice:
                    try:
                        await m.move_to(ch)
                        moved[key] += 1
                    except discord.HTTPException as e:
                        log.warning("통화방 이동 실패 %s: %s", m, e)
        await interaction.followup.send(f"🔊 이동 완료 — 🔵 A팀 {moved['a']}명 ➡️ {ch_a.mention} · 🔴 B팀 {moved['b']}명 ➡️ {ch_b.mention}")

    @commands.hybrid_command(name="모집", description="내전 대기실을 만듭니다. @태그로 참여할 인원을 미리 넣을 수 있어요.")
    @app_commands.describe(members="미리 참여할 사람을 @태그로 입력 (선택 사항)")
    @app_commands.rename(members="인원")
    @commands.guild_only()
    async def recruit(self, ctx: commands.Context, *, members: Optional[str] = None):
        preset, skipped = [], 0
        for uid in dict.fromkeys(re.findall(r"<@!?(\d+)>", members or "")):
            m = ctx.guild.get_member(int(uid))
            if m is None or m.bot:
                skipped += 1
            else:
                preset.append(m)
        text = "📢 **내전 대기실 만들기** — 게임을 고르고, 미리 참여할 인원을 골라 **확인**을 누르세요. (인원은 나중에 바꿔도 돼요)"
        if preset:
            text += "\n📌 @태그로 지정한 인원: " + ", ".join(m.mention for m in preset)
        if skipped:
            text += f"\n⚠️ 찾을 수 없거나 봇인 대상 {skipped}명은 제외했어요."
        await ctx.send(text, view=LobbySetupView(self, ctx.author, ctx.channel, preset), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Lobby(bot))
