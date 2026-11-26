"""/전적 (최근 경기·연승) · /되돌리기 (경기 결과 취소)"""
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.ranks import LOBBY_GAMES, OW_ROLE_NAME, normalize_game

OW_ROLE_LABEL = OW_ROLE_NAME

KST = timezone(timedelta(hours=9))


def fmt_time(iso) -> str:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(KST).strftime("%m/%d %H:%M")
    except Exception:
        return "?"


def describe_entry(e: dict) -> str:
    detail = []
    if OW_ROLE_LABEL.get(e.get("role")):
        detail.append(OW_ROLE_LABEL[e["role"]])
    if e.get("mmr_delta"):
        detail.append(f"{e['mmr_delta']:+d}")
    if (e.get("count") or 1) > 1:
        detail.append(f"x{e['count']}")
    name = e.get("name") or e.get("discord_id", "?")
    return f"{name}({' '.join(detail)})" if detail else name


def match_summary(m: dict) -> str:
    winner = {"A": "🔵 A팀 승리", "B": "🔴 B팀 승리"}.get(m.get("winner"), "수동 입력")
    entries = m.get("entries") or []
    wins = ", ".join(describe_entry(e) for e in entries if e.get("is_win")) or "-"
    losses = ", ".join(describe_entry(e) for e in entries if not e.get("is_win")) or "-"
    return f"**#{m['id']}** · {m['game']} · {fmt_time(m.get('created_at'))} · {winner}\n🏆 승: {wins}\n💔 패: {losses}"[:1800]


class UndoConfirmView(discord.ui.View):
    def __init__(self, db, match: dict, invoker_id: int):
        super().__init__(timeout=30)
        self.db, self.match, self.invoker_id = db, match, invoker_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ 명령어를 입력한 사람만 누를 수 있어요.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="⌛ 시간이 지나 되돌리기가 취소되었어요.", view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="↩️ 되돌리기 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer()
        mid = self.match["id"]
        try:
            res = await self.db.undo_match(mid, interaction.user.id)
        except Exception:
            await interaction.edit_original_response(content="❌ 되돌리는 중 오류가 발생했어요. 콘솔 로그를 확인해 주세요.", view=None)
            return
        if res.get("ok"):
            msg = f"↩️ **경기 #{mid}** 결과를 취소했어요. 승패와 MMR이 원래대로 돌아갔어요."
            if res.get("skipped"):
                msg += f"\nℹ️ 서버를 떠나 DB에 없는 인원은 건너뛰었어요: {', '.join(res['skipped'])}"
        elif res.get("error") == "already_undone":
            msg = f"ℹ️ 경기 #{mid}은(는) 이미 취소된 경기예요."
        else:
            msg = f"❌ 경기 #{mid}을(를) 찾지 못했어요."
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="되돌리기를 취소했어요.", view=None)


class History(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    @commands.hybrid_command(name="전적", description="유저의 최근 경기 이력과 연승/연패를 보여줍니다.")
    @app_commands.describe(target="조회할 유저 (비우면 본인)", game="게임 (비우면 전체)")
    @app_commands.choices(game=[app_commands.Choice(name=g, value=g) for g in LOBBY_GAMES])
    async def recent(self, ctx: commands.Context, target: discord.Member = None, game: str = None):
        await ctx.defer()
        user = target or ctx.author
        game_name = normalize_game(game) if game else None
        if game and not game_name:
            await ctx.send("❌ 게임은 `발로란트`/`롤`/`옵치` 중에서 골라주세요.")
            return
        matches = await self.db.user_matches(user.id, game_name, limit=10)
        if not matches:
            await ctx.send(f"📭 {user.display_name}님의 기록된 경기가 아직 없어요.")
            return

        lines, results = [], []
        for m in matches:
            e = next((x for x in (m.get("entries") or []) if x.get("discord_id") == str(user.id)), None)
            if not e:
                continue
            cnt, win = int(e.get("count") or 1), bool(e.get("is_win"))
            results.extend([win] * cnt)
            role = OW_ROLE_LABEL.get(e.get("role"))
            delta = f" ({e['mmr_delta']:+d})" if e.get("mmr_delta") else ""
            lines.append(
                f"`#{m['id']}` {fmt_time(m.get('created_at'))} · {m['game']}{f' · {role}' if role else ''} · "
                f"{'✅ 승' if win else '❌ 패'}{f' x{cnt}' if cnt > 1 else ''}{delta}"
            )
        if not results:
            await ctx.send(f"📭 {user.display_name}님의 기록된 경기가 아직 없어요.")
            return
        wins = sum(results)
        streak = next((i for i, r in enumerate(results) if r != results[0]), len(results))
        embed = discord.Embed(
            title=f"📜 {user.display_name}님의 최근 전적" + (f" · {game_name}" if game_name else ""),
            description="\n".join(lines), color=discord.Color.blurple(),
        )
        embed.add_field(name="최근 기록", value=f"{wins}승 {len(results) - wins}패", inline=True)
        embed.add_field(name="현재", value=f"{streak}연승 🔥" if results[0] else f"{streak}연패 🥶", inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="되돌린 경기는 표시되지 않아요.")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="되돌리기", description="경기 결과를 취소하고 전적과 MMR을 원래대로 되돌립니다.")
    @app_commands.describe(match_id="경기 번호 (비우면 가장 최근 경기)")
    async def undo(self, ctx: commands.Context, match_id: int = None):
        await ctx.defer()
        match = await (self.db.get_match(match_id) if match_id else self.db.latest_match())
        if not match:
            await ctx.send("❌ 되돌릴 경기를 찾지 못했어요.")
            return
        if match.get("undone"):
            await ctx.send(f"ℹ️ 경기 #{match['id']}은(는) 이미 취소된 경기예요.")
            return
        if not await self.bot.is_owner(ctx.author) and str(ctx.author.id) not in {match.get("host_id"), match.get("recorded_by")}:
            await ctx.send("❌ 소유자나 이 경기를 기록한 방장만 되돌릴 수 있어요.")
            return
        view = UndoConfirmView(self.db, match, ctx.author.id)
        view.message = await ctx.send(
            "⚠️ **이 경기를 되돌릴까요?**\n" + match_summary(match) + "\n\n승패 횟수와 옵치 포지션 MMR이 원래대로 돌아가요.", view=view
        )


def _balance_report(matches: list) -> str:
    """경기 전 예상 승률과 실제 결과 비교"""
    rows = [(float(m["expected_a"]), m["winner"] == "A") for m in matches]
    n = len(rows)
    fav_hits = sum(1 for e, a_won in rows if (e >= 0.5) == a_won)
    brier = sum((e - (1.0 if a_won else 0.0)) ** 2 for e, a_won in rows) / n
    buckets = [("50~55%", 0.50, 0.55), ("55~60%", 0.55, 0.60), ("60% 이상", 0.60, 1.01)]
    lines = [f"최근 **{n}판** 기준", f"• 봇이 우세하다고 본 팀의 실제 승률: **{fav_hits / n * 100:.0f}%** ({fav_hits}/{n})",
             f"• 예측 오차(브라이어 점수): **{brier:.3f}** (0.25 = 동전 던지기, 낮을수록 정확)", "", "**우세 정도별**"]
    for name, lo, hi in buckets:
        sel = [(max(e, 1 - e), (e >= 0.5) == a_won) for e, a_won in rows if lo <= max(e, 1 - e) < hi]
        if sel:
            exp = sum(x for x, _ in sel) / len(sel) * 100
            act = sum(1 for _, hit in sel if hit) / len(sel) * 100
            lines.append(f"• {name}: {len(sel)}판 · 예상 {exp:.0f}% → 실제 {act:.0f}%")
    lines += ["", "📌 균형 잡힌 팀이면 우세 팀 실제 승률이 **50~55%** 근처예요. 예상보다 실제가 크게 높으면 점수 차가 실제보다 작게 잡힌 거예요."]
    return "\n".join(lines)


class BalanceStats(commands.Cog):
    def __init__(self, bot):
        self.db = bot.db

    @commands.hybrid_command(name="밸런스통계", description="봇이 예상한 승률과 실제 결과를 비교해 팀 밸런스가 잘 맞는지 봅니다.")
    @app_commands.describe(game="게임 (기본 오버워치)", count="최근 몇 판 (기본 30)")
    @app_commands.choices(game=[app_commands.Choice(name="오버워치", value="옵치"), app_commands.Choice(name="발로란트", value="발로란트")])
    async def balance_stats(self, ctx: commands.Context, game: str = "옵치", count: int = 30):
        await ctx.defer()
        game = normalize_game(game) or "옵치"
        matches = await self.db.recent_matches(game, limit=max(5, min(count, 200)), with_expected=True)
        if len(matches) < 3:
            await ctx.send(f"📭 예상 승률이 기록된 {game} 경기가 아직 {len(matches)}판뿐이에요. 몇 판 더 하고 확인해 주세요!")
            return
        await ctx.send(embed=discord.Embed(title=f"⚖️ {game} 밸런스 통계", description=_balance_report(matches),
                                           color=discord.Color.teal()))


async def setup(bot):
    await bot.add_cog(BalanceStats(bot))
    await bot.add_cog(History(bot))
