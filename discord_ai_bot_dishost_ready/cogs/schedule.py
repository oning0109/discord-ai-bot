import os
import re
import requests
from datetime import datetime
import discord
from discord.ext import commands

# Supabase 설정
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://tdatedpggtutuqrywguk.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_KEY 환경변수가 설정되지 않았습니다.")

# 📌 귤봇 포스트 채널 ID
FORUM_CHANNEL_ID = 1559903394137645137

# 🏷️ 상태 태그 ID
STATE_TAG_IDS = {
    "모집중": 1559905662320906345,
    "정원마감": 1559905662320906346,
    "모집마감": 1559905662320906347,
    "종료": 1559905662320906348,
}

# 🏷️ 게임 태그 ID
GAME_TAG_IDS = {
    "발로란트": 1559905959185350727,
    "오버워치": 1559915462404612228,
    "종합게임": 1559912489053978717,
    "술캠": 1559924340961710141,
    "공구": 1559926535748853932
}


class ScheduleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # -------------------------------------------------------------
    # 📌 포스트 업데이트 이벤트 (제목 변경 & 태그 변경 감지)
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        if after.parent_id != FORUM_CHANNEL_ID:
            return

        before_tag_ids = {t.id for t in before.applied_tags}
        after_tag_ids = {t.id for t in after.applied_tags}

        close_tag_id = STATE_TAG_IDS["모집마감"]
        end_tag_id = STATE_TAG_IDS["종료"]

        title_changed = (before.name != after.name)
        close_tag_added = (close_tag_id in after_tag_ids and close_tag_id not in before_tag_ids)
        is_currently_closed = (close_tag_id in after_tag_ids)

        # 1. '모집마감' 태그 상태에서 제목이 변경되었거나, '모집마감' 태그가 새로 추가된 경우
        if is_currently_closed and (title_changed or close_tag_added):
            print(f"🔍 [포스트 상태 감지] '{after.name}' (모집마감 상태 / DB 동기화 검수 시작)")
            await self.save_schedule_to_db(after)

        # 2. '종료' 태그가 새로 부착된 경우 -> DB 삭제
        elif end_tag_id in after_tag_ids and end_tag_id not in before_tag_ids:
            print(f"🏁 [포스트 상태 감지] '{after.name}' (종료 태그 감지 / DB 삭제 처리)")
            await self.delete_schedule_from_db(after)

    # -------------------------------------------------------------
    # 🔍 귤봇 포스트 제목 파싱 및 Supabase DB 저장
    # -------------------------------------------------------------
    async def save_schedule_to_db(self, thread: discord.Thread):
        try:
            raw_title = thread.name
            parts = [p.strip() for p in raw_title.split("|")]

            # 1. 제목 추출 및 접두사 정제
            if len(parts) >= 2:
                title = parts[1]
                title = re.sub(r"^(모집중\s*\)?|정원마감\s*\)?|모집마감\s*\)?)\s*", "", title).strip()
            else:
                title = raw_title

            # 2. 일시(월/일/시/분) 파싱
            now = datetime.now()
            year = now.year
            event_date_str = None

            match_full = re.search(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2}).*?(\d{1,2}):(\d{2})", raw_title)
            match_short = re.search(r"(\d{1,2})[\.-](\d{1,2}).*?(\d{1,2}):(\d{2})", raw_title)

            if match_full:
                y, month, day, hour, minute = map(int, match_full.groups())
                event_date_str = f"{y:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"
            elif match_short:
                month, day, hour, minute = map(int, match_short.groups())
                target_year = year if month >= now.month else year + 1
                event_date_str = f"{target_year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"

            # ⚠️ 날짜/시간 정보가 포함되지 않은 경우 스킵
            if not event_date_str:
                print(f"ℹ️ [DB 저장 스킵] '{title}' - 제목에 날짜/시간 정보가 포함되어 있지 않습니다.")
                return

            # 3. 게임 카테고리 추출
            reverse_game_tags = {v: k for k, v in GAME_TAG_IDS.items()}
            category = "기타"
            for tag in thread.applied_tags:
                if tag.id in reverse_game_tags:
                    category = reverse_game_tags[tag.id]
                    break

            # 4. 방장(모집자) 정보 추출
            creator_id = str(thread.owner_id) if thread.owner_id else "0"
            creator_name = "귤봇"
            creator_avatar = ""

            try:
                starter_msg = await thread.fetch_message(thread.id)
                if starter_msg.embeds:
                    embed = starter_msg.embeds[0]
                    for field in embed.fields:
                        if "함께하는 친구들" in field.name or "참가자" in field.name:
                            lines = field.value.split("\n")
                            if lines and "👑" in lines[0]:
                                creator_name = re.sub(r"<@!?\d+>|1\.\s*👑\s*", "", lines[0]).strip()
                if starter_msg.author and not starter_msg.author.bot:
                    creator_id = str(starter_msg.author.id)
                    creator_name = starter_msg.author.display_name
                    creator_avatar = starter_msg.author.display_avatar.url
            except Exception:
                pass

            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            }

            # 기존 동일 제목 삭제 후 새로 추가 (중복 방지 및 최신화)
            requests.delete(f"{SUPABASE_URL}/rest/v1/schedules?title=eq.{title}", headers=headers)

            payload = {
                "discord_thread_id": str(thread.id),
                "title": title,
                "event_date": event_date_str,
                "category": category,
                "created_by_discord_id": creator_id,
                "created_by_name": creator_name,
                "created_by_avatar": creator_avatar
            }
            res = requests.post(f"{SUPABASE_URL}/rest/v1/schedules", json=payload, headers=headers)

            if res.status_code in [200, 201]:
                formatted_time = event_date_str[:16].replace("T", " ")
                print(f"✅ [DB 저장 성공] 제목: '{title}' | 일시: {formatted_time} | 카테고리: {category}")
            else:
                print(f"❌ [DB 저장 실패]: HTTP {res.status_code} - {res.text}")

        except Exception as e:
            print(f"❌ [포스트 파싱 에러]: {e}")

    # -------------------------------------------------------------
    # 🗑️ Supabase DB 일정 삭제
    # -------------------------------------------------------------
    async def delete_schedule_from_db(self, thread: discord.Thread):
        try:
            raw_title = thread.name
            parts = [p.strip() for p in raw_title.split("|")]
            if len(parts) >= 2:
                title = re.sub(r"^(모집중\s*\)?|정원마감\s*\)?|모집마감\s*\)?)\s*", "", parts[1]).strip()
            else:
                title = raw_title

            headers = {
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            }
            thread_id = str(thread.id)
            res = requests.delete(f"{SUPABASE_URL}/rest/v1/schedules?discord_thread_id=eq.{thread_id}", headers=headers)
            print(f"🏁 [DB 삭제 완료] '{title}' 일정이 웹 달력 DB에서 정상 삭제되었습니다. (Status: {res.status_code})")

        except Exception as e:
            print(f"❌ [DB 삭제 에러]: {e}")


async def setup(bot):
    await bot.add_cog(ScheduleCog(bot))