"""프로필: /등록(발로 티어·포지션 → 옵치 포지션 선호·티어), /수정, /조회, /옵치연동(재미용)
소유자: /강제수정, /승패추가, /옵치경기기록, /전체목록"""
import asyncio
import datetime
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from core.overfast import OWError, normalize_battletag
from core.ranks import (
    GAMES, OW_PREF_NAME, OW_RANKS, OW_ROLE_EMOJI, OW_ROLE_NAME, OW_ROLES, VAL_ROLES, VAL_TIERS,
    normalize_game, normalize_ow_role, ow_rank_label, ow_rank_mmr, ow_tier, parse_profile, player_from_row,
)
from core.recording import record_full_match

log = logging.getLogger("rakko.profile")
KST = datetime.timezone(datetime.timedelta(hours=9))
OW_LOCK_GAMES = 5  # 이 판수 이상 뛴 포지션은 본인이 티어를 다시 고를 수 없음 (내전으로 쌓은 점수 보호)
OW_AUTO_ORDER = ("tank", "deal", "deal", "heal", "heal")


def _own_check(user_id: int):
    async def check(interaction: discord.Interaction) -> bool:
        if interaction.user.id != user_id:
            await interaction.response.send_message("❌ 본인의 등록 과정만 진행할 수 있습니다.", ephemeral=True)
            return False
        return True
    return check


# ═══════════════════════ 발로란트 등록 ═══════════════════════
class ValorantView(discord.ui.View):
    """티어 → 포지션. 끝나면 on_done(interaction, 결과문구)"""

    def __init__(self, db, user_id: int, on_done):
        super().__init__(timeout=300)
        self.db, self.user_id, self.on_done = db, user_id, on_done
        self.interaction_check = _own_check(user_id)
        sel = discord.ui.Select(
            placeholder="🎯 [발로란트] 티어를 선택하세요",
            options=[discord.SelectOption(label=t, value=t) for t in VAL_TIERS]
                    + [discord.SelectOption(label="발로란트 안 함", value="_none", emoji="🚫")],
        )
        sel.callback = self.pick_tier
        self.add_item(sel)
        self.tier = None

    async def pick_tier(self, interaction: discord.Interaction):
        self.tier = interaction.data["values"][0]
        if self.tier == "_none":
            await self.db.ensure_profile(interaction.user)
            await self.db.update_profile(self.user_id, {"val_tier": None})
            await self.on_done(interaction, "🎯 발로란트: 안 함")
            self.stop()
            return
        self.clear_items()
        sel = discord.ui.Select(placeholder="🎯 [발로란트] 주 포지션 (최대 3개)", min_values=1, max_values=3,
                                options=[discord.SelectOption(label=r, value=r) for r in VAL_ROLES])
        sel.callback = self.pick_roles
        self.add_item(sel)
        await interaction.response.edit_message(content=f"🎯 **발로란트** `{self.tier}` — 주 포지션을 골라 주세요.", view=self)

    async def pick_roles(self, interaction: discord.Interaction):
        roles = interaction.data["values"]
        await self.db.ensure_profile(interaction.user)
        await self.db.update_profile(self.user_id, {"val_tier": self.tier, "val_mmr": VAL_TIERS[self.tier], "val_roles": roles})
        await self.on_done(interaction, f"🎯 발로란트: `{self.tier}` / {', '.join(roles)}")
        self.stop()


# ═══════════════════════ 오버워치 등록 ═══════════════════════
class OverwatchView(discord.ui.View):
    """탱/딜/힐 각각: 주·부·안 함 + (경기 수가 적으면) 대략 티어"""

    def __init__(self, db, user_id: int, row: dict | None, on_done, is_owner: bool = False):
        super().__init__(timeout=300)
        self.db, self.user_id, self.on_done = db, user_id, on_done
        self.interaction_check = _own_check(user_id)
        cur = parse_profile(row).get("옵치")
        self.choice: dict[str, str | None] = {}
        for i, r in enumerate(OW_ROLES):
            pref = cur["prefs"][r] if cur else None
            mmr = cur[f"{r}_mmr"] if cur else None
            games = cur[f"{r}_games"] if cur else 0
            opts = []
            if cur and (pref != "no" or games > 0):
                opts += [discord.SelectOption(label=f"주 포지션 · 점수 유지 ({ow_tier(mmr)})", value="main:keep"),
                         discord.SelectOption(label=f"부 포지션 · 점수 유지 ({ow_tier(mmr)})", value="sub:keep")]
            if games < OW_LOCK_GAMES or is_owner:
                for pk, pname in (("main", "주"), ("sub", "부")):
                    opts += [discord.SelectOption(label=f"{pname} 포지션 · {ko}", value=f"{pk}:{key}") for key, ko, _ in OW_RANKS]
            opts.append(discord.SelectOption(label="안 함", value="no", emoji="🚫"))
            default = (f"{pref}:keep" if pref and pref != "no" and any(o.value == f"{pref}:keep" for o in opts)
                       else ("no" if pref == "no" else None))
            for o in opts:
                o.default = o.value == default
            self.choice[r] = default
            note = "" if games < OW_LOCK_GAMES or is_owner else f" ({games}판 뛰어서 점수 유지만 가능)"
            sel = discord.ui.Select(placeholder=f"{OW_ROLE_EMOJI[r]} {OW_ROLE_NAME[r]}{note}", options=opts[:25], row=i)
            sel.callback = self._make_cb(r, sel)
            self.add_item(sel)

    def _make_cb(self, role: str, sel: discord.ui.Select):
        async def cb(interaction: discord.Interaction):
            self.choice[role] = sel.values[0]
            await interaction.response.defer()
        return cb

    @discord.ui.button(label="저장", style=discord.ButtonStyle.success, emoji="✅", row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        missing = [OW_ROLE_NAME[r] for r in OW_ROLES if not self.choice.get(r)]
        if missing:
            await interaction.response.send_message(f"❌ {', '.join(missing)}도 골라 주세요. (안 하면 '안 함')", ephemeral=True)
            return
        prefs, payload, parts = {}, {}, []
        for r in OW_ROLES:
            v = self.choice[r]
            if v == "no":
                prefs[r] = "no"
                continue
            pref, rank = v.split(":")
            prefs[r] = pref
            if rank != "keep":
                payload[f"{r}_mmr"] = ow_rank_mmr(rank)
            parts.append(f"{OW_ROLE_NAME[r]}({OW_PREF_NAME[pref]})")
        payload["ow_prefs"] = prefs
        await self.db.ensure_profile(interaction.user)
        await self.db.update_profile(self.user_id, payload)
        summary = "🛡️ 오버워치: " + (", ".join(parts) if parts else "안 함")
        if parts and not any(p == "main" for p in prefs.values()):
            summary += " (주 포지션이 없어서 모두 부 포지션으로 배정돼요)"
        await self.on_done(interaction, summary)
        self.stop()


# ═══════════════════════ 오버워치 랭크 (재미용) ═══════════════════════
def ow_rank_embed(member, tag: str, ranks: dict) -> discord.Embed:
    if ranks.get("private"):
        desc = "🔒 프로필이 **비공개**라 랭크를 볼 수 없어요.\n오버워치 → 옵션 → 소셜 → 커리어 프로필을 **공개**로 바꾸면 보여요."
    elif not ranks.get("platform"):
        desc = "이번 시즌 경쟁전 배치 기록이 없어요."
    else:
        desc = f"🎮 {'PC' if ranks['platform'] == 'pc' else '콘솔'} · 시즌 {ranks.get('season') or '?'}"
    e = discord.Embed(title=f"🛡️ {member.display_name}님의 오버워치 랭크", description=desc, color=discord.Color.orange())
    e.set_author(name=tag)
    if ranks.get("platform"):
        for r in OW_ROLES:
            e.add_field(name=OW_ROLE_NAME[r], value=ow_rank_label(ranks.get(r)), inline=True)
    e.set_footer(text="재미용 표시예요 · 내전 팀 밸런스에는 반영되지 않아요. 매일 05시 자동 갱신")
    return e


# ═══════════════════════ Cog ═══════════════════════
class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.ow = bot.ow

    async def cog_load(self):
        self.refresh_ow_ranks.start()

    async def cog_unload(self):
        self.refresh_ow_ranks.cancel()

    # ── 등록 / 수정 ───────────────────────────────────
    @commands.hybrid_command(name="등록", description="발로란트 티어·포지션 → 오버워치 포지션(주/부/안 함)·티어를 등록합니다.")
    async def register(self, ctx: commands.Context):
        is_owner = await self.bot.is_owner(ctx.author)

        async def to_ow(interaction: discord.Interaction, val_summary: str):
            row = await self.db.get_profile(ctx.author.id)

            async def finish(i2: discord.Interaction, ow_summary: str):
                await i2.response.edit_message(content=f"✅ **등록 완료!**\n{val_summary}\n{ow_summary}", view=None)

            await interaction.response.edit_message(
                content=(f"{val_summary}\n\n2단계: **오버워치** 포지션마다 주/부/안 함과 대략적인 티어를 골라 주세요.\n"
                         "티어는 첫 점수로만 쓰이고, 내전 결과에 따라 자동으로 맞춰져요."),
                view=OverwatchView(self.db, ctx.author.id, row, finish, is_owner),
            )

        await ctx.send(f"📝 {ctx.author.mention}님, 프로필 등록을 시작합니다.\n1단계: **발로란트** 티어를 선택하세요.",
                       view=ValorantView(self.db, ctx.author.id, to_ow), ephemeral=True)

    @commands.hybrid_command(name="수정", description="발로란트 또는 오버워치 정보만 다시 등록합니다.")
    @app_commands.describe(game="수정할 게임")
    @app_commands.choices(game=[app_commands.Choice(name=g, value=g) for g in GAMES])
    async def edit(self, ctx: commands.Context, game: str):
        game = normalize_game(game)

        async def done(interaction: discord.Interaction, summary: str):
            await interaction.response.edit_message(content=f"✅ 수정 완료! {summary}", view=None)

        if game == "발로란트":
            view = ValorantView(self.db, ctx.author.id, done)
        elif game == "옵치":
            view = OverwatchView(self.db, ctx.author.id, await self.db.get_profile(ctx.author.id), done,
                                 await self.bot.is_owner(ctx.author))
        else:
            await ctx.send("❌ `발로란트` 또는 `옵치`를 골라 주세요.", ephemeral=True)
            return
        await ctx.send(f"✏️ **{game}** 정보를 다시 골라 주세요.", view=view, ephemeral=True)

    # ── 조회 ──────────────────────────────────────────
    @commands.hybrid_command(name="조회", description="유저의 발로란트·오버워치 내전 정보를 봅니다.")
    async def view(self, ctx: commands.Context, target: discord.Member = None):
        await ctx.defer()
        user = target or ctx.author
        g = parse_profile(await self.db.get_profile(user.id))
        e = discord.Embed(title=f"👤 {user.display_name}님의 내전 프로필", color=discord.Color.green())
        e.set_thumbnail(url=user.display_avatar.url)
        if "발로란트" in g:
            p = g["발로란트"]
            e.add_field(name="🎯 발로란트", inline=False, value=(
                f"• **티어:** `{p['tier']}` · **포지션:** {', '.join(p['roles']) or '자율'}\n"
                f"• **전적:** `{p['wins']}승 {p['losses']}패` (승률 {p['win_rate']}%)"))
        ranks = {}
        if "옵치" in g:
            p = g["옵치"]
            ranks = p["ranks"]
            lines = []
            for r in OW_ROLES:
                pref = p["prefs"][r]
                if pref == "no":
                    continue
                w, l = p[f"{r}_wins"], p[f"{r}_losses"]
                wr = f" ({round(w / (w + l) * 100)}%)" if w + l else ""
                lines.append(f"{OW_ROLE_EMOJI[r]} **{OW_ROLE_NAME[r]}** ({OW_PREF_NAME[pref]}) · `{ow_tier(p[f'{r}_mmr'])}` "
                             f"{p[f'{r}_mmr']}점 · {w}승 {l}패{wr}")
            lines.append(f"• **전체:** `{p['wins']}승 {p['losses']}패` (승률 {p['win_rate']}%)")
            e.add_field(name="🛡️ 오버워치", value="\n".join(lines), inline=False)
        elif "_ow_display" in g:
            ranks = g["_ow_display"]["ranks"]
        if ranks.get("platform"):
            e.add_field(name="🎮 오버워치 인게임 랭크 (재미용)", inline=False,
                        value=" · ".join(f"{OW_ROLE_NAME[r]} {ow_rank_label(ranks.get(r))}" for r in OW_ROLES))
        if not e.fields:
            e.description = "❌ 아직 등록된 정보가 없어요. `/등록`으로 시작해 보세요!"
        await ctx.send(embed=e)

    # ── 오버워치 인게임 랭크 (재미용) ─────────────────
    @commands.hybrid_command(name="옵치연동", description="배틀태그로 오버워치 인게임 티어를 불러와 /조회에 표시합니다. (재미용)")
    @app_commands.describe(battletag="예) 닉네임#1234 (비우면 저장된 배틀태그로 새로고침)", target="[소유자] 다른 유저 대신 연동")
    @app_commands.rename(battletag="배틀태그", target="대상")
    async def ow_link(self, ctx: commands.Context, battletag: Optional[str] = None, target: Optional[discord.Member] = None):
        await ctx.defer(ephemeral=True)
        member = target or ctx.author
        if member.id != ctx.author.id and not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ 다른 사람의 연동은 소유자만 할 수 있어요.", ephemeral=True)
            return
        row = await self.db.ensure_profile(member)
        tag = normalize_battletag(battletag) if battletag else (row or {}).get("ow_battletag")
        if not tag:
            await ctx.send("❌ 배틀태그를 입력해 주세요. 예) `/옵치연동 배틀태그:닉네임#1234`", ephemeral=True)
            return
        try:
            ranks = await self.ow.fetch_ranks(tag)
        except OWError as e:
            await ctx.send(f"❌ {e}", ephemeral=True)
            return
        await self.db.update_profile(member.id, {"ow_battletag": tag, "ow_ranks": ranks, "ow_ranks_at": "now()"})
        await ctx.send(embed=ow_rank_embed(member, tag, ranks), ephemeral=True)

    @tasks.loop(time=datetime.time(hour=config.OW_RANK_REFRESH_HOUR, minute=0, tzinfo=KST))
    async def refresh_ow_ranks(self):
        try:
            rows = await self.db.select("user_profiles", select="discord_id,ow_battletag",
                                        ow_battletag="not.is.null", in_guild="is.true")
        except Exception as e:
            log.error("옵치 랭크 갱신 대상 조회 실패: %s", e)
            return
        for r in rows:
            try:
                ranks = await self.ow.fetch_ranks(r["ow_battletag"])
                await self.db.update_profile(int(r["discord_id"]), {"ow_ranks": ranks, "ow_ranks_at": "now()"})
            except Exception as e:
                log.warning("옵치 랭크 갱신 실패 %s: %s", r["ow_battletag"], e)
            await asyncio.sleep(2)  # 공용 API 부하 방지

    @refresh_ow_ranks.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ── 소유자: 강제수정 ──────────────────────────────
    FORCE_USAGE = "사용법: `/강제수정 @유저 <발로란트|옵치> <티어|MMR|탱|딜|힐> <값>`\n예) `/강제수정 @유저 옵치 힐 2700` / `/강제수정 @유저 발로란트 티어 골드`"

    @commands.hybrid_command(name="강제수정", description="[소유자] 발로란트 티어/MMR, 옵치 포지션 MMR을 강제로 수정합니다.")
    @app_commands.describe(target="수정할 유저", game="게임", field="발로: 티어/MMR, 옵치: 탱/딜/힐", value="티어 이름 또는 숫자")
    @app_commands.choices(
        game=[app_commands.Choice(name=g, value=g) for g in GAMES],
        field=[app_commands.Choice(name=f, value=f) for f in ("티어", "MMR", "탱", "딜", "힐")],
    )
    @commands.is_owner()
    async def force_edit(self, ctx: commands.Context, target: discord.Member, game: str, field: str, value: str):
        game, field, value = normalize_game(game), field.strip(), value.strip()
        row = await self.db.get_profile(target.id)
        if not row:
            await ctx.send(f"❌ {target.display_name}님은 DB에 프로필이 없습니다.", ephemeral=True)
            return
        num = int(value) if value.isdigit() and int(value) <= 10000 else None

        if game == "옵치":
            role = normalize_ow_role(field)
            if not role or num is None:
                await ctx.send(f"❌ 옵치는 `탱`/`딜`/`힐` + 0~10000 숫자로 입력해 주세요.\n{self.FORCE_USAGE}", ephemeral=True)
                return
            old = row.get(f"{role}_mmr") or 1000
            payload = {f"{role}_mmr": num}
            summary = f"**[옵치 {OW_ROLE_NAME[role]}]** `{old}점` → `{num}점` ({ow_tier(num)})"
        elif game == "발로란트":
            if field == "티어":
                if value not in VAL_TIERS:
                    await ctx.send(f"❌ 티어 이름이 올바르지 않습니다. 사용 가능: {', '.join(VAL_TIERS)}", ephemeral=True)
                    return
                payload = {"val_tier": value, "val_mmr": VAL_TIERS[value]}
                summary = f"**[발로란트 티어]** `{row.get('val_tier') or '없음'}` → `{value}`"
            elif field.upper() == "MMR" and num is not None and row.get("val_tier"):
                payload = {"val_mmr": num}
                summary = f"**[발로란트 MMR]** `{row.get('val_mmr')}점` → `{num}점`"
            else:
                await ctx.send(f"❌ 발로란트는 `티어` 또는 `MMR`(티어 등록 후)로 입력해 주세요.\n{self.FORCE_USAGE}", ephemeral=True)
                return
        else:
            await ctx.send(f"❌ 게임은 `발로란트`/`옵치` 중에서 골라주세요.\n{self.FORCE_USAGE}", ephemeral=True)
            return
        ok = await self.db.update_profile(target.id, payload)
        await ctx.send(f"✅ {target.mention} {summary}" if ok else "❌ DB 수정 중 오류가 발생했습니다.", ephemeral=True)

    @force_edit.error
    async def force_edit_error(self, ctx, error):
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"❌ 입력이 올바르지 않습니다.\n{self.FORCE_USAGE}", ephemeral=True)

    # ── 소유자: 옵치 경기 단위 기록 (④) ───────────────
    @app_commands.command(name="옵치경기기록", description="[소유자] 봇 없이 한 옵치 내전 1판을 팀·포지션 단위로 기록합니다.")
    @app_commands.describe(
        winner="이긴 팀",
        a_tank="🔵 A팀 탱커", a_deal1="🔵 A팀 딜러", a_deal2="🔵 A팀 딜러", a_heal1="🔵 A팀 힐러", a_heal2="🔵 A팀 힐러",
        b_tank="🔴 B팀 탱커", b_deal1="🔴 B팀 딜러", b_deal2="🔴 B팀 딜러", b_heal1="🔴 B팀 힐러", b_heal2="🔴 B팀 힐러",
    )
    @app_commands.rename(
        winner="승리팀", a_tank="a탱커", a_deal1="a딜러1", a_deal2="a딜러2", a_heal1="a힐러1", a_heal2="a힐러2",
        b_tank="b탱커", b_deal1="b딜러1", b_deal2="b딜러2", b_heal1="b힐러1", b_heal2="b힐러2",
    )
    @app_commands.choices(winner=[app_commands.Choice(name="🔵 A팀", value="A"), app_commands.Choice(name="🔴 B팀", value="B")])
    async def ow_match_record(
        self, interaction: discord.Interaction, winner: str,
        a_tank: discord.Member, a_deal1: discord.Member, a_deal2: discord.Member, a_heal1: discord.Member, a_heal2: discord.Member,
        b_tank: discord.Member, b_deal1: discord.Member, b_deal2: discord.Member, b_heal1: discord.Member, b_heal2: discord.Member,
    ):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("❌ 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)
            return
        slots_a = [(a_tank, "tank"), (a_deal1, "deal"), (a_deal2, "deal"), (a_heal1, "heal"), (a_heal2, "heal")]
        slots_b = [(b_tank, "tank"), (b_deal1, "deal"), (b_deal2, "deal"), (b_heal1, "heal"), (b_heal2, "heal")]
        ids = [m.id for m, _ in slots_a + slots_b]
        if len(set(ids)) != len(ids):
            await interaction.response.send_message("❌ 같은 사람이 두 자리에 들어갔어요.", ephemeral=True)
            return
        await interaction.response.defer()
        rows = await self.db.get_profiles(ids)
        missing = [m.display_name for m in (s[0] for s in slots_a + slots_b) if "옵치" not in parse_profile(rows.get(m.id))]
        if missing:
            await interaction.followup.send(f"❌ 옵치 등록이 안 된 사람이 있어요 (`/등록` 필요): {', '.join(missing)}")
            return

        def team(slots):
            out = []
            for m, role in slots:
                p = player_from_row(m, rows[m.id], "옵치")
                p["assigned_role"] = role
                out.append(p)
            return out

        res = await record_full_match(self.db, "옵치", team(slots_a), team(slots_b), winner, host_id=interaction.user.id,
                                      recorded_by=interaction.user.id, source="manual_match", step=config.OW_MMR_STEP)
        lines = [f"✅ **옵치 1판 기록** · {'🔵 A팀' if winner == 'A' else '🔴 B팀'} 승리 · 경기 전 예상 승률 A {res['expected_a'] * 100:.0f}%"]
        for e in res.get("entries", []):
            lines.append(f"{'🔵' if e['side'] == 'A' else '🔴'} {OW_ROLE_EMOJI[e['role']]} {e['name']} {e['mmr_delta']:+d} → {e['mmr']}점")
        if res.get("match_id"):
            lines.append(f"🧾 경기 번호 **#{res['match_id']}** — 잘못 넣었다면 `/되돌리기 {res['match_id']}`")
        await interaction.followup.send("\n".join(lines)[:1990])

    # ── 소유자: 개인별 승패추가 (상대 정보 없음 → 고정 ±점수) ─────
    RECORD_USAGE = (
        "사용법: `!승패추가 <발로란트|옵치> <승|패> @유저1 @유저2 ... [횟수] [탱|딜|힐]`\n"
        "※ 팀 단위로 한 판을 넣을 땐 `/옵치경기기록`이 더 정확해요 (예상 승률 기반 점수 변동)."
    )

    async def _apply_records(self, game_in, result, members, count, role_in, by) -> str:
        game = normalize_game(game_in)
        if game not in GAMES:
            return f"❌ 게임은 `발로란트`/`옵치` 중에서 골라주세요.\n{self.RECORD_USAGE}"
        r = str(result).strip().lower()
        if r not in ("승", "패", "win", "lose", "w", "l"):
            return f"❌ 결과는 `승` 또는 `패`로 입력해 주세요.\n{self.RECORD_USAGE}"
        is_win = r in ("승", "win", "w")
        if not members:
            return f"❌ 대상 유저를 멘션해 주세요.\n{self.RECORD_USAGE}"
        if count is None or not 1 <= count <= 50:
            return "❌ 횟수는 1~50 사이의 숫자여야 합니다."
        role = normalize_ow_role(role_in)
        if role_in and (game != "옵치" or role is None):
            return f"❌ 포지션은 옵치에서 `탱`/`딜`/`힐` 중에서만 지정할 수 있어요.\n{self.RECORD_USAGE}"
        unique = list({m.id: m for m in members}.values())
        auto = OW_AUTO_ORDER if game == "옵치" and not role and len(unique) == len(OW_AUTO_ORDER) else None
        rows = await self.db.get_profiles(m.id for m in unique)
        entries, skipped = [], []
        for i, m in enumerate(unique):
            if game not in parse_profile(rows.get(m.id)):
                skipped.append(f"{m.display_name}(미등록)")
                continue
            entries.append({"discord_id": str(m.id), "name": m.display_name, "side": None,
                            "role": role or (auto[i] if auto else None), "is_win": is_win, "count": count})
        if not entries:
            return "⚠️ 반영할 인원이 없어요. " + ", ".join(skipped)
        res = await self.db.record_match(game, "manual", entries, host_id=by, recorded_by=by, step=config.OW_MMR_STEP)
        done = [f"{e['name']} → " + (f"{OW_ROLE_NAME[e['role']]} {e['mmr']}점 " if e.get("role") else "")
                + f"{e['wins']}승 {e['losses']}패" for e in res.get("entries", [])]
        skipped += [f"{n}(DB 없음)" for n in res.get("failed", [])]
        lines = [f"✅ **[{game}]** {'승' if is_win else '패'} +{count} 반영 ({len(done)}명)\n" + "\n".join(f"• {d}" for d in done)]
        if skipped:
            lines.append(f"⚠️ 반영 안 됨: {', '.join(skipped)}")
        if res.get("match_id"):
            lines.append(f"🧾 경기 번호 **#{res['match_id']}** — 잘못 넣었다면 `/되돌리기 {res['match_id']}`")
        return "\n".join(lines)[:1990]

    @commands.command(name="승패추가", ignore_extra=False)
    @commands.is_owner()
    async def add_record(self, ctx, game: str, result: str, members: commands.Greedy[discord.Member],
                         count: Optional[int] = 1, role: Optional[str] = None):
        await ctx.send(await self._apply_records(game, result, list(members), count, role, ctx.author.id))

    @add_record.error
    async def add_record_error(self, ctx, error):
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ 소유자만 사용할 수 있는 명령어입니다.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.TooManyArguments)):
            await ctx.send(f"❌ 입력이 올바르지 않습니다.\n{self.RECORD_USAGE}")

    # ── 소유자: 전체목록 ──────────────────────────────
    @commands.hybrid_command(name="전체목록", description="[소유자] 서버 멤버 전원의 발로란트/옵치 내전 정보를 봅니다.")
    @commands.is_owner()
    async def list_all(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        lines = []
        for row in await self.db.all_profiles():
            g = parse_profile(row)
            parts = []
            if "발로란트" in g:
                p = g["발로란트"]
                parts.append(f"발로 `{p['tier']}` {p['wins']}승{p['losses']}패")
            if "옵치" in g:
                p = g["옵치"]
                roles = [f"{OW_ROLE_EMOJI[r]}{p[f'{r}_mmr']}{'' if p['prefs'][r] == 'main' else '(부)'}"
                         for r in OW_ROLES if p["prefs"][r] != "no"]
                parts.append("옵치 " + " ".join(roles) + f" {p['wins']}승{p['losses']}패")
            if parts:
                lines.append(f"**{row.get('username') or row['discord_id']}** · " + " · ".join(parts))
        if not lines:
            await ctx.send("📂 등록된 내전 프로필이 없습니다.", ephemeral=True)
            return
        pages, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > 3900:
                pages.append(cur)
                cur = ""
            cur += ln + "\n"
        pages.append(cur)
        await ctx.send(embeds=[
            discord.Embed(title=f"📂 전체 내전 프로필 ({len(lines)}명)" if i == 0 else None, description=pg, color=discord.Color.purple())
            for i, pg in enumerate(pages[:10])
        ], ephemeral=True)


async def setup(bot):
    await bot.add_cog(Profile(bot))
