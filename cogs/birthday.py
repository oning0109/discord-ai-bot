import datetime
import discord
from discord.ext import commands, tasks

# 한국 표준시 (KST, UTC+9) 설정
KST = datetime.timezone(datetime.timedelta(hours=9))

# 🌟 생일 축하 메시지를 발송할 채널 ID (본인 서버 채널 ID로 변경 필수)
ANNOUNCEMENT_CHANNEL_ID = 1557866992512409650  


class Birthday(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db  # DBManager 인스턴스
        self.check_birthdays.start()  # 루프 시작

    def cog_unload(self):
        self.check_birthdays.cancel()

    # ⏰ 한국 시간(KST) 기준 매일 밤 12시(00:00)에 자동 실행
    @tasks.loop(time=datetime.time(hour=0, minute=0, second=0, tzinfo=KST))
    async def check_birthdays(self):
        await self.bot.wait_until_ready()

        # 오늘 날짜 (MM-DD)
        today_str = datetime.datetime.now(KST).strftime("%m-%d")
        birthday_users = self.db.get_todays_birthdays(today_str)

        if not birthday_users:
            return

        channel = self.bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if not channel:
            print("❌ 생일 축하 채널을 찾을 수 없습니다.")
            return

        for user_info in birthday_users:
            discord_id = user_info.get("discord_id")
            username = user_info.get("username", "제자")

            embed = discord.Embed(
                title="🎉 오늘의 생일 축하드립니다!",
                description=f"🎂 **<@{discord_id}>**님, 생일을 진심으로 축하합니다! 🥳\n스승 랏코와 제자들 모두가 행복하고 특별한 하루가 되기를 기원합니다!",
                color=0xFFD700,
            )
            embed.set_thumbnail(
                url="https://namu.wiki"
            )

            # 🛠️ @everyone 태그와 유저 멘션을 함께 보냅니다.
            await channel.send(content=f"@everyone 🎉 오늘의 주인공 <@{discord_id}>님을 축하해 주세요!", embed=embed)

    @commands.command(name="생일등록")
    async def register_birthday(self, ctx, date_str: str):
        """!생일등록 MM-DD (예: !생일등록 10-13)"""
        try:
            # MM-DD 유효성 검사
            datetime.datetime.strptime(date_str, "%m-%d")
        except ValueError:
            await ctx.send(
                "❌ 올바른 날짜 형식이 아닙니다. `MM-DD` 형태로 입력해 주세요. (예: `!생일등록 10-13`)"
            )
            return

        success = self.db.set_birthday(ctx.author.id, date_str)
        if success:
            await ctx.send(
                f"✅ {ctx.author.mention}님의 생일이 `{date_str}`로 등록되었습니다!"
            )
        else:
            await ctx.send("❌ 생일 등록 중 오류가 발생했습니다.")


async def setup(bot):
    await bot.add_cog(Birthday(bot))
