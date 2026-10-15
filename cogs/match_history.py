from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from balancer import OW_ROLE_LABEL
from db_manager import DBManager

KST = timezone(timedelta(hours=9))
GAMES = ("발로란트", "롤", "옵치")
GAME_ALIASES = {
    "발로": "발로란트", "발로란트": "발로란트", "valorant": "발로란트",
    "롤": "롤", "lol": "롤",
    "옵치": "옵치", "오버워치": "옵치", "overwatch": "옵치",
}


def fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(KST)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return "?"


def describe_entry(e: dict) -> str:
    name = e.get("name") or e.get("discord_id", "?")
    detail = []
    role = OW_ROLE_LABEL.get(e.get("role"))
    if role:
        detail.append(role)
    if e.get("mmr_delta"):
        detail.append(f"{e['mmr_delta']:+d}")
    if e.get("count", 1) > 1:
        detail.append(f"x{e['count']}")
    return f"{name}({' '.join(detail)})" if detail else name


def match_summary(match: dict) -> str:
    winner = {"A": "🔵 A팀 승리", "B": "🔴 B팀 승리"}.get(match.get("winner"), "수동 입력")
    head = f"**#{match['id']}** · {match['game']} · {fmt_time(match.get('created_at'))} · {winner}"
    entries = match.get("entries") or []
    wins = ", ".join(describe_entry(e) for e in entries if e.get("is_win")) or "-"
    losses = ", ".join(describe_entry(e) for e in entries if not e.get("is_win")) or "-"
    text = f"{head}\n🏆 승: {wins}\n💔 패: {losses}"
    return text[:1800]


class UndoConfirmView(discord.ui.View):
    def __init__(self, db: DBManager, match: dict, invoker_id: int):
        super().__init__(timeout=30)
        self.db = db
        self.match = match
        self.invoker_id = invoker_id
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
            except Exception:
                pass

    @discord.ui.button(label="↩️ 되돌리기 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer()
        result = self.db.undo_match(self.match["id"], interaction.user.id)
        mid = self.match["id"]
        if result.get("ok"):
            msg = f"↩️ **경기 #{mid}** 결과를 취소했어요. 승패 횟수와 MMR이 원래대로 돌아갔어요."
        elif result.get("error") == "already_undone":
            msg = f"ℹ️ 경기 #{mid}은(는) 이미 취소된 경기예요."
        elif result.get("failed"):
            msg = (
                f"⚠️ 경기 #{mid} 일부만 되돌렸어요. 실패: {', '.join(map(str, result['failed']))}\n"
                "같은 명령어를 다시 실행하면 실패한 사람만 다시 시도해요."
            )
        else:
            msg = "❌ 되돌리는 중 오류가 발생했어요. 콘솔 로그를 확인해 주세요."
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="되돌리기를 취소했어요.", view=None)


class MatchHistory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = DBManager()

    # ------------------------------------------------------------------
    # /전적 : 최근 경기 이력 + 연승/연패
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="전적", description="유저의 최근 경기 이력과 연승/연패를 보여줍니다.")
    @app_commands.describe(target="조회할 유저 (비우면 본인)", game="게임 (비우면 전체)")
    @app_commands.choices(game=[app_commands.Choice(name=g, value=g) for g in GAMES])
    async def recent_matches(self, ctx: commands.Context, target: discord.Member = None, game: str = None):
        await ctx.defer()
        user = target or ctx.author

        game_name = None
        if game:
            game_name = GAME_ALIASES.get(game.strip().lower())
            if not game_name:
                await ctx.send("❌ 게임은 `발로란트`/`롤`/`옵치` 중에서 골라주세요.")
                return

        matches = self.db.get_user_matches(user.id, game_name, limit=10)
        if matches is None:
            await ctx.send("❌ 전적을 불러오지 못했어요. `matches` 테이블이 만들어져 있는지 확인해 주세요.")
            return
        if not matches:
            await ctx.send(f"📭 {user.display_name}님의 기록된 경기가 아직 없어요. (이 기능을 켠 이후 경기부터 쌓여요)")
            return

        lines, results = [], []
        for m in matches:
            entry = next((e for e in (m.get("entries") or []) if e.get("discord_id") == str(user.id)), None)
            if not entry:
                continue
            count = int(entry.get("count", 1))
            is_win = bool(entry.get("is_win"))
            results.extend([is_win] * count)
            role = OW_ROLE_LABEL.get(entry.get("role"))
            tag = f" · {role}" if role else ""
            delta = f" ({entry['mmr_delta']:+d})" if entry.get("mmr_delta") else ""
            mark = "✅ 승" if is_win else "❌ 패"
            times = f" x{count}" if count > 1 else ""
            lines.append(f"`#{m['id']}` {fmt_time(m.get('created_at'))} · {m['game']}{tag} · {mark}{times}{delta}")

        wins = sum(results)
        losses = len(results) - wins
        streak = 0
        for r in results:  # results는 최신순
            if r == results[0]:
                streak += 1
            else:
                break
        streak_text = f"{streak}연승 🔥" if results[0] else f"{streak}연패 🥶"

        embed = discord.Embed(
            title=f"📜 {user.display_name}님의 최근 전적" + (f" · {game_name}" if game_name else ""),
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="최근 기록", value=f"{wins}승 {losses}패", inline=True)
        embed.add_field(name="현재", value=streak_text, inline=True)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text="되돌린 경기는 표시되지 않아요.")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # /되돌리기 : 경기 결과 취소 (소유자 또는 그 경기를 기록한 방장)
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="되돌리기", description="경기 결과를 취소하고 전적과 MMR을 원래대로 되돌립니다.")
    @app_commands.describe(match_id="경기 번호 (비우면 가장 최근 경기)")
    async def undo_match(self, ctx: commands.Context, match_id: int = None):
        await ctx.defer()
        match = self.db.get_match(match_id) if match_id else self.db.get_latest_match()
        if not match:
            await ctx.send("❌ 되돌릴 경기를 찾지 못했어요. (경기 번호를 확인하거나 `matches` 테이블을 확인해 주세요)")
            return
        if match.get("undone"):
            await ctx.send(f"ℹ️ 경기 #{match['id']}은(는) 이미 취소된 경기예요.")
            return

        is_owner = await self.bot.is_owner(ctx.author)
        allowed = {match.get("host_id"), match.get("recorded_by")}
        if not is_owner and str(ctx.author.id) not in allowed:
            await ctx.send("❌ 소유자나 이 경기를 기록한 방장만 되돌릴 수 있어요.")
            return

        view = UndoConfirmView(self.db, match, ctx.author.id)
        view.message = await ctx.send(
            "⚠️ **이 경기를 되돌릴까요?**\n" + match_summary(match) + "\n\n승패 횟수와 옵치 포지션 MMR이 원래대로 돌아가요.",
            view=view,
        )


async def setup(bot):
    await bot.add_cog(MatchHistory(bot))