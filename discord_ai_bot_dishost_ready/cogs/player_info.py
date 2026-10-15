import os
import io
import logging
import numpy as np
import discord
from discord.ext import commands
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
logging.getLogger('matplotlib').setLevel(logging.ERROR)

# 프로젝트 내 폰트 파일 경로 지정
current_dir = os.path.dirname(os.path.abspath(__file__))
font_path = os.path.join(current_dir, "fonts", "NEXONLv1GothicBold.ttf")

# 폰트 등록 및 설정
if os.path.exists(font_path):
    fm.fontManager.addfont(font_path)
    font_name = fm.FontProperties(fname=font_path).get_name()
    plt.rc("font", family=font_name)
else:
    # 예비 경로 (상대 경로로 로드 시도)
    fallback_path = "cogs/fonts/NEXONLv1GothicBold.ttf.ttf"
    if os.path.exists(fallback_path):
        fm.fontManager.addfont(fallback_path)
        font_name = fm.FontProperties(fname=fallback_path).get_name()
        plt.rc("font", family=font_name)

# 마이너스 기호 깨짐 방지
plt.rcParams["axes.unicode_minus"] = False

# 🆔 [1] 디스코드 유저 ID와 이름 매핑 (여기에 ID와 이름 연결)
USER_ID_MAP = {
    651350202405486602: "진영", 
    615432731937079306: "규영",
    461064799329976321: "동영",
    658181149973282837: "예은",
    543496611519660042: "정원",
    516569495196663809: "시온",
    741958155000348804: "현서",
    662358376650506257: "응채",
    820867389485875260: "소형",
    456068370639618054: "정흠",
    437190288537288705: "상진",
    416063306894278656: "희수",
    449924737561395201: "시성",
    417854928749461505: "예준",
    614674154301685829: "동현",
    1041011141171749005: "시우",
    318004143686156308: "수제비",
    811837908377927712: "까치",
    686092168804302848: "소은",
    900011851507695616: "현경",
    1107981991950950420: "연수",
    411774373091868672: "강우",
    367293959187464209: "종민",
    300543115515789314: "정우",
    517345173022310410: "예진",
    1559140514551435298: "민호",
    822825093733285889: "곰식"


}

# --- 데이터 정의 ---
TIER_DATA = {
    "0티어": ["민호"],
    "1티어": ["진영", "규영", "동영", "예은", "정원", "시온"],
    "2티어": ["현서", "응채", "소형", "정흠", "상진"],
    "3티어": ["희수", "시성", "예준", "예진"],
    "4티어": ["동현", "시우", "수제비", "까치", "동원", "정우"],
    "5티어": ["소은", "현경", "연수", "강우", "종민"],
}

STATS_DATA = {
    "진영": {"지능": 80, "센스": 70, "에임": 80, "멘탈": 50, "요원폭": 80, "숙련도": 90},
    "규영": {"지능": 70, "센스": 60, "에임": 80, "멘탈": 50, "요원폭": 50, "숙련도": 80},
    "예은": {"지능": 70, "센스": 70, "에임": 100, "멘탈": 10, "요원폭": 20, "숙련도": 80},
    "동영": {"지능": 80, "센스": 50, "에임": 90, "멘탈": 20, "요원폭": 40, "숙련도": 70},
    "시온": {"지능": 90, "센스": 80, "에임": 70, "멘탈": 50, "요원폭": 20, "숙련도": 80},
    "정원": {"지능": 50, "센스": 60, "에임": 90, "멘탈": 30, "요원폭": 80, "숙련도": 50},
    "응채": {"지능": 40, "센스": 60, "에임": 60, "멘탈": 50, "요원폭": 30, "숙련도": 50},
    "소형": {"지능": 50, "센스": 60, "에임": 70, "멘탈": 20, "요원폭": 50, "숙련도": 50},
    "현서": {"지능": 50, "센스": 60, "에임": 80, "멘탈": 50, "요원폭": 70, "숙련도": 60},
    "정흠": {"지능": 100, "센스": 50, "에임": 30, "멘탈": 50, "요원폭": 50, "숙련도": 80},
    "상진": {"지능": 10, "센스": 70, "에임": 50, "멘탈": 100, "요원폭": 20, "숙련도": 60},
    "민호": {"지능": 70, "센스": 100, "에임": 100, "멘탈": 80, "요원폭": 30, "숙련도": 80},
}

KEYWORD_DATA = {
    "오더형": ["정흠", "규영", "진영", "시온", "시우", "정우", "곰식"],
    "샷발형": ["동영", "응채", "소형", "예은", "예준", "시성", "민호"],
    "돌진형": ["상진", "예준", "종민", "희수", "정원", "연수", "소은", "현경", "까치", "수제비", "예진", "민호", "곰식"],
    "멘탈형": ["동영", "예은", "정원", "소형", "동원", "연수", "예진"],
    "주사위형": ["규영", "진영", "종민", "소형", "상진", "예준", "희수", "정원", "현경", "강우", "까치", "수제비", "예진", "현서", "라이", "정우"],
    "노기복형": ["시온", "정흠", "응채", "시우", "소은", "시성", "동현", "민호"],
    "몰입형": ["응채", "정원", "동원", "강우", "동현"],
}


def get_user_tier(name: str) -> str:
    for tier, members in TIER_DATA.items():
        if name in members:
            return tier
    return "미등록"


def get_user_keywords(name: str) -> list:
    keywords = []
    for kw, members in KEYWORD_DATA.items():
        if name in members:
            keywords.append(kw)
    return keywords


def generate_hex_chart(name: str, stats: dict) -> io.BytesIO:
    categories = ['지능', '센스', '에임', '멘탈', '요원폭', '숙련도']
    values = [stats.get(cat, 0) for cat in categories]
    num_vars = len(categories)

    angles = np.linspace(0, 2 * np.pi, num_vars, endpoint=False).tolist()
    values += values[:1]
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(5, 5), subplot_kw=dict(polar=True))
    fig.patch.set_facecolor('#2f3136')
    ax.set_facecolor('#2f3136')

    plt.xticks(angles[:-1], categories, color='white', size=11, fontweight='bold')
    ax.set_rlabel_position(0)
    plt.yticks([20, 40, 60, 80, 100], ["20", "40", "60", "80", "100"], color="#8e9297", size=8)
    plt.ylim(0, 100)

    ax.spines['polar'].set_color('#4f545c')
    ax.grid(color='#4f545c', linestyle='--', linewidth=0.8)

    ax.plot(angles, values, color='#5865F2', linewidth=2.5, linestyle='solid')
    ax.fill(angles, values, color='#5865F2', alpha=0.35)

    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', bbox_inches='tight', transparent=True, dpi=120)
    buffer.seek(0)
    plt.close(fig)
    return buffer


class PlayerInfo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="정보", description="특정 유저의 티어, 키워드 및 육각형 능력치를 조회합니다.")
    async def player_info_cmd(self, ctx: commands.Context, target: discord.Member = None):
        target_member = target or ctx.author
        
        # 1. 디스코드 ID 기반으로 매핑된 이름이 있는지 최우선 확인
        found_name = USER_ID_MAP.get(target_member.id)

        # 2. ID 매핑이 없다면 기존처럼 닉네임 텍스트 검색 (보완책)
        if not found_name:
            clean_name = target_member.display_name.strip()
            all_known_names = set(STATS_DATA.keys())
            for members in TIER_DATA.values():
                all_known_names.update(members)

            for known_name in all_known_names:
                if known_name in clean_name:
                    found_name = known_name
                    break
            
            if not found_name:
                found_name = clean_name

        tier = get_user_tier(found_name)
        keywords = get_user_keywords(found_name)
        keywords_str = ", ".join([f"`#{kw}`" for kw in keywords]) if keywords else "등록된 키워드 없음"

        if tier == "미등록":
            await ctx.send(f"❌ **{target_member.display_name}**님의 등록된 내전 정보가 없습니다.", ephemeral=True)
            return

        color_map = {
            "0티어": discord.Color.red(),
            "1티어": discord.Color.gold(),
            "2티어": discord.Color.purple(),
            "3티어": discord.Color.blue(),
            "4티어": discord.Color.green(),
            "5티어": discord.Color.light_grey()
        }
        embed_color = color_map.get(tier, discord.Color.default())

        embed = discord.Embed(
            title=f"🎮 {found_name}님의 내전 정보",
            color=embed_color
        )
        embed.set_thumbnail(url=target_member.display_avatar.url)
        embed.add_field(name="🏆 티어", value=f"**{tier}**", inline=True)
        embed.add_field(name="🏷️ 대표 키워드", value=keywords_str, inline=False)

        if tier in ["0티어", "1티어", "2티어"] and found_name in STATS_DATA:
            stats = STATS_DATA[found_name]
            chart_buffer = generate_hex_chart(found_name, stats)
            file = discord.File(fp=chart_buffer, filename="hex_chart.png")
            
            embed.set_image(url="attachment://hex_chart.png")
            await ctx.send(embed=embed, file=file)
        else:
            await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(PlayerInfo(bot))