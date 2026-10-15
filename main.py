import asyncio
import discord
from discord.ext import commands
import config
from db_manager import DBManager  # 1. DBManager 클래스 임포트

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=config.DEFAULT_PREFIX, intents=intents)

# 2. bot 객체에 DBManager 인스턴스를 미리 연결 (cogs 로드 전 필수)
bot.db = DBManager()


@bot.event
async def on_ready():
    print("==========================================", flush=True)
    print(f"🤖 봇 성공적으로 로그인 완료: {bot.user.name}", flush=True)

    try:
        synced = await bot.tree.sync()
        print(f"🔄 슬래시 커맨드 {len(synced)}개 동기화 완료!", flush=True)
    except Exception as e:
        print(f"❌ 슬래시 커맨드 동기화 실패: {e}", flush=True)
    print("==========================================", flush=True)


async def main():
    async with bot:
        cogs = [
            "cogs.custom_game",
            "cogs.map_picker",
            "cogs.hero_picker",
            "cogs.player_info",
            "cogs.chat_tracker",
            "cogs.birthday",
            "cogs.schedule",
            "cogs.match_history",
        ]

        # 각 Cog 모듈 로드
        for cog in cogs:
            try:
                await bot.load_extension(cog)
                print(f"✅ [{cog}] 로드 성공!", flush=True)
            except Exception as e:
                print(f"❌ [{cog}] 로드 실패 원인: {e}", flush=True)

        await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())