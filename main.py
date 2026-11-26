import asyncio
import logging

import discord
from discord.ext import commands

import config
from core.db import DB
from core.overfast import OverFast

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rakko")

COGS = (
    "cogs.members",   # 입·퇴장 동기화 + 채팅 집계 + 생일
    "cogs.profile",   # 등록/수정/조회/옵치연동/소유자 명령어
    "cogs.lobby",     # 내전 모집·팀 편성·결과
    "cogs.history",   # 전적/되돌리기
    "cogs.map_picker",
    "cogs.hero_picker",
)


class RakkoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # 입·퇴장 감지 (개발자 포털에서 SERVER MEMBERS INTENT 켜야 함)
        intents.message_content = True  # ! 접두사 명령어
        super().__init__(command_prefix=config.PREFIX, intents=intents, owner_ids=config.OWNER_IDS)
        self.db = DB(config.SUPABASE_URL, config.SUPABASE_KEY)
        self.ow = OverFast(config.OVERFAST_URL)

    async def setup_hook(self):
        await self.db.start()
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("✅ %s 로드", cog)
            except Exception:
                log.exception("❌ %s 로드 실패", cog)
        try:
            synced = await self.tree.sync()
            log.info("🔄 슬래시 커맨드 %d개 동기화", len(synced))
        except Exception:
            log.exception("슬래시 커맨드 동기화 실패")

    async def close(self):
        await super().close()  # 코그 언로드(채팅 집계 마지막 반영) 후 연결 정리
        await self.db.close()
        await self.ow.close()

    def main_guild(self) -> discord.Guild | None:
        if config.GUILD_ID:
            return self.get_guild(config.GUILD_ID)
        return self.guilds[0] if self.guilds else None

    async def on_ready(self):
        log.info("🤖 로그인: %s (서버 %d개)", self.user, len(self.guilds))

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if ctx.command and ctx.command.has_error_handler():
            return
        if isinstance(error, commands.NotOwner):
            await ctx.send("❌ 소유자만 사용할 수 있는 명령어입니다.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ 서버 채널에서만 쓸 수 있어요.", ephemeral=True)
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send(f"❌ 입력이 올바르지 않아요. `/{ctx.command}` 설명을 확인해 주세요.", ephemeral=True)
        elif not isinstance(error, commands.CommandNotFound):
            log.error("명령어 오류 (%s): %s", ctx.command, error, exc_info=error)
            try:
                await ctx.send("❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.", ephemeral=True)
            except discord.HTTPException:
                pass


async def main():
    bot = RakkoBot()
    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
