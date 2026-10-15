import os
import discord
from discord.ext import commands
from supabase import create_client

# Supabase 연동 설정
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://tdatedpggtutuqrywguk.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY 환경변수가 설정되지 않았습니다.")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 디스코드 역할 ID(숫자) ➔ Supabase team_id 매핑
ROLE_TEAM_MAP = {
    1556323926693642312: "chiikawa",
    1556324021979713536: "hachiware",
    1556324340784431217: "usagi",
    1556324066326216704: "kurimanju",
    1556324215563493437: "momonga",
    1556324296731660350: "shisa",
    1558124747487973436: "rakko"
}

class ChiikawaCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        # 봇 메시지 및 DM 무시
        if message.author.bot or not message.guild:
            return

        discord_id = str(message.author.id)
        author_name = message.author.global_name or message.author.name
        avatar_url = str(message.author.display_avatar.url)
        user_role_ids = [role.id for role in message.author.roles]

        # 1. 디스코드 역할 매칭 확인
        matched_team_id = None
        for role_id, team_id in ROLE_TEAM_MAP.items():
            if role_id in user_role_ids:
                matched_team_id = team_id
                break

        try:
            # 2. discord_id 기준 유저 조회 (웹 로그인 UUID 유저와 디스코드 봇 유저 공통 기준)
            res = supabase.table("user_profiles").select("*").eq("discord_id", discord_id).execute()

            if res.data:
                # [기존 유저 UPDATE] 프로필 정보 및 점수 반영
                target_user = res.data[0]
                target_id = target_user["user_id"]

                update_payload = {
                    "username": author_name,
                    "avatar_url": avatar_url,
                    "updated_at": "now()"
                }
                if matched_team_id:
                    update_payload["team_id"] = matched_team_id

                supabase.table("user_profiles").update(update_payload).eq("discord_id", discord_id).execute()
                supabase.rpc("increment_user_chat", {"p_user_id": target_id}).execute()

            else:
                # [신규 유저 INSERT] 미가입 유저 자동 생성
                new_user = {
                    "user_id": discord_id,
                    "discord_id": discord_id,
                    "username": author_name,
                    "team_id": matched_team_id,
                    "avatar_url": avatar_url,
                    "chat_count": 1,
                    "points": 1
                }
                supabase.table("user_profiles").insert(new_user).execute()

            # 3. 팀 점수 원자적 증가 (RPC)
            if matched_team_id:
                supabase.rpc("increment_team_chat", {"p_team_id": matched_team_id}).execute()

        except Exception as e:
            # 예외 발생 시에만 모니터링 로그 출력
            print(f"❌ [cogs.chiikawa 에러]: {e}", flush=True)

async def setup(bot):
    await bot.add_cog(ChiikawaCog(bot))