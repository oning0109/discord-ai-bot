# cogs/map_picker.py
import random
import discord
from discord.ext import commands

# --- 맵 데이터 베이스 ---
VALORANT_MAPS = [
    "어센트", "바인드", "헤이븐", "스플릿",
    "아이스박스", "브리즈", "프랙처",
    "펄", "로터스", "선셋", "어비스", 
    "코로드", "서밋"
]

OVERWATCH_MAPS = {
    "쟁탈": [
        "남극 반도", "네팔", "리장 타워", "부산", "사모아", "오아시스", "일리오스"
    ],
    "호위": [
        "66번 국도", "감시 기지: 지브롤터", "그림스뵈튼", "도라도", "리알토", "샴발리 수도원", "서킷 로얄", "쓰레기촌", "하바나"
    ],
    "혼합": [
        "네온 교차로", "눔바니", "미드타운", "블리자드 월드", "아이헨발데", "왕의 길", "파라이수", "할리우드"
    ],
    "밀기": [
        "뉴 퀸 스트리트", "이스페란사", "콜로세오", "루나사피"
    ],
    "플래시포인트": [
        "뉴 정크 시티", "수라바사", "아틀리스"
    ]
}


# --- 오버워치 전장 모드 선택 UI ---
class OverwatchModeView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ 명령어를 호출한 본인만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="🛡️ 오버워치 전장 모드를 선택하세요",
        options=[
            discord.SelectOption(label="쟁탈", value="쟁탈", description="쟁탈전 전장 중 랜덤 추첨", emoji="⚔️"),
            discord.SelectOption(label="호위", value="호위", description="호위전 중 랜덤 추첨", emoji="🚚"),
            discord.SelectOption(label="혼합", value="혼합", description="혼합전 전장 중 랜덤 추첨", emoji="🥊"),
            discord.SelectOption(label="밀기", value="밀기", description="밀기전 전장 중 랜덤 추첨", emoji="🤖"),
            discord.SelectOption(label="플래시포인트", value="플래시포인트", description="플래시포인트 전장 중 랜덤 추첨", emoji="🚩"),
            discord.SelectOption(label="전체 랜덤", value="랜덤", description="모든 오버워치 전장 중 상관없이 랜덤 추첨", emoji="🎲"),
        ]
    )
    async def select_mode(self, interaction: discord.Interaction, select: discord.ui.Select):
        mode = select.values[0]

        if mode == "랜덤":
            all_maps = []
            for map_list in OVERWATCH_MAPS.values():
                all_maps.extend(map_list)
            chosen_map = random.choice(all_maps)
            mode_title = "전체 랜덤 모드"
        else:
            chosen_map = random.choice(OVERWATCH_MAPS[mode])
            mode_title = f"{mode} 모드"

        embed = discord.Embed(
            title="🛡️ 오버워치 전장 추첨 완료!",
            description=f"**선택된 모드:** `{mode_title}`\n\n🎯 **추첨된 맵:** **{chosen_map}**",
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"추첨 요청자: {interaction.user.display_name}")

        # 기존 메시지를 결과 카드로 변경하고 버튼 제거
        await interaction.response.edit_message(content=None, embed=embed, view=None)


# --- 최초 게임 선택 UI (발로란트 vs 오버워치) ---
class MapPickerView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ 명령어를 호출한 본인만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="발로란트", style=discord.ButtonStyle.danger, emoji="🎯")
    async def valorant_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        chosen_map = random.choice(VALORANT_MAPS)

        embed = discord.Embed(
            title="🎯 발로란트 맵 추첨 완료!",
            description=f"🎯 **추첨된 맵:** **{chosen_map}**",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"추첨 요청자: {interaction.user.display_name}")

        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="오버워치", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def overwatch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ow_view = OverwatchModeView(self.author_id)
        await interaction.response.edit_message(
            content="🛡️ **[오버워치]** 원하시는 전장 모드를 선택해 주세요.",
            embed=None,
            view=ow_view
        )


# --- Cog 메인 클래스 ---
class MapPicker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="랜덤맵", description="발로란트 또는 오버워치 맵을 랜덤으로 추첨합니다.")
    async def pick_map(self, ctx: commands.Context):
        """!랜덤맵 또는 /랜덤맵 지원"""
        view = MapPickerView(ctx.author.id)
        await ctx.send("🎮 **랜덤 맵 추첨**: 게임을 선택해 주세요!", view=view)


async def setup(bot):
    await bot.add_cog(MapPicker(bot))