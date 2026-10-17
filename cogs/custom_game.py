import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from db_manager import DBManager, TIER_SCORES, OW_MMR_STEP
from balancer import balance_teams_with_roles, calculate_expected_win_rate, OW_ROLE_LABEL, OW_EXCLUDED_MMR, split_key

try:
    import betting  # 🎰 베팅 시스템 (betting.py) — 없어도 내전 기능은 정상 동작
except Exception as _bet_import_error:  # pragma: no cover
    print(f"⚠️ [베팅 모듈 불러오기 실패] {_bet_import_error}", flush=True)
    betting = None


async def bet_hook(name: str, *args):
    """베팅 연동 호출. 오류가 나도 내전 진행에는 영향을 주지 않는다."""
    if betting is None:
        return
    try:
        await getattr(betting, name)(*args)
    except Exception as e:
        print(f"❌ [베팅 연동 에러:{name}] {e}", flush=True)

# 📌 팀원들을 이동시킬 디스코드 음성 채널(통화방) ID 입력
VOICE_CHANNEL_A_ID = 1558806907706282004  # A팀 통화방 ID
VOICE_CHANNEL_B_ID = 1558807423983292579  # B팀 통화방 ID


def normalize_game_name(game_input: str) -> str:
    g = game_input.lower().strip()
    if g in ["발로란트", "발로", "valorant"]:
        return "발로란트"
    elif g in ["오버워치", "옵치", "overwatch"]:
        return "옵치"
    elif g in ["리그오브레전드", "롤", "lol"]:
        return "롤"
    return game_input


def get_ow_tier(mmr: int) -> str:
    if mmr >= 3500:
        return "0tier"
    elif mmr >= 2800:
        return "1tier"
    elif mmr >= 2300:
        return "2tier"
    elif mmr >= 1800:
        return "3tier"
    elif mmr >= 1300:
        return "4tier"
    else:
        return "5tier"


# 다시 섞기 때 '가장 균형 좋은 조합'에서 허용하는 팀 평균 MMR 차이 여유(점)
RESHUFFLE_TOLERANCE = 30


# 경기 이력(matches)에 저장하는 1명분 기록
def make_entry(member, side, role, is_win: bool, count: int = 1, mmr_delta: int = 0) -> dict:
    return {
        "discord_id": str(member.id),
        "name": member.display_name,
        "side": side,
        "role": role,
        "is_win": is_win,
        "count": count,
        "mmr_delta": mmr_delta,
    }


# 옵치 5명을 포지션 없이 멘션했을 때 자동 배정 순서 (탱 1, 딜 2, 힐 2)
OW_AUTO_ORDER = ("tank", "deal", "deal", "heal", "heal")


def normalize_ow_role(text) -> Optional[str]:
    """탱/탱커/tank, 딜/딜러/dps, 힐/힐러/heal/support -> tank / deal / heal"""
    if not text:
        return None
    t = str(text).strip().lower()
    if t in ("탱", "탱커", "tank"):
        return "tank"
    if t in ("딜", "딜러", "dps", "deal", "damage"):
        return "deal"
    if t in ("힐", "힐러", "heal", "support", "서포터"):
        return "heal"
    return None


def ow_team_power(team: list) -> int:
    """오버워치 팀 파워 = 배정된 포지션 MMR의 합"""
    return sum(p.get(f"{p.get('assigned_role', 'deal')}_mmr", 1000) for p in team)


def build_power_ranking(power_a: int, power_b: int) -> str:
    if power_a == power_b:
        return f"⚡ **파워 랭킹:** 🔵 A팀 `{power_a:,}` = 🔴 B팀 `{power_b:,}` (동률)"
    (n1, s1), (n2, s2) = sorted([("🔵 A팀", power_a), ("🔴 B팀", power_b)], key=lambda x: -x[1])
    return f"⚡ **파워 랭킹** (포지션 MMR 합계)\n🥇 {n1} `{s1:,}`\n🥈 {n2} `{s2:,}` (-{s1 - s2:,})"


# 📊 매치 결과 임베드 생성 헬퍼 함수
def build_match_result_embed(selected_game: str, team_a: list, team_b: list) -> discord.Embed:
    avg_adj_a = sum(p.get("adj_mmr", p.get("mmr", 1000)) for p in team_a) / max(len(team_a), 1)
    avg_adj_b = sum(p.get("adj_mmr", p.get("mmr", 1000)) for p in team_b) / max(len(team_b), 1)

    avg_winrate_a = sum(p.get("win_rate", 50.0) for p in team_a) / max(len(team_a), 1)
    avg_winrate_b = sum(p.get("win_rate", 50.0) for p in team_b) / max(len(team_b), 1)

    exp_win_a, exp_win_b = calculate_expected_win_rate(avg_adj_a, avg_adj_b)

    description = f"📊 **예상 승률:** 🔵 A팀 `{exp_win_a}%` vs 🔴 B팀 `{exp_win_b}%`"
    power_a_str = power_b_str = ""
    if selected_game == "옵치":
        power_a, power_b = ow_team_power(team_a), ow_team_power(team_b)
        description += "\n" + build_power_ranking(power_a, power_b)
        power_a_str = f" · ⚡{power_a:,}"
        power_b_str = f" · ⚡{power_b:,}"

    result_embed = discord.Embed(
        title=f"⚔️ {selected_game.upper()} 내전 자동 팀 구성 완료!",
        description=description,
        color=discord.Color.gold(),
    )

    def format_player_info(p):
        if selected_game == "옵치":
            role = p.get("assigned_role", "deal")
            role_mmr = p.get(f"{role}_mmr", 1000)
            warn = " ⚠️" if p.get("off_role") else ""
            return (
                f"• **[{OW_ROLE_LABEL.get(role, role)}]** {p['member'].mention} "
                f"{get_ow_tier(role_mmr)} ({role_mmr}점){warn}"
            )
        else:
            roles_list = p.get("roles", [])
            roles_str = f"[{', '.join(roles_list)}]" if roles_list else "[자율]"
            p_tier = p.get("tier", "5tier")
            p_wins = p.get("wins", 0)
            p_losses = p.get("losses", 0)
            p_wr = p.get("win_rate", 50.0)
            stats_str = f"{p_wins}승 {p_losses}패 ({p_wr}%)"
            return f"• {p['member'].mention} ({p_tier} / {stats_str} / {roles_str})"

    team_a_str = "\n".join([format_player_info(p) for p in team_a])
    team_b_str = "\n".join([format_player_info(p) for p in team_b])

    result_embed.add_field(
        name=f"🔵 A 팀{power_a_str} (평균 승률 {round(avg_winrate_a, 1)}%)",
        value=team_a_str if team_a_str else "없음",
        inline=True,
    )
    result_embed.add_field(
        name=f"🔴 B 팀{power_b_str} (평균 승률 {round(avg_winrate_b, 1)}%)",
        value=team_b_str if team_b_str else "없음",
        inline=True,
    )

    off_players = [p for p in team_a + team_b if p.get("off_role")]
    if selected_game == "옵치" and off_players:
        names = ", ".join(
            f"{p['member'].display_name}({OW_ROLE_LABEL.get(p['assigned_role'])})" for p in off_players
        )
        result_embed.add_field(
            name="⚠️ 포지션 부족",
            value=f"가능한 포지션만으로는 팀 구성이 안 돼서 미선택 포지션(1000점)에 배정됐어요: {names}",
            inline=False,
        )

    result_embed.set_footer(text=f"내전 종료 후 아래 버튼으로 승리 팀을 기록하세요 (옵치는 포지션별 MMR ±{OW_MMR_STEP}점).")
    return result_embed


# 🔄 팀원 수동 교체 선택 드롭다운 View
class TeamSwapSelectView(discord.ui.View):
    def __init__(self, parent_view, main_message: discord.Message):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.main_message = main_message
        self.selected_a_idx = None
        self.selected_b_idx = None

        options_a = [
            discord.SelectOption(label=f"A팀: {p['member'].display_name}", value=str(i))
            for i, p in enumerate(self.parent_view.team_a)
        ]
        self.select_a = discord.ui.Select(
            placeholder="🔵 A팀에서 교체할 유저 선택",
            options=options_a,
            row=0
        )
        self.select_a.callback = self.on_select_a
        self.add_item(self.select_a)

        options_b = [
            discord.SelectOption(label=f"B팀: {p['member'].display_name}", value=str(i))
            for i, p in enumerate(self.parent_view.team_b)
        ]
        self.select_b = discord.ui.Select(
            placeholder="🔴 B팀에서 교체할 유저 선택",
            options=options_b,
            row=1
        )
        self.select_b.callback = self.on_select_b
        self.add_item(self.select_b)

    async def on_select_a(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.host_id:
            await interaction.response.send_message("❌ 방장만 선택할 수 있습니다!", ephemeral=True)
            return
        self.selected_a_idx = int(self.select_a.values[0])
        await interaction.response.defer()

    async def on_select_b(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.host_id:
            await interaction.response.send_message("❌ 방장만 선택할 수 있습니다!", ephemeral=True)
            return
        self.selected_b_idx = int(self.select_b.values[0])
        await interaction.response.defer()

    @discord.ui.button(label="교체 실행 ⚔️", style=discord.ButtonStyle.success, row=2)
    async def confirm_swap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.parent_view.host_id:
            await interaction.response.send_message("❌ 방장만 팀 교체를 실행할 수 있습니다!", ephemeral=True)
            return

        if self.selected_a_idx is None or self.selected_b_idx is None:
            await interaction.response.send_message("❌ A팀과 B팀에서 각각 교체할 유저를 1명씩 선택해 주세요!", ephemeral=True)
            return

        # 1. 팀원 스왑
        p_a = self.parent_view.team_a[self.selected_a_idx]
        p_b = self.parent_view.team_b[self.selected_b_idx]

        self.parent_view.team_a[self.selected_a_idx] = p_b
        self.parent_view.team_b[self.selected_b_idx] = p_a

        # 2. 결과 임베드 재구성 및 메인 메시지 업데이트
        new_embed = build_match_result_embed(
            self.parent_view.game,
            self.parent_view.team_a,
            self.parent_view.team_b
        )

        await self.main_message.edit(embed=new_embed, view=self.parent_view)
        await interaction.response.send_message(
            f"🔄 **교체 완료!** {p_a['member'].mention} 님과 {p_b['member'].mention} 님의 팀이 교체되었습니다.",
            ephemeral=True
        )
        pv = self.parent_view
        await bet_hook(
            "on_teams_changed", pv.guild_id, pv.channel_id, pv.source_message_id,
            pv.game, pv.host_id, pv.team_a, pv.team_b,
        )


class MatchResultView(discord.ui.View):
    def __init__(self, host_id: int, game: str, team_a: list, team_b: list, db: DBManager):
        super().__init__(timeout=86400)
        self.host_id = int(host_id)
        self.game = game
        self.team_a = team_a
        self.team_b = team_b
        self.db = db
        self.finished = False
        self.seen_splits = {split_key(team_a, team_b)}  # 다시 섞기에서 이미 보여준 조합
        self.source_message_id = None  # 🎰 베팅 연동용: 팀 구성 메시지 ID
        self.channel_id = None
        self.guild_id = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message("❌ 경기 조작 및 결과 기록 권한은 방장에게 있습니다.", ephemeral=True)
            return False
        return True

    async def _record(self, interaction: discord.Interaction, winners: list, losers: list, title: str, winner_side: str):
        if self.finished:
            await interaction.response.send_message("이미 결과가 기록된 경기입니다.", ephemeral=True)
            return
        self.finished = True
        await interaction.response.defer()

        # 🎰 이 경기의 베팅 정산 (승리 팀 적중자 지급 / 실패자 칩 소멸)
        await bet_hook("on_match_finished", self.channel_id, self.source_message_id, winner_side)

        loser_side = "B" if winner_side == "A" else "A"
        failed, entries = [], []
        for team, is_win, side in ((winners, True, winner_side), (losers, False, loser_side)):
            for p in team:
                member = p["member"]
                try:
                    if self.game == "옵치":
                        role = p.get("assigned_role", "deal")
                        res = self.db.record_ow_match_result(member.id, role=role, is_win=is_win)
                        if res is None:
                            failed.append(member.display_name)
                            continue
                        entries.append(make_entry(member, side, role, is_win, 1, res["mmr_delta"]))
                    else:
                        if not self.db.record_match_result(member.id, self.game, is_win=is_win):
                            failed.append(member.display_name)
                            continue
                        entries.append(make_entry(member, side, None, is_win))
                except Exception as e:
                    print(f"❌ [MatchResultView 기록 에러] {member.id}: {e}", flush=True)
                    failed.append(member.display_name)

        match_id = None
        if entries:
            match_id = self.db.save_match(
                self.game, "lobby", interaction.user.id, entries, winner=winner_side, host_id=self.host_id
            )

        detail = f"포지션별 MMR ±{OW_MMR_STEP}점 + 포지션별 승패 반영" if self.game == "옵치" else "승패 횟수 반영"
        msg = f"🎉 **[{title}]** 결과가 기록되었습니다! ({detail})"
        if match_id:
            msg += f"\n🧾 경기 번호 **#{match_id}** — 잘못 눌렀다면 `/되돌리기 {match_id}`"
        elif entries:
            msg += "\n⚠️ 경기 이력 저장에 실패해서 `/되돌리기`를 쓸 수 없어요. (matches 테이블을 확인해 주세요)"
        if failed:
            msg += f"\n⚠️ 기록 실패: {', '.join(failed)}"
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="🔵 A팀 승리", style=discord.ButtonStyle.primary, emoji="🏆", row=0)
    async def team_a_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, self.team_a, self.team_b, "🔵 A팀 승리", "A")

    @discord.ui.button(label="🔴 B팀 승리", style=discord.ButtonStyle.danger, emoji="🏆", row=0)
    async def team_b_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, self.team_b, self.team_a, "🔴 B팀 승리", "B")

    @discord.ui.button(label="❌ 무효 / 취소", style=discord.ButtonStyle.secondary, row=0)
    async def cancel_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await bet_hook("on_match_cancelled", self.channel_id, self.source_message_id)
        await interaction.edit_original_response(content="⚠️ 경기가 전적 반영 없이 종료되었습니다.", view=None)

    # 🔘 팀 수동 교체 버튼 (방장 전용)
    @discord.ui.button(label="팀 수동 교체 🔄", style=discord.ButtonStyle.primary, row=1, custom_id="match_result:swap")
    async def open_swap_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        swap_view = TeamSwapSelectView(parent_view=self, main_message=interaction.message)
        await interaction.response.send_message(
            "🔄 **교체할 유저를 A팀과 B팀에서 각각 1명씩 선택 후 [교체 실행]을 눌러주세요.**",
            view=swap_view,
            ephemeral=True
        )

    # 🔀 팀 다시 섞기 버튼 (방장 전용) — 이미 보여준 조합은 피하고, 균형이 비슷한 다른 조합을 보여준다
    @discord.ui.button(label="다시 섞기 🔀", style=discord.ButtonStyle.secondary, row=1)
    async def reshuffle_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        players = self.team_a + self.team_b
        try:
            new_a, new_b, _ = balance_teams_with_roles(
                players, self.game, avoid=self.seen_splits, tolerance=RESHUFFLE_TOLERANCE
            )
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        key = split_key(new_a, new_b)
        repeated = key in self.seen_splits  # 가능한 조합을 모두 보여줘서 처음부터 다시 도는 경우
        if repeated:
            self.seen_splits = set()
        self.seen_splits.add(key)

        self.team_a, self.team_b = new_a, new_b
        embed = build_match_result_embed(self.game, new_a, new_b)
        if repeated:
            embed.set_footer(text="가능한 조합을 모두 보여드려서 처음부터 다시 섞었어요.")
        await interaction.response.edit_message(embed=embed, view=self)
        await bet_hook(
            "on_teams_changed", self.guild_id, self.channel_id, self.source_message_id,
            self.game, self.host_id, self.team_a, self.team_b,
        )

    # 🔊 통화방 일괄 이동 버튼 (방장 전용)
    @discord.ui.button(label="통화방 이동 🔊", style=discord.ButtonStyle.success, row=1, custom_id="match_result:move_voice")
    async def move_to_voice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ 서버 채널 내에서만 실행할 수 있습니다.", ephemeral=True)
            return

        channel_a = guild.get_channel(VOICE_CHANNEL_A_ID)
        channel_b = guild.get_channel(VOICE_CHANNEL_B_ID)

        if not channel_a or not channel_b:
            await interaction.followup.send("❌ 지정된 음성 채널(통화방)을 찾을 수 없습니다. VOICE_CHANNEL_A_ID / B_ID를 확인해 주세요.", ephemeral=True)
            return

        moved_a, moved_b = 0, 0

        for p in self.team_a:
            member = p.get("member")
            if member and isinstance(member, discord.Member) and member.voice:
                try:
                    await member.move_to(channel_a)
                    moved_a += 1
                except Exception as e:
                    print(f"[A팀 이동 실패] {member.display_name}: {e}")

        for p in self.team_b:
            member = p.get("member")
            if member and isinstance(member, discord.Member) and member.voice:
                try:
                    await member.move_to(channel_b)
                    moved_b += 1
                except Exception as e:
                    print(f"[B팀 이동 실패] {member.display_name}: {e}")

        await interaction.followup.send(
            f"🔊 **팀원 통화방 이동 완료!**\n- 🔵 A팀 ({moved_a}명) ➡️ <#{VOICE_CHANNEL_A_ID}>\n- 🔴 B팀 ({moved_b}명) ➡️ <#{VOICE_CHANNEL_B_ID}>",
            ephemeral=False
        )


class RoleSelectView(discord.ui.View):
    def __init__(self, user_id: int, game: str, tier: str, db: DBManager, next_step_callback):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.game = game
        self.tier = tier
        self.db = db
        self.next_step_callback = next_step_callback

        roles_map = {
            "발로란트": ["타격대", "척후대", "감시자", "전략가"],
            "옵치": ["탱커", "딜러", "힐러"],
            "롤": ["탑", "정글", "미드", "원딜", "서폿"],
        }

        options = [discord.SelectOption(label=r, value=r) for r in roles_map[game]]

        select = discord.ui.Select(
            placeholder=f"[{game}] 주 포지션을 선택하세요 (최대 3개)",
            min_values=1,
            max_values=min(3, len(options)),
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인의 등록 과정만 진행할 수 있습니다.", ephemeral=True)
            return

        selected_roles = interaction.data["values"]
        self.db.set_user_profile(self.user_id, self.game, self.tier, selected_roles)

        if self.next_step_callback:
            await self.next_step_callback(interaction, self.game, self.tier, selected_roles)
        else:
            joined_roles = ", ".join(selected_roles)
            msg = f"🎉 **[{self.game}]** `{self.tier}` / `{joined_roles}` 저장 완료!\n\n모든 프로필 설정이 완료되었습니다!"
            await interaction.response.edit_message(content=msg, view=None)


class TierSelectView(discord.ui.View):
    def __init__(self, user_id: int, game: str, db: DBManager, next_step_callback):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.game = game
        self.db = db
        self.next_step_callback = next_step_callback

        options = [discord.SelectOption(label=tier, value=tier) for tier in TIER_SCORES.keys()]
        select = discord.ui.Select(
            placeholder=f"[{game}] 본인의 티어를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인의 등록 과정만 진행할 수 있습니다.", ephemeral=True)
            return

        selected_tier = interaction.data["values"][0]
        role_view = RoleSelectView(self.user_id, self.game, selected_tier, self.db, self.next_step_callback)

        msg = f"🎯 **[{self.game}]** `{selected_tier}` 선택 완료!\n주 포지션을 선택해 주세요. (최대 3개)"
        await interaction.response.edit_message(content=msg, view=role_view)


class EditGameSelectView(discord.ui.View):
    def __init__(self, user_id: int, db: DBManager):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.db = db

        options = [
            discord.SelectOption(label="발로란트", value="발로란트", emoji="🎯"),
            discord.SelectOption(label="오버워치", value="옵치", emoji="🛡️"),
            discord.SelectOption(label="리그 오브 레전드", value="롤", emoji="⚔️"),
        ]

        select = discord.ui.Select(
            placeholder="수정할 게임을 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인의 수정 과정만 진행할 수 있습니다.", ephemeral=True)
            return

        selected_game = interaction.data["values"][0]
        tier_view = TierSelectView(self.user_id, selected_game, self.db, next_step_callback=None)

        msg = f"✏️ **[{selected_game}]** 새로운 티어를 선택해 주세요."
        await interaction.response.edit_message(content=msg, view=tier_view)


# 🏠 대기실 임베드 생성 (대기실 버튼/인원 추가·제외 등 어디서든 재사용)
async def build_lobby_embed(guild, db: DBManager, message_id: int) -> Optional[discord.Embed]:
    lobby = db.get_lobby(message_id)
    if not lobby:
        return None

    participant_ids = db.get_lobby_participants(message_id)
    selected_game = lobby["selected_game"]
    host_id = int(lobby["host_id"])

    host_member = guild.get_member(host_id) if guild else None
    if not host_member and guild:
        try:
            host_member = await guild.fetch_member(host_id)
        except Exception:
            host_member = None

    host_name = host_member.display_name if host_member else f"유저({host_id})"

    member_mentions = []
    for idx, uid in enumerate(participant_ids, start=1):
        uid_int = int(uid)
        m = guild.get_member(uid_int) if guild else None
        if m:
            member_mentions.append(f"{idx}. {m.mention}")
        else:
            member_mentions.append(f"{idx}. <@{uid_int}>")

    member_list_str = "\n".join(member_mentions)

    embed = discord.Embed(
        title=f"📢 내전 대기실 ({selected_game.upper()})",
        description="아래 버튼을 눌러 내전에 참가하거나 취소하세요!",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name=f"👥 현재 참가자 목록 ({len(participant_ids)}명)",
        value=member_list_str if member_list_str else "아직 참가자가 없습니다.",
        inline=False,
    )
    embed.set_footer(text=f"방장: {host_name}")
    return embed


# ➕ 선택한 유저들을 대기실에 일괄 추가 (봇/중복/프로필 미등록자는 건너뜀)
def add_members_to_lobby(db: DBManager, message_id: int, game: str, members) -> dict:
    existing = {int(p) for p in db.get_lobby_participants(message_id)}
    result = {"added": [], "duplicated": [], "no_profile": [], "bots": []}

    for m in members:
        if getattr(m, "bot", False):
            result["bots"].append(m)
        elif m.id in existing:
            result["duplicated"].append(m)
        elif not db.has_user_profile(m.id, game):
            result["no_profile"].append(m)
        else:
            db.add_participant(message_id, m.id)
            existing.add(m.id)
            result["added"].append(m)
    return result


def summarize_add_result(result: dict, game: str) -> str:
    lines = []
    if result["added"]:
        lines.append(f"✅ 추가됨 ({len(result['added'])}명): " + ", ".join(m.mention for m in result["added"]))
    if result["duplicated"]:
        lines.append("ℹ️ 이미 참가 중: " + ", ".join(m.mention for m in result["duplicated"]))
    if result["no_profile"]:
        lines.append(
            f"⚠️ **[{game}]** 프로필이 없어 제외됨 (`/등록` 필요): "
            + ", ".join(m.mention for m in result["no_profile"])
        )
    if result["bots"]:
        lines.append("🤖 봇은 추가할 수 없어요.")
    return "\n".join(lines) if lines else "추가할 인원이 없습니다."


# 📢 /모집 첫 화면: 게임 + 미리 참여할 인원을 고른 뒤 [확인]을 누르면 대기실 생성
class LobbySetupView(discord.ui.View):
    def __init__(self, db: DBManager, host, channel, preset_members=None):
        super().__init__(timeout=300)
        self.db = db
        self.host = host
        self.channel = channel
        self.preset_members = list(preset_members or [])  # /모집 인원:@닉1 @닉2 로 미리 지정한 인원
        self.game = "발로란트"

        self.game_select = discord.ui.Select(
            placeholder="🎮 내전할 게임 (기본: 발로란트)",
            options=[
                discord.SelectOption(label="발로란트", value="발로란트", emoji="🎯", default=True),
                discord.SelectOption(label="오버워치", value="옵치", emoji="🛡️"),
                discord.SelectOption(label="리그 오브 레전드", value="롤", emoji="⚔️"),
            ],
            row=0,
        )
        self.game_select.callback = self.on_game_select
        self.add_item(self.game_select)

        self.user_select = discord.ui.UserSelect(
            placeholder="👥 미리 참여할 인원 선택 (최대 25명 / 안 골라도 됩니다)",
            min_values=0,
            max_values=25,
            row=1,
        )
        self.user_select.callback = self.on_user_select
        self.add_item(self.user_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host.id:
            await interaction.response.send_message("❌ 모집을 시작한 방장만 조작할 수 있습니다!", ephemeral=True)
            return False
        return True

    async def on_game_select(self, interaction: discord.Interaction):
        self.game = self.game_select.values[0]
        await interaction.response.defer()

    async def on_user_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

    @discord.ui.button(label="확인 (대기실 만들기)", style=discord.ButtonStyle.success, emoji="✅", row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            # @태그로 지정한 인원 + 드롭다운으로 고른 인원 (중복 제거)
            merged = {}
            for m in self.preset_members + list(self.user_select.values):
                merged[m.id] = m
            members = list(merged.values())

            embed = discord.Embed(
                title=f"📢 내전 대기실 ({self.game.upper()})",
                description="아래 버튼을 눌러 내전에 참가하거나 취소하세요!",
                color=discord.Color.blue(),
            )
            embed.add_field(name="👥 현재 참가자 목록 (0명)", value="아직 참가자가 없습니다.", inline=False)
            embed.set_footer(text=f"방장: {self.host.display_name}")

            msg = await self.channel.send(embed=embed, view=PersistentMatchLobbyView(db=self.db))

            self.db.create_lobby(
                message_id=msg.id,
                channel_id=self.channel.id,
                host_id=self.host.id,
                game=self.game,
            )

            result = add_members_to_lobby(self.db, msg.id, self.game, members)

            lobby_embed = await build_lobby_embed(interaction.guild, self.db, msg.id)
            if lobby_embed:
                await msg.edit(embed=lobby_embed)

            text = "✅ 내전 대기실을 만들었어요! 대기실의 **인원 추가 / 인원 제외** 버튼으로 언제든 명단을 바꿀 수 있습니다."
            if members:
                text += "\n\n" + summarize_add_result(result, self.game)
            await interaction.edit_original_response(content=text, view=None)
            self.stop()
        except Exception as e:
            print(f"❌ [LobbySetupView 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 대기실 생성 중 오류가 발생했습니다: {e}", ephemeral=True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❎ 대기실 만들기를 취소했습니다.", view=None)
        self.stop()


# ➕ 대기실에서 방장이 인원을 추가하는 임시(본인에게만 보이는) 메뉴
class LobbyAddMembersView(discord.ui.View):
    def __init__(self, db: DBManager, lobby_message: discord.Message):
        super().__init__(timeout=120)
        self.db = db
        self.lobby_message = lobby_message

        self.user_select = discord.ui.UserSelect(
            placeholder="➕ 추가할 인원 선택 (최대 25명)",
            min_values=1,
            max_values=25,
        )
        self.user_select.callback = self.on_select
        self.add_item(self.user_select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            lobby = self.db.get_lobby(self.lobby_message.id)
            if not lobby:
                await interaction.edit_original_response(content="❌ 만료되었거나 이미 종료된 대기실입니다.", view=None)
                return
            if interaction.user.id != int(lobby["host_id"]):
                await interaction.edit_original_response(content="❌ 인원 추가 권한은 방장에게 있습니다.", view=None)
                return

            game = lobby["selected_game"]
            result = add_members_to_lobby(self.db, self.lobby_message.id, game, list(self.user_select.values))

            embed = await build_lobby_embed(interaction.guild, self.db, self.lobby_message.id)
            if embed:
                await self.lobby_message.edit(embed=embed)

            await interaction.edit_original_response(content=summarize_add_result(result, game), view=None)
            self.stop()
        except Exception as e:
            print(f"❌ [LobbyAddMembersView 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 인원 추가 중 오류가 발생했습니다: {e}", ephemeral=True)


# ➖ 대기실에서 방장이 대기자를 빼는 임시(본인에게만 보이는) 메뉴
class LobbyRemoveMembersView(discord.ui.View):
    def __init__(self, db: DBManager, lobby_message: discord.Message, options: list):
        super().__init__(timeout=120)
        self.db = db
        self.lobby_message = lobby_message

        self.member_select = discord.ui.Select(
            placeholder="➖ 제외할 대기자 선택",
            min_values=1,
            max_values=len(options),
            options=options,
        )
        self.member_select.callback = self.on_select
        self.add_item(self.member_select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            lobby = self.db.get_lobby(self.lobby_message.id)
            if not lobby:
                await interaction.edit_original_response(content="❌ 만료되었거나 이미 종료된 대기실입니다.", view=None)
                return
            if interaction.user.id != int(lobby["host_id"]):
                await interaction.edit_original_response(content="❌ 인원 제외 권한은 방장에게 있습니다.", view=None)
                return

            current = {int(p) for p in self.db.get_lobby_participants(self.lobby_message.id)}
            removed = []
            for v in self.member_select.values:
                uid = int(v)
                if uid in current:
                    self.db.remove_participant(self.lobby_message.id, uid)
                    removed.append(uid)

            embed = await build_lobby_embed(interaction.guild, self.db, self.lobby_message.id)
            if embed:
                await self.lobby_message.edit(embed=embed)

            text = "✅ 제외됨: " + ", ".join(f"<@{u}>" for u in removed) if removed else "제외할 인원이 없습니다."
            await interaction.edit_original_response(content=text, view=None)
            self.stop()
        except Exception as e:
            print(f"❌ [LobbyRemoveMembersView 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 인원 제외 중 오류가 발생했습니다: {e}", ephemeral=True)


class PersistentGameSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="발로란트", value="발로란트", description="승률/티어 기반 팀 밸런싱", emoji="🎯"),
            discord.SelectOption(label="오버워치", value="옵치", description="포지션별 MMR 기반 팀 밸런싱", emoji="🛡️"),
            discord.SelectOption(label="리그 오브 레전드", value="롤", description="승률/티어 기반 팀 밸런싱", emoji="⚔️"),
        ]
        super().__init__(
            placeholder="🎮 내전할 게임을 선택하세요!",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="persistent_lobby:game_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            msg_id = interaction.message.id
            lobby = self.view.db.get_lobby(msg_id)
            if not lobby:
                await interaction.followup.send("❌ 만료되었거나 종료된 대기실입니다.", ephemeral=True)
                return

            if interaction.user.id != int(lobby["host_id"]):
                await interaction.followup.send("❌ 내전 게임 변경 권한은 방장에게 있습니다.", ephemeral=True)
                return

            selected_game = self.values[0]
            self.view.db.update_lobby_game(msg_id, selected_game)
            await self.view.update_lobby_embed(interaction, msg_id)
        except Exception as e:
            print(f"❌ [PersistentGameSelect 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 게임 변경 실패: {e}", ephemeral=True)


class PersistentMatchLobbyView(discord.ui.View):
    def __init__(self, db: DBManager):
        super().__init__(timeout=None)
        self.db = db
        self.add_item(PersistentGameSelect())

    async def update_lobby_embed(self, interaction: discord.Interaction, message_id: int):
        try:
            embed = await build_lobby_embed(interaction.guild, self.db, message_id)
            if embed is None:
                return

            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            print(f"❌ [update_lobby_embed 에러] {e}", flush=True)

    @discord.ui.button(label="참가 신청", style=discord.ButtonStyle.success, emoji="✋", custom_id="persistent_lobby:join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            msg_id = interaction.message.id
            lobby = self.db.get_lobby(msg_id)
            if not lobby:
                await interaction.followup.send("❌ 만료되었거나 이미 종료된 대기실입니다.", ephemeral=True)
                return

            selected_game = lobby["selected_game"]

            if not self.db.has_user_profile(interaction.user.id, selected_game):
                msg = f"❌ **[{selected_game}]** 프로필 정보가 등록되어 있지 않습니다!\n`!등록` 또는 `/등록` 명령어를 통해 프로필을 설정해 주세요."
                await interaction.followup.send(msg, ephemeral=True)
                return

            participants = [int(p) for p in self.db.get_lobby_participants(msg_id)]
            if interaction.user.id in participants:
                await interaction.followup.send("이미 참가 명단에 있습니다!", ephemeral=True)
                return

            self.db.add_participant(msg_id, interaction.user.id)
            await self.update_lobby_embed(interaction, msg_id)
        except Exception as e:
            print(f"❌ [join_button 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 참가 처리 중 오류가 발생했습니다: {e}", ephemeral=True)

    @discord.ui.button(label="참가 취소", style=discord.ButtonStyle.secondary, emoji="🚪", custom_id="persistent_lobby:leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            msg_id = interaction.message.id
            lobby = self.db.get_lobby(msg_id)
            if not lobby:
                await interaction.followup.send("❌ 만료되었거나 이미 종료된 대기실입니다.", ephemeral=True)
                return

            participants = [int(p) for p in self.db.get_lobby_participants(msg_id)]
            if interaction.user.id not in participants:
                await interaction.followup.send("참가 명단에 없습니다!", ephemeral=True)
                return

            self.db.remove_participant(msg_id, interaction.user.id)
            await self.update_lobby_embed(interaction, msg_id)
        except Exception as e:
            print(f"❌ [leave_button 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 취소 처리 중 오류가 발생했습니다: {e}", ephemeral=True)

    @discord.ui.button(label="인원 추가", style=discord.ButtonStyle.secondary, emoji="➕", row=0, custom_id="persistent_lobby:add_members")
    async def add_members_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = self.db.get_lobby(interaction.message.id)
        if not lobby:
            await interaction.response.send_message("❌ 만료되었거나 이미 종료된 대기실입니다.", ephemeral=True)
            return
        if interaction.user.id != int(lobby["host_id"]):
            await interaction.response.send_message("❌ 인원 추가 권한은 방장에게 있습니다.", ephemeral=True)
            return

        view = LobbyAddMembersView(self.db, interaction.message)
        await interaction.response.send_message(
            f"➕ **[{lobby['selected_game']}]** 대기실에 추가할 인원을 선택하세요.", view=view, ephemeral=True
        )

    @discord.ui.button(label="인원 제외", style=discord.ButtonStyle.secondary, emoji="➖", row=0, custom_id="persistent_lobby:remove_members")
    async def remove_members_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = self.db.get_lobby(interaction.message.id)
        if not lobby:
            await interaction.response.send_message("❌ 만료되었거나 이미 종료된 대기실입니다.", ephemeral=True)
            return
        if interaction.user.id != int(lobby["host_id"]):
            await interaction.response.send_message("❌ 인원 제외 권한은 방장에게 있습니다.", ephemeral=True)
            return

        participant_ids = [int(p) for p in self.db.get_lobby_participants(interaction.message.id)]
        if not participant_ids:
            await interaction.response.send_message("제외할 대기자가 없습니다.", ephemeral=True)
            return

        options = []
        for uid in participant_ids[:25]:  # 드롭다운 한도 25
            m = interaction.guild.get_member(uid) if interaction.guild else None
            name = m.display_name if m else f"유저({uid})"
            options.append(discord.SelectOption(label=name[:100], value=str(uid)))

        note = "\n(참가자가 25명을 넘어 앞의 25명만 표시됩니다.)" if len(participant_ids) > 25 else ""
        view = LobbyRemoveMembersView(self.db, interaction.message, options)
        await interaction.response.send_message(f"➖ 대기실에서 뺄 인원을 선택하세요.{note}", view=view, ephemeral=True)

    @discord.ui.button(label="팀 짜기 시작", style=discord.ButtonStyle.primary, emoji="🎲", row=2, custom_id="persistent_lobby:start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            msg_id = interaction.message.id
            lobby = self.db.get_lobby(msg_id)
            if not lobby:
                await interaction.followup.send("❌ 만료되었거나 이미 종료된 대기실입니다.", ephemeral=True)
                return

            if interaction.user.id != int(lobby["host_id"]):
                await interaction.followup.send("❌ 내전을 시작할 권한은 방장에게 있습니다.", ephemeral=True)
                return

            participant_ids = [int(p) for p in self.db.get_lobby_participants(msg_id)]
            if len(participant_ids) < 2:
                msg = f"❌ 최소 2명 이상이 모집되어야 합니다. (현재: {len(participant_ids)}명)"
                await interaction.followup.send(msg, ephemeral=True)
                return

            selected_game = lobby["selected_game"]

            player_data = []
            for uid in participant_ids:
                member = interaction.guild.get_member(uid) if interaction.guild else None
                if not member and interaction.guild:
                    try:
                        member = await interaction.guild.fetch_member(uid)
                    except Exception:
                        member = None

                if member:
                    profile = self.db.get_user_profile(uid, selected_game)
                    player_data.append({
                        "member": member,
                        "tier": profile.get("tier", "5tier"),
                        "mmr": profile.get("mmr", 1000),
                        "tank_mmr": profile.get("tank_mmr", 1000),
                        "deal_mmr": profile.get("deal_mmr", 1000),
                        "heal_mmr": profile.get("heal_mmr", 1000),
                        "roles": profile.get("roles", []),
                        "wins": profile.get("wins", 0),
                        "losses": profile.get("losses", 0),
                        "win_rate": profile.get("win_rate", 50.0),
                    })

            if len(player_data) < 2:
                await interaction.followup.send("❌ 참가자의 정보를 불러올 수 없습니다.", ephemeral=True)
                return

            team_a, team_b, diff = balance_teams_with_roles(player_data, selected_game)

            # 📌 헬퍼 함수로 통합된 결과 임베드 생성
            result_embed = build_match_result_embed(selected_game, team_a, team_b)

            self.db.delete_lobby(msg_id)

            result_view = MatchResultView(int(lobby["host_id"]), selected_game, team_a, team_b, self.db)
            await interaction.edit_original_response(embed=result_embed, view=result_view)

            # 🎰 팀이 확정되면 이 경기의 베팅 마켓을 연다 (관전자는 /배팅 으로 참여)
            result_view.source_message_id = msg_id
            result_view.channel_id = interaction.channel_id
            result_view.guild_id = interaction.guild_id
            await bet_hook(
                "on_match_started", interaction.guild_id, interaction.channel_id, msg_id,
                selected_game, int(lobby["host_id"]), team_a, team_b,
            )
        except Exception as e:
            print(f"❌ [start_button 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 팀 구성 중 오류가 발생했습니다: {e}", ephemeral=True)

    @discord.ui.button(label="대기실 폭파", style=discord.ButtonStyle.danger, emoji="💥", row=2, custom_id="persistent_lobby:cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            msg_id = interaction.message.id
            lobby = self.db.get_lobby(msg_id)
            if not lobby:
                await interaction.followup.send("❌ 만료되었거나 이미 종료된 대기실입니다.", ephemeral=True)
                return

            if interaction.user.id != int(lobby["host_id"]):
                await interaction.followup.send("❌ 내전을 취소할 권한은 방장에게 있습니다.", ephemeral=True)
                return

            selected_game = lobby["selected_game"]
            host_id = int(lobby["host_id"])
            host_member = interaction.guild.get_member(host_id) if interaction.guild else None
            host_mention = host_member.mention if host_member else f"<@{host_id}>"

            cancel_embed = discord.Embed(
                title=f"💥 {selected_game.upper()} 내전 대기실이 폭파되었습니다.",
                description=f"방장 {host_mention}님에 의해 내전 모집이 취소되었습니다.",
                color=discord.Color.red(),
            )

            self.db.delete_lobby(msg_id)
            await interaction.edit_original_response(embed=cancel_embed, view=None)
        except Exception as e:
            print(f"❌ [cancel_button 에러] {e}", flush=True)
            await interaction.followup.send(f"❌ 대기실 폭파 중 오류가 발생했습니다: {e}", ephemeral=True)


class CustomGame(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = DBManager()
        self.bot.add_view(PersistentMatchLobbyView(self.db))

    @commands.hybrid_command(name="초기화", description="[소유자] 데이터베이스의 모든 데이터를 초기화합니다.")
    @commands.is_owner()
    async def clear_database(self, ctx: commands.Context):
        if hasattr(self.db, "clear_all_data"):
            self.db.clear_all_data()
        await ctx.send("🧹 데이터베이스의 모든 데이터가 초기화되었습니다!", ephemeral=True)

    @commands.hybrid_command(name="수정", description="특정 게임의 티어 및 포지션 정보만 수정합니다.")
    async def edit_registration(self, ctx: commands.Context):
        view = EditGameSelectView(ctx.author.id, self.db)
        await ctx.send(f"✏️ {ctx.author.mention}님, 프로필을 수정할 게임을 선택하세요.", view=view, ephemeral=True)

    @commands.hybrid_command(name="등록", description="발로란트 -> 롤 순서대로 티어 및 포지션을 연속 등록합니다.")
    async def start_registration(self, ctx: commands.Context):
        async def step2_lol(interaction: discord.Interaction, prev_game, prev_tier, prev_roles):
            view = TierSelectView(ctx.author.id, "롤", self.db, None)
            joined_prev = ", ".join(prev_roles)
            msg = f"✅ **[{prev_game}]** `{prev_tier}` / `{joined_prev}` 저장 완료!\n\n2단계: **[리그 오브 레전드]** 티어를 선택하세요."
            await interaction.response.edit_message(content=msg, view=view)

        view = TierSelectView(ctx.author.id, "발로란트", self.db, step2_lol)
        await ctx.send(f"📝 {ctx.author.mention}님, 프로필 등록을 시작합니다.\n1단계: **[발로란트]** 티어를 선택하세요.", view=view, ephemeral=True)

    @commands.hybrid_command(name="모집", description="내전 대기실을 만듭니다. @태그로 참여할 인원을 미리 넣을 수 있어요.")
    @app_commands.describe(members="미리 참여할 사람을 @태그로 입력하세요 (예: @철수 @영희) — 선택 사항")
    @app_commands.rename(members="인원")
    async def start_lobby(self, ctx: commands.Context, *, members: Optional[str] = None):
        preset = []
        not_found = 0
        if members and ctx.guild:
            seen = set()
            for uid in re.findall(r"<@!?(\d+)>", members):
                uid = int(uid)
                if uid in seen:
                    continue
                seen.add(uid)
                m = ctx.guild.get_member(uid)
                if m is None:
                    try:
                        m = await ctx.guild.fetch_member(uid)
                    except Exception:
                        m = None
                if m is None or m.bot:
                    not_found += 1
                    continue
                preset.append(m)

        view = LobbySetupView(self.db, ctx.author, ctx.channel, preset_members=preset)
        text = (
            "📢 **내전 대기실 만들기**\n"
            "게임을 고르고, 아래 메뉴에서 미리 참여할 인원을 **@태그처럼 검색해서** 골라 주세요. 다 골랐으면 **확인**을 누르세요.\n"
            "(인원은 안 골라도 되고, 만든 뒤에도 대기실의 **인원 추가 / 인원 제외** 버튼으로 바꿀 수 있어요.)"
        )
        if preset:
            text += "\n\n📌 **@태그로 미리 지정한 인원:** " + ", ".join(m.mention for m in preset)
        if not_found:
            text += f"\n⚠️ 찾을 수 없거나 봇인 대상 {not_found}명은 제외했어요."
        await ctx.send(text, view=view, ephemeral=True)

    @commands.hybrid_command(name="조회", description="특정 유저(또는 본인)의 MMR, 티어, 전적 프로필을 조회합니다.")
    async def view_profile(self, ctx: commands.Context, target: discord.Member = None):
        await ctx.defer()

        target_user = target or ctx.author
        all_profiles = self.db.get_user_all_games(target_user.id)

        embed = discord.Embed(
            title=f"👤 {target_user.display_name}님의 내전 프로필",
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=target_user.display_avatar.url)

        if not all_profiles:
            embed.description = "❌ 아직 등록된 게임 프로필이 없습니다. `!등록`으로 시작해 보세요!"
            await ctx.send(embed=embed)
            return

        for game, p in all_profiles.items():
            if game == "옵치":
                tank_m = p.get("tank_mmr", 1000)
                deal_m = p.get("deal_mmr", 1000)
                heal_m = p.get("heal_mmr", 1000)
                ow_wins = p.get("wins", 0)
                ow_losses = p.get("losses", 0)
                ow_wr = p.get("win_rate", 50.0)
                role_lines = []
                for label, key, mmr in (("탱커", "tank", tank_m), ("딜러", "deal", deal_m), ("힐러", "heal", heal_m)):
                    if mmr == OW_EXCLUDED_MMR:  # 1000점 = 하지 않는 포지션은 숨김
                        continue
                    rw, rl = p.get(f"{key}_wins", 0), p.get(f"{key}_losses", 0)
                    rwr = f" ({round(rw / (rw + rl) * 100)}%)" if rw + rl > 0 else ""
                    role_lines.append(f"• **{label}:** `{get_ow_tier(mmr)}` ({mmr}점) · `{rw}승 {rl}패`{rwr}")
                if not role_lines:
                    role_lines = ["• 등록된 포지션 점수가 없습니다."]
                value_str = (
                    "\n".join(role_lines)
                    + f"\n• **전체 전적:** `{ow_wins}승 {ow_losses}패` (승률 {ow_wr}%)"
                )
            else:
                roles_list = p.get("roles", [])
                roles_str = ", ".join(roles_list) if roles_list else "자율"
                tier_val = p.get("tier", "5tier")
                mmr_val = p.get("mmr", 1000)
                wins_val = p.get("wins", 0)
                losses_val = p.get("losses", 0)
                wr_val = p.get("win_rate", 50.0)
                value_str = (
                    f"• **티어:** `{tier_val}` (MMR: {mmr_val}점)\n"
                    f"• **전적:** `{wins_val}승 {losses_val}패` (승률 {wr_val}%)\n"
                    f"• **포지션:** `{roles_str}`"
                )

            embed.add_field(name=f"🎮 {game.UPPER() if hasattr(game, 'UPPER') else game.upper()}", value=value_str, inline=False)

        await ctx.send(embed=embed)

    FORCE_USAGE = "사용법: `!강제수정 @유저 <발로란트|롤|옵치> <티어|MMR|탱|딜|힐> <값>`\n예) `!강제수정 @유저 옵치 힐 1700` / `!강제수정 @유저 발로란트 티어 골드`"

    @commands.hybrid_command(name="강제수정", description="[소유자 전용] 유저의 발로란트/롤/옵치 MMR을 강제로 수정합니다.")
    @app_commands.describe(
        target="수정할 유저",
        game="게임",
        field="발로·롤: 티어 또는 MMR / 옵치: 탱·딜·힐",
        value="티어 이름(예: 골드) 또는 MMR 숫자",
    )
    @app_commands.choices(
        game=[app_commands.Choice(name=g, value=g) for g in ("발로란트", "롤", "옵치")],
        field=[app_commands.Choice(name=f, value=f) for f in ("티어", "MMR", "탱", "딜", "힐")],
    )
    async def force_edit(self, ctx: commands.Context, target: discord.Member, game: str, field: str, value: str):
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)
            return

        game = normalize_game_name(game)
        field = "MMR" if field.strip().lower() == "mmr" else field.strip()
        value = value.strip()

        row = self.db._get_user_row(target.id)
        if not row:
            await ctx.send(f"❌ {target.display_name}님은 DB에 프로필이 없습니다. 먼저 `/등록`이 필요해요.", ephemeral=True)
            return

        def parse_mmr(text: str):
            try:
                n = int(text)
            except ValueError:
                return None
            return n if 0 <= n <= 10000 else None

        if game == "옵치":
            col_map = {"탱": "tank_mmr", "딜": "deal_mmr", "힐": "heal_mmr"}
            if field not in col_map:
                await ctx.send(f"❌ 옵치는 항목을 `탱`/`딜`/`힐` 중에서 골라주세요.\n{self.FORCE_USAGE}", ephemeral=True)
                return
            new_mmr = parse_mmr(value)
            if new_mmr is None:
                await ctx.send("❌ 값은 0~10000 사이의 숫자여야 합니다.", ephemeral=True)
                return
            col = col_map[field]
            old_mmr = row.get(col)
            old_mmr = 1000 if old_mmr is None else old_mmr
            payload = {col: new_mmr}
            summary = f"**[옵치 {field}]** `{old_mmr}점` → `{new_mmr}점` ({get_ow_tier(new_mmr)})"

        elif game in ("발로란트", "롤"):
            prefix = "val" if game == "발로란트" else "lol"
            old_tier = row.get(f"{prefix}_tier")
            old_mmr = row.get(f"{prefix}_mmr")
            old_mmr = 1000 if old_mmr is None else old_mmr
            if field == "티어":
                valid = [t for t in TIER_SCORES if not t.endswith("tier")]
                if value not in valid:
                    await ctx.send(f"❌ 티어 이름이 올바르지 않습니다.\n사용 가능: {', '.join(valid)}", ephemeral=True)
                    return
                new_mmr = TIER_SCORES[value]
                payload = {f"{prefix}_tier": value, f"{prefix}_mmr": new_mmr}
                summary = f"**[{game} 티어]** `{old_tier or '없음'}({old_mmr}점)` → `{value}({new_mmr}점)`"
            elif field == "MMR":
                if not old_tier:
                    await ctx.send(f"❌ {game} 티어가 등록되어 있지 않아요. `티어`로 먼저 설정해 주세요.", ephemeral=True)
                    return
                new_mmr = parse_mmr(value)
                if new_mmr is None:
                    await ctx.send("❌ 값은 0~10000 사이의 숫자여야 합니다.", ephemeral=True)
                    return
                payload = {f"{prefix}_mmr": new_mmr}
                summary = f"**[{game} MMR]** `{old_mmr}점` → `{new_mmr}점` (티어 `{old_tier}` 유지)"
            else:
                await ctx.send(f"❌ {game}의 항목은 `티어`/`MMR` 중에서 골라주세요.\n{self.FORCE_USAGE}", ephemeral=True)
                return
        else:
            await ctx.send(f"❌ 게임은 `발로란트`/`롤`/`옵치` 중에서 골라주세요.\n{self.FORCE_USAGE}", ephemeral=True)
            return

        if self.db.force_update_profile(target.id, payload):
            await ctx.send(f"✅ {target.mention} {summary}", ephemeral=True)
        else:
            await ctx.send("❌ DB 수정 중 오류가 발생했습니다. 콘솔 로그를 확인해 주세요.", ephemeral=True)

    @force_edit.error
    async def force_edit_error(self, ctx: commands.Context, error):
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"❌ 입력이 올바르지 않습니다.\n{self.FORCE_USAGE}", ephemeral=True)

    RECORD_USAGE = (
        "사용법: `!승패추가 <발로란트|롤|옵치> <승|패> @유저1 @유저2 ... [횟수] [탱|딜|힐]` 또는 `/승패추가`\n"
        "예) `!승패추가 옵치 승 @A @B 힐` / `!승패추가 롤 패 @A 3` / `!승패추가 옵치 패 @A 2 탱`\n"
        "※ 옵치에서 포지션을 적으면 그 포지션 MMR(±20)과 포지션별 전적까지 반영돼요. (순서: 횟수 → 포지션)\n"
        "※ 옵치에서 포지션 없이 **5명**을 멘션하면 순서대로 탱·딜·딜·힐·힐로 자동 배정돼요."
    )

    async def _apply_match_records(self, game_input: str, result: str, members: list, count, role_input=None, recorded_by=None) -> str:
        """!승패추가 / /승패추가 공통 로직. 사용자에게 보낼 메시지를 반환"""
        game = normalize_game_name(game_input)
        if game not in ("발로란트", "롤", "옵치"):
            return f"❌ 게임은 `발로란트`/`롤`/`옵치` 중에서 골라주세요.\n{self.RECORD_USAGE}"

        r = result.strip().lower()
        if r in ("승", "win", "w"):
            is_win = True
        elif r in ("패", "lose", "loss", "l"):
            is_win = False
        else:
            return f"❌ 결과는 `승` 또는 `패`로 입력해 주세요.\n{self.RECORD_USAGE}"

        if not members:
            return f"❌ 대상 유저를 멘션해 주세요.\n{self.RECORD_USAGE}"
        if count is None or not 1 <= count <= 50:
            return "❌ 횟수는 1~50 사이의 숫자여야 합니다."

        role = normalize_ow_role(role_input)
        if role_input:
            if game != "옵치":
                return f"❌ 포지션은 옵치에서만 지정할 수 있어요.\n{self.RECORD_USAGE}"
            if role is None:
                return f"❌ 포지션은 `탱`/`딜`/`힐` 중에서 골라주세요.\n{self.RECORD_USAGE}"

        # 옵치 + 포지션 미지정 + 정확히 5명 -> 멘션 순서대로 탱·딜·딜·힐·힐 자동 배정
        unique = list({m.id: m for m in members}.values())  # 중복 멘션 제거(ID 기준, 순서 유지)
        auto_roles = None
        if game == "옵치" and not role and len(members) == len(OW_AUTO_ORDER):
            if len(unique) != len(members):
                return "❌ 같은 유저가 중복으로 멘션됐어요. 5명을 순서대로(탱·딜·딜·힐·힐) 멘션해 주세요."
            auto_roles = OW_AUTO_ORDER

        done, skipped, entries = [], [], []
        for idx, m in enumerate(unique):
            if not self.db.has_user_profile(m.id, game):
                skipped.append(f"{m.display_name}(미등록)")
                continue
            m_role = role or (auto_roles[idx] if auto_roles else None)
            if m_role:
                res = self.db.record_ow_match_result(m.id, role=m_role, is_win=is_win, count=count)
                if res is None:
                    skipped.append(f"{m.display_name}(DB 오류)")
                else:
                    done.append(
                        f"{m.display_name} → {OW_ROLE_LABEL[m_role]} {res['mmr']}점 "
                        f"({res['role_wins']}승 {res['role_losses']}패) / 전체 {res['wins']}승 {res['losses']}패"
                    )
                    entries.append(make_entry(m, None, m_role, is_win, count, res["mmr_delta"]))
            else:
                res = self.db.add_match_record(
                    m.id, game, wins=count if is_win else 0, losses=0 if is_win else count
                )
                if res is None:
                    skipped.append(f"{m.display_name}(DB 오류)")
                else:
                    done.append(f"{m.display_name} → {res[0]}승 {res[1]}패")
                    entries.append(make_entry(m, None, None, is_win, count))

        label = "승" if is_win else "패"
        if role:
            role_tag = f" {OW_ROLE_LABEL[role]}"
        elif auto_roles:
            role_tag = " " + "·".join(OW_ROLE_LABEL[r] for r in auto_roles) + " 순서 배정"
        else:
            role_tag = ""
        lines = []
        if done:
            lines.append(f"✅ **[{game}{role_tag}]** {label} +{count} 반영 ({len(done)}명)\n" + "\n".join(f"• {d}" for d in done))
        if skipped:
            lines.append(f"⚠️ 반영 안 됨: {', '.join(skipped)}")
        if entries:
            match_id = self.db.save_match(game, "manual", recorded_by, entries, winner=None, host_id=recorded_by)
            if match_id:
                lines.append(f"🧾 경기 번호 **#{match_id}** — 잘못 넣었다면 `/되돌리기 {match_id}`")
            else:
                lines.append("⚠️ 경기 이력 저장에 실패해서 `/되돌리기`를 쓸 수 없어요. (matches 테이블을 확인해 주세요)")
        if game == "옵치" and done and not role and not auto_roles:
            lines.append(
                "ℹ️ 포지션이 없어 전체 전적만 올랐어요. 포지션(탱/딜/힐)을 적거나, "
                "5명을 탱·딜·딜·힐·힐 순서로 멘션하면 포지션 MMR과 포지션별 전적도 반영돼요."
            )
        return "\n".join(lines)

    @commands.command(name="승패추가", help="[소유자 전용] 봇 없이 진행한 내전의 승/패를 직접 추가합니다.", ignore_extra=False)
    async def add_record(
        self,
        ctx: commands.Context,
        game: str,
        result: str,
        members: commands.Greedy[discord.Member],
        count: Optional[int] = 1,
        role: Optional[str] = None,
    ):
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ 소유자만 사용할 수 있는 명령어입니다.")
            return
        await ctx.send(await self._apply_match_records(game, result, list(members), count, role, ctx.author.id))

    @add_record.error
    async def add_record_error(self, ctx: commands.Context, error):
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.TooManyArguments)):
            await ctx.send(f"❌ 입력이 올바르지 않습니다.\n{self.RECORD_USAGE}")

    @app_commands.command(name="승패추가", description="[소유자 전용] 봇 없이 진행한 내전의 승/패를 직접 추가합니다.")
    @app_commands.describe(
        game="게임",
        result="승 또는 패",
        count="추가할 횟수 (기본 1)",
        role="[옵치 전용] 포지션 (지정하면 포지션 MMR ±20과 포지션별 전적도 반영)",
        **{f"user{i}": f"대상 유저 {i}" for i in range(1, 11)},
    )
    @app_commands.choices(
        game=[app_commands.Choice(name=g, value=g) for g in ("발로란트", "롤", "옵치")],
        result=[app_commands.Choice(name=r, value=r) for r in ("승", "패")],
        role=[app_commands.Choice(name=r, value=r) for r in ("탱", "딜", "힐")],
    )
    async def add_record_slash(
        self,
        interaction: discord.Interaction,
        game: str,
        result: str,
        user1: discord.Member,
        user2: Optional[discord.Member] = None,
        user3: Optional[discord.Member] = None,
        user4: Optional[discord.Member] = None,
        user5: Optional[discord.Member] = None,
        user6: Optional[discord.Member] = None,
        user7: Optional[discord.Member] = None,
        user8: Optional[discord.Member] = None,
        user9: Optional[discord.Member] = None,
        user10: Optional[discord.Member] = None,
        count: app_commands.Range[int, 1, 50] = 1,
        role: Optional[str] = None,
    ):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)
            return
        members = [m for m in (user1, user2, user3, user4, user5, user6, user7, user8, user9, user10) if m]
        await interaction.response.defer()
        await interaction.followup.send(await self._apply_match_records(game, result, members, count, role, interaction.user.id))

    @commands.hybrid_command(name="전체목록", description="[소유자 전용] DB에 등록된 모든 유저의 게임별 MMR/전적을 조회합니다.")
    @commands.is_owner()
    async def list_all_profiles(self, ctx: commands.Context):
        all_data = self.db.get_all_user_profiles()
        if not all_data:
            await ctx.send("📂 DB에 등록된 유저 프로필이 없습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📂 DB 전체 유저 프로필 현황",
            description="서버 닉네임 기준으로 정렬된 유저 데이터 목록입니다.",
            color=discord.Color.purple(),
        )

        user_grouped = {}
        for item in all_data:
            uid = item.get("user_id")
            if uid not in user_grouped:
                user_grouped[uid] = []
            user_grouped[uid].append(item)

        for uid, profiles in user_grouped.items():
            try:
                uid_int = int(uid)
            except (ValueError, TypeError):
                continue

            member = ctx.guild.get_member(uid_int)
            display_name = f"{member.mention} ({member.display_name})" if member else f"<@{uid_int}>"

            info_lines = []
            for p in profiles:
                if p.get("game") == "옵치":
                    t_m = p.get("tank_mmr", 1000)
                    d_m = p.get("deal_mmr", 1000)
                    h_m = p.get("heal_mmr", 1000)
                    info_lines.append(
                        f"• **[옵치]** 탱:`{get_ow_tier(t_m)}`({t_m}점) | 딜:`{get_ow_tier(d_m)}`({d_m}점) | 힐:`{get_ow_tier(h_m)}`({h_m}점)"
                    )
                else:
                    g_name = str(p.get("game", "")).upper()
                    tier_v = p.get("tier", "5tier")
                    mmr_v = p.get("mmr", 1000)
                    w_v = p.get("wins", 0)
                    l_v = p.get("losses", 0)
                    wr_v = p.get("win_rate", 50.0)
                    info_lines.append(
                        f"• **[{g_name}]** `{tier_v}` ({mmr_v}점) | `{w_v}승 {l_v}패` ({wr_v}%)"
                    )

            member_title = member.display_name if member else "알 수 없음"
            embed.add_field(
                name=f"👤 {member_title}",
                value=f"유저: {display_name}\n" + "\n".join(info_lines),
                inline=False,
            )

        await ctx.send(embed=embed, ephemeral=True)


# -------------------------------------------------------------
# Extension Setup 함수
# -------------------------------------------------------------
async def setup(bot):
    await bot.add_cog(CustomGame(bot))
