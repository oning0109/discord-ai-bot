# cogs/hero_picker.py
import random
import discord
from discord.ext import commands

# --- 게임별 역할군 및 영웅/요원 데이터 ---
VALORANT_HEROES = {
    "타격대": ["제트", "레이즈", "피닉스", "레이나", "요루", "네온", "아이소", "웨이레이"],
    "척후대": ["소바", "브리치", "스카이", "케이/오", "페이드", "게코", "테호"],
    "감시자": ["세이지", "사이퍼", "킬조이", "체임버", "데드록", "바이스", "비토"],
    "전략가": ["브림스톤", "바이퍼", "오멘", "아스트라", "하버", "클로브", "믹스"]
}

OVERWATCH_HEROES = {
    "탱커": [
        "디몬(D.Mon)", "디바(D.Va)", "도미나", "둠피스트", "라마트라", "라인하르트", "레킹볼",
        "로드호그", "마우가", "시그마", "오리사", "윈스턴", "자리야", "정커퀸", "해저드"
    ],
    "딜러": [
        "겐지", "리퍼", "메이", "바스티온", "벤데타", "벤처", "소전", "솔져: 76", "시메트라",
        "시에라", "시온", "안란", "애쉬", "에코", "엠레", "위도우메이커", "정크렛", "캐서디"
        "토르비욘", "트레이서", "파라", "프레야", "한조"
    ],
    "힐러": [
        "아나", "바티스트", "브리기테", "일리아리", "키리코", "독트린", "우양", "제트팩 캣",
        "라이프위버", "루시우", "메르시", "모이라", "젠야타", "주노", "솜브라", "미즈키"
    ]
}


# --- 발로란트 역할군 선택 UI --- 
class ValorantRoleView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ 명령어를 호출한 본인만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="🎯 발로란트 역할군을 선택하세요",
        options=[
            discord.SelectOption(label="타격대", value="타격대", description="제트, 레이즈, 피닉스, 레이나, 요루, 네온, 아이소", emoji="⚔️"),
            discord.SelectOption(label="척후대", value="척후대", description="소바, 브리치, 스카이, 케이/오, 페이드, 게코", emoji="🏹"),
            discord.SelectOption(label="감시자", value="감시자", description="세이지, 사이퍼, 킬조이, 체임버, 데드록", emoji="🛡️"),
            discord.SelectOption(label="전략가", value="전략가", description="브림스톤, 바이퍼, 오멘, 아스트라, 하버, 클로브", emoji="☁️"),
            discord.SelectOption(label="전체 상관없이 랜덤", value="랜덤", description="모든 발로란트 요원 중 랜덤 추천", emoji="🎲"),
        ]
    )
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.Select):
        role = select.values[0]

        if role == "랜덤":
            all_heroes = []
            for h_list in VALORANT_HEROES.values():
                all_heroes.extend(h_list)
            chosen_hero = random.choice(all_heroes)
            # 선택된 요원의 실제 역할군 찾기
            actual_role = next(r for r, h_list in VALORANT_HEROES.items() if chosen_hero in h_list)
            role_display = f"전체 랜덤 (추첨된 역할군: {actual_role})"
        else:
            chosen_hero = random.choice(VALORANT_HEROES[role])
            role_display = role

        embed = discord.Embed(
            title="🎯 발로란트 요원 추천 완료!",
            description=f"**선택 역할군:** `{role_display}`\n\n✨ **추천 요원:** **{chosen_hero}**",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"추첨 요청자: {interaction.user.display_name}")

        await interaction.response.edit_message(content=None, embed=embed, view=None)


# --- 오버워치 역할군 선택 UI ---
class OverwatchRoleView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ 명령어를 호출한 본인만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="🛡️ 오버워치 역할군을 선택하세요",
        options=[
            discord.SelectOption(label="탱커", value="탱커", description="돌격 영웅 중 랜덤 추천", emoji="🛡️"),
            discord.SelectOption(label="딜러", value="딜러", description="공격 영웅 중 랜덤 추천", emoji="⚔️"),
            discord.SelectOption(label="힐러", value="힐러", description="지원 영웅 중 랜덤 추천", emoji="💉"),
            discord.SelectOption(label="전체 상관없이 랜덤", value="랜덤", description="모든 오버워치 영웅 중 랜덤 추천", emoji="🎲"),
        ]
    )
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.Select):
        role = select.values[0]

        if role == "랜덤":
            all_heroes = []
            for h_list in OVERWATCH_HEROES.values():
                all_heroes.extend(h_list)
            chosen_hero = random.choice(all_heroes)
            actual_role = next(r for r, h_list in OVERWATCH_HEROES.items() if chosen_hero in h_list)
            role_display = f"전체 랜덤 (추첨된 역할군: {actual_role})"
        else:
            chosen_hero = random.choice(OVERWATCH_HEROES[role])
            role_display = role

        embed = discord.Embed(
            title="🛡️ 오버워치 영웅 추천 완료!",
            description=f"**선택 역할군:** `{role_display}`\n\n✨ **추천 영웅:** **{chosen_hero}**",
            color=discord.Color.orange()
        )
        embed.set_footer(text=f"추첨 요청자: {interaction.user.display_name}")

        await interaction.response.edit_message(content=None, embed=embed, view=None)


# --- 최초 게임 선택 UI (발로란트 vs 오버워치) ---
class HeroPickerView(discord.ui.View):
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
        val_view = ValorantRoleView(self.author_id)
        await interaction.response.edit_message(
            content="🎯 **[발로란트]** 원하시는 역할군을 선택해 주세요.",
            embed=None,
            view=val_view
        )

    @discord.ui.button(label="오버워치", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def overwatch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ow_view = OverwatchRoleView(self.author_id)
        await interaction.response.edit_message(
            content="🛡️ **[오버워치]** 원하시는 역할군을 선택해 주세요.",
            embed=None,
            view=ow_view
        )


# --- Cog 메인 클래스 ---
class HeroPicker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="랜덤캐릭터", description="발로란트 또는 오버워치의 역할군별 랜덤 영웅/요원을 추천합니다.")
    async def pick_hero(self, ctx: commands.Context):
        """!랜덤캐릭터 또는 /랜덤캐릭터 지원"""
        view = HeroPickerView(ctx.author.id)
        await ctx.send("🎮 **랜덤 캐릭터 추천**: 게임을 선택해 주세요!", view=view)


async def setup(bot):
    await bot.add_cog(HeroPicker(bot))