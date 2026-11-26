"""서버 멤버 ↔ DB 동기화, 채팅 집계(사이트 점수), 생일 축하

* 입장: 프로필 자동 생성 (종족 역할 → team_id)
* 퇴장: 바로 지우지 않고 '퇴장' 표시 → LEAVE_PURGE_DAYS 일 뒤 자동 삭제 (그 안에 돌아오면 복구)
* 채팅: 메시지마다 DB를 부르지 않고 모아서 CHAT_FLUSH_SECONDS 마다 한 번에 반영
"""
import datetime
import logging

import discord
from discord.ext import commands, tasks

import config
from core.db import member_row, team_of

log = logging.getLogger("rakko.members")
KST = datetime.timezone(datetime.timedelta(hours=9))


class Members(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._chat: dict[int, dict] = {}   # discord_id → member_row (count 누적)
        self._synced_once = False

    async def cog_load(self):
        self.flush_chats.change_interval(seconds=config.CHAT_FLUSH_SECONDS)
        self.flush_chats.start()
        self.daily_purge.start()
        self.birthdays.start()

    async def cog_unload(self):
        self.flush_chats.cancel()
        self.daily_purge.cancel()
        self.birthdays.cancel()
        await self._flush()  # 종료 직전까지 쌓인 채팅 반영

    # ── 로그 채널 ─────────────────────────────────────
    async def notify(self, text: str):
        log.info(text)
        if config.LOG_CHANNEL_ID:
            ch = self.bot.get_channel(config.LOG_CHANNEL_ID)
            if ch:
                try:
                    await ch.send(text[:1900])
                except discord.HTTPException:
                    pass

    def _is_main(self, guild: discord.Guild | None) -> bool:
        main = self.bot.main_guild()
        return guild is not None and main is not None and guild.id == main.id

    # ── 채팅 집계 ─────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self._is_main(message.guild):
            return
        row = self._chat.get(message.author.id)
        if row:
            row["count"] += 1
            row["team_id"] = team_of(message.author, config.ROLE_TEAM_MAP) or row["team_id"]
        else:
            self._chat[message.author.id] = member_row(message.author, 1)

    async def _flush(self):
        if not self._chat:
            return
        batch, self._chat = list(self._chat.values()), {}
        try:
            await self.db.upsert_members(batch)
        except Exception as e:
            log.error("채팅 집계 반영 실패 (%d명, 다음 주기에 재시도): %s", len(batch), e)
            for r in batch:  # 실패분은 다시 쌓아 둔다
                cur = self._chat.get(int(r["discord_id"]))
                if cur:
                    cur["count"] += r["count"]
                else:
                    self._chat[int(r["discord_id"])] = r

    @tasks.loop(seconds=15)
    async def flush_chats(self):
        await self._flush()

    # ── 입장 / 퇴장 / 역할 변경 ───────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not self._is_main(member.guild):
            return
        try:
            await self.db.upsert_members([member_row(member, 0)])
            log.info("➕ 입장 → DB 추가/복구: %s", member)
        except Exception as e:
            log.error("입장 처리 실패 %s: %s", member, e)

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        user = payload.user
        if getattr(user, "bot", False) or not self._is_main(self.bot.get_guild(payload.guild_id)):
            return
        self._chat.pop(user.id, None)
        try:
            await self.db.mark_left([user.id])
            log.info("➖ 퇴장 → %d일 뒤 삭제 예정: %s", config.LEAVE_PURGE_DAYS, user)
        except Exception as e:
            log.error("퇴장 처리 실패 %s: %s", user, e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """종족 역할이 바뀌면 채팅을 기다리지 않고 바로 반영"""
        if after.bot or not self._is_main(after.guild):
            return
        t_before = team_of(before, config.ROLE_TEAM_MAP)
        t_after = team_of(after, config.ROLE_TEAM_MAP)
        if t_after and t_after != t_before:
            try:
                await self.db.upsert_members([member_row(after, 0)])
            except Exception as e:
                log.error("종족 변경 반영 실패 %s: %s", after, e)

    # ── 전체 동기화 ───────────────────────────────────
    async def sync_all(self, force: bool = True) -> dict | None:
        """force=False(봇 시작 시)면 퇴장 처리 인원이 비정상적으로 많을 때 건너뛴다 (멤버 목록 로딩 실패 대비)"""
        guild = self.bot.main_guild()
        if guild is None:
            return None
        if not guild.chunked:
            await guild.chunk()
        members = {m.id: m for m in guild.members if not m.bot}
        states = await self.db.member_states()

        to_upsert = [member_row(m, 0) for uid, m in members.items() if states.get(uid) is not True]
        to_leave = [uid for uid, active in states.items() if active and uid not in members]
        active = sum(states.values())
        skipped = 0
        if not force and len(to_leave) > max(10, active * 0.3):
            skipped, to_leave = len(to_leave), []

        for i in range(0, len(to_upsert), 100):
            await self.db.upsert_members(to_upsert[i:i + 100])
        left = await self.db.mark_left(to_leave)
        return {"guild": guild.name, "members": len(members), "added": len(to_upsert), "left": left, "skipped": skipped}

    @commands.Cog.listener()
    async def on_ready(self):
        if self._synced_once:
            return
        self._synced_once = True
        try:
            r = await self.sync_all(force=False)
            if r and r["skipped"]:
                await self.notify(
                    f"⚠️ 멤버 동기화: DB에는 있는데 서버에 없는 사람이 {r['skipped']}명이라 자동 퇴장 처리를 멈췄어요. "
                    "맞다면 `/멤버동기화`로 직접 실행해 주세요."
                )
            if r and (r["added"] or r["left"]):
                await self.notify(
                    f"🔄 멤버 동기화 ({r['guild']}): 추가·복구 {r['added']}명, "
                    f"퇴장 처리 {r['left']}명 ({config.LEAVE_PURGE_DAYS}일 뒤 삭제)"
                )
        except Exception as e:
            log.error("시작 동기화 실패: %s", e)

    @tasks.loop(time=datetime.time(hour=4, minute=0, tzinfo=KST))
    async def daily_purge(self):
        try:
            removed = await self.db.purge_left(config.LEAVE_PURGE_DAYS)
            if removed:
                names = ", ".join(r.get("username") or r["discord_id"] for r in removed)
                await self.notify(f"🗑️ 서버를 떠난 지 {config.LEAVE_PURGE_DAYS}일이 지난 {len(removed)}명을 DB에서 삭제했어요: {names}")
            await self.db.delete_old_lobbies(config.LOBBY_EXPIRE_HOURS)
        except Exception as e:
            log.error("퇴장자 정리 실패: %s", e)

    @daily_purge.before_loop
    async def _purge_wait(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="멤버동기화", description="[소유자] 서버 멤버와 DB를 지금 바로 맞춥니다.")
    @commands.is_owner()
    async def sync_cmd(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        r = await self.sync_all()
        if not r:
            await ctx.send("❌ 서버를 찾지 못했어요. `GUILD_ID`를 확인해 주세요.", ephemeral=True)
            return
        await ctx.send(
            f"✅ **{r['guild']}** 멤버 {r['members']}명 기준으로 맞췄어요.\n"
            f"• 추가·복구: {r['added']}명\n• 퇴장 처리: {r['left']}명 (돌아오지 않으면 {config.LEAVE_PURGE_DAYS}일 뒤 삭제)",
            ephemeral=True,
        )

    # ── 생일 ──────────────────────────────────────────
    @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=KST))
    async def birthdays(self):
        today = datetime.datetime.now(KST).strftime("%m-%d")
        try:
            users = await self.db.todays_birthdays(today)
        except Exception as e:
            log.error("생일 조회 실패: %s", e)
            return
        channel = self.bot.get_channel(config.BIRTHDAY_CHANNEL_ID)
        if not users or channel is None:
            return
        for u in users:
            did = u["discord_id"]
            embed = discord.Embed(
                title="🎉 오늘의 생일 축하드립니다!",
                description=(f"🎂 **<@{did}>**님, 생일을 진심으로 축하합니다! 🥳\n"
                             "스승 랏코와 제자들 모두가 행복하고 특별한 하루가 되기를 기원합니다!"),
                color=0xFFD700,
            )
            await channel.send(
                content=f"@everyone 🎉 오늘의 주인공 <@{did}>님을 축하해 주세요!",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=True, users=True),
            )

    @birthdays.before_loop
    async def _birthday_wait(self):
        await self.bot.wait_until_ready()

    @commands.command(name="생일등록")
    async def register_birthday(self, ctx: commands.Context, date_str: str):
        """!생일등록 MM-DD (예: !생일등록 10-13)"""
        try:
            datetime.datetime.strptime(f"2000-{date_str}", "%Y-%m-%d")  # 2/29 허용
        except ValueError:
            await ctx.send("❌ `MM-DD` 형태로 입력해 주세요. (예: `!생일등록 10-13`)")
            return
        try:
            await self.db.ensure_profile(ctx.author)
            await self.db.update_profile(ctx.author.id, {"birthday": date_str})
            await ctx.send(f"✅ {ctx.author.mention}님의 생일이 `{date_str}`로 등록되었습니다!")
        except Exception as e:
            log.error("생일 등록 실패: %s", e)
            await ctx.send("❌ 생일 등록 중 오류가 발생했습니다.")


async def setup(bot):
    await bot.add_cog(Members(bot))
