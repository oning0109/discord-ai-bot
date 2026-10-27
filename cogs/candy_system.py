import random
import discord
from discord.ext import commands

# 🍬 사탕 설정 및 가중치(확률 %) 설정
CANDY_TYPES = [
    {"name": "🍬 일반사탕", "chips": 1000, "weight": 40},
    {"name": "🍭 달콤한 사탕", "chips": 3000, "weight": 30},
    {"name": "🎃 황금호박", "chips": 10000, "weight": 15},
    {"name": "🦇 박쥐사탕", "chips": 15000, "weight": 10},
    {"name": "👻 저주받은 사탕 (대박)", "chips": 20000, "weight": 2.5},
    {"name": "💀 저주받은 사탕 (꽝)", "chips": 0, "weight": 2.5},
]

class CandyBoxButton(discord.ui.Button):
    def __init__(self, box_num: int, user_id: int):
        super().__init__(
            label=f"{box_num}번 상자", 
            style=discord.ButtonStyle.secondary, 
            emoji="🎁"
        )
        self.box_num = box_num
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 본인의 사탕 상자만 열 수 있어요!", ephemeral=True)
            return

        await interaction.response.defer()
        
        # 📦 무작위 뽑기
        weights = [c["weight"] for c in CANDY_TYPES]
        chosen_candy = random.choices(CANDY_TYPES, weights=weights, k=1)[0]
        
        candy_name = chosen_candy["name"]
        chips_to_give = chosen_candy["chips"]

        try:
            # 💡 기존 베팅 시스템 파일(예: betting_system.py)에서 rpc 함수를 가져옵니다.
            from betting_system import rpc
            
            # 1. 칩 지급 및 오늘 참여 기록을 Supabase RPC로 동시 처리
            # (이 RPC 함수 내부에서 유저에게 칩을 더하고 오늘 날짜의 참여 기록을 저장해야 합니다)
            await rpc("bet_add_candy_chips", {
                "p_discord_id": str(interaction.user.id),
                "p_amount": chips_to_give,
                "p_reason": f"사탕뽑기: {candy_name}"
            })
            
            if chips_to_give > 0:
                result_text = f"🎁 **{self.box_num}번 상자**를 열었습니다!\n\n결과: **{candy_name}** 획득! 🎉\n💰 **{chips_to_give:,} 칩**이 지급되었습니다!\n\n⚠️ 사탕뽑기는 **하루에 1번**만 참여할 수 있으며, 내일 다시 참여하실 수 있습니다."
            else:
                result_text = f"🎁 **{self.box_num}번 상자**를 열었습니다!\n\n결과: **{candy_name}**... 👻\n아쉽게도 칩을 얻지 못했습니다.\n\n⚠️ 사탕뽑기는 **하루에 1번**만 참여할 수 있으며, 내일 다시 참여하실 수 있습니다."
                
        except Exception as e:
            print(f"❌ [사탕지급 오류] {e}", flush=True)
            result_text = f"🎁 **{candy_name}**을(를) 뽑았으나, 데이터베이스 반영 중 오류가 발생했습니다. 관리자에게 문의해 주세요."

        # 버튼 비활성화 처리
        for child in self.view.children:
            child.disabled = True
            if isinstance(child, discord.ui.Button) and child.label == f"{self.box_num}번 상자":
                child.style = discord.ButtonStyle.success
                child.emoji = "🔓"

        await interaction.edit_original_response(content=result_text, view=self.view)


class CandyGameView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        for i in range(1, 6):
            self.add_item(CandyBoxButton(box_num=i, user_id=user_id))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ---------------------------------------------------------------
# 🧩 사탕뽑기 Cog 클래스 정의
# ---------------------------------------------------------------
class CandySystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.app_commands.command(name="사탕뽑기", description="하루에 한 번, 5개의 상자 중 하나를 골라 사탕을 뽑습니다.")
    async def candy_pick(self, interaction: discord.Interaction):
        try:
            from betting_system import rpc
            
            # 🔍 Supabase RPC를 호출하여 오늘 이미 뽑았는지 확인
            # (RPC 함수에서 오늘 기록이 있으면 {"can_join": false, "remaining_time": "05:23:10"} 등을 반환하도록 설계)
            cooldown_check = await rpc("bet_check_candy_cooldown", {
                "p_discord_id": str(interaction.user.id)
            })
            
            if cooldown_check and not cooldown_check.get("can_join", True):
                rem_time = cooldown_check.get("remaining_time", "내일")
                await interaction.response.send_message(
                    f"❌ 이미 오늘 사탕을 뽑으셨습니다!\n⏱️ 다음 참여 가능 시간까지: **{rem_time}**", 
                    ephemeral=True
                )
                return

        except Exception as e:
            print(f"❌ [쿨다운 체크 오류] {e}", flush=True)
            # DB 오류 시 안전하게 진행을 막거나 기본 허용 처리할 수 있습니다. 여기서는 경고 후 진행합니다.

        view = CandyGameView(interaction.user.id)
        await interaction.response.send_message("🎃 **사탕 뽑기 게임! (하루 1회 가능)**\n아래 5개의 상자 중 하나를 선택하세요!", view=view)


async def setup(bot):
    await bot.add_cog(CandySystem(bot))
