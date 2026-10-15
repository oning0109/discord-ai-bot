import asyncio
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

REST = f"{SUPABASE_URL}/rest/v1/schedules"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

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

# 포스트 제목 구분자: 실제 제목은 '┃'(U+2503)를 쓰므로 '|' 와 둘 다 지원
SEP_RE = re.compile(r"[|┃]")
PREFIX_RE = re.compile(r"^(모집중\s*\)?|정원마감\s*\)?|모집마감\s*\)?)\s*")


def split_title(raw_title: str):
    """(정제된 제목, 날짜를 찾을 문자열) 반환"""
    parts = [p.strip() for p in SEP_RE.split(raw_title)]
    if len(parts) >= 2:
        title = parts[1]
        # '모집중) 모집마감 …' 처럼 접두사가 겹쳐 있어도 전부 제거
        while True:
            stripped = PREFIX_RE.sub("", title).strip()
            if stripped == title:
                break
            title = stripped
        # 날짜는 마지막 구역("10.16(금) 23:00")에서만 찾아 제목 속 숫자와 헷갈리지 않게 함
        return title, parts[-1]
    return raw_title, raw_title


def parse_event_date(date_src: str):
    now = datetime.now()
    match_full = re.search(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2}).*?(\d{1,2}):(\d{2})", date_src)
    match_short = re.search(r"(\d{1,2})[\.-](\d{1,2}).*?(\d{1,2}):(\d{2})", date_src)

    if match_full:
        y, month, day, hour, minute = map(int, match_full.groups())
        return f"{y:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"
    if match_short:
        month, day, hour, minute = map(int, match_short.groups())
        target_year = now.year if month >= now.month else now.year + 1
        return f"{target_year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"
    return None


class ScheduleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # -------------------------------------------------------------
    # 📌 포스트 업데이트 이벤트 (제목 변경 & 태그 변경 감지)
    #   on_thread_update 는 봇 캐시에 없는 스레드(보관된 포스트, 재시작 후 등)에서는
    #   호출되지 않으므로 on_raw_thread_update 를 사용한다.
    #   ※ raw 이벤트 시점에는 payload.thread 가 아직 '변경 전' 상태라서
    #     제목/태그는 반드시 payload.data 에서 읽는다.
    # -------------------------------------------------------------
    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent):
        data = payload.data
        if payload.parent_id != FORUM_CHANNEL_ID:
            return

        name = data.get("name") or ""
        tag_ids = {int(t) for t in data.get("applied_tags", [])}

        is_ended = STATE_TAG_IDS["종료"] in tag_ids
        is_closed = STATE_TAG_IDS["모집마감"] in tag_ids

        # 1. '종료' 태그가 붙어 있으면 무조건 DB에서 삭제 (모집마감 태그가 같이 남아 있어도 종료가 우선)
        if is_ended:
            print(f"🏁 [포스트 상태 감지] '{name}' (종료 태그 / DB 삭제 처리)")
            await self.delete_schedule_from_db(payload.thread_id, name)
            return

        # 2. '모집마감' 상태면 제목/태그/기타 변경 때마다 DB를 최신 상태로 교체
        if is_closed:
            thread = payload.thread or self.bot.get_channel(payload.thread_id)
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(payload.thread_id)
                except Exception as e:
                    print(f"❌ [스레드 조회 실패] {payload.thread_id}: {e}")
                    return
            print(f"🔍 [포스트 상태 감지] '{name}' (모집마감 상태 / DB 동기화 시작)")
            await self.save_schedule_to_db(thread, name, tag_ids)

    # -------------------------------------------------------------
    # 🔍 귤봇 포스트 제목 파싱 및 Supabase DB 저장 (스레드 ID 기준으로 교체)
    # -------------------------------------------------------------
    async def save_schedule_to_db(self, thread: discord.Thread, raw_title: str, tag_ids: set):
        try:
            title, date_src = split_title(raw_title)
            event_date_str = parse_event_date(date_src)

            # ⚠️ 날짜/시간 정보가 포함되지 않은 경우 스킵
            if not event_date_str:
                print(f"ℹ️ [DB 저장 스킵] '{title}' - 제목에 날짜/시간 정보가 포함되어 있지 않습니다.")
                return

            # 게임 카테고리 추출
            reverse_game_tags = {v: k for k, v in GAME_TAG_IDS.items()}
            category = "기타"
            for tid in tag_ids:
                if tid in reverse_game_tags:
                    category = reverse_game_tags[tid]
                    break

            # 방장(모집자) 정보 추출
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

            thread_id = str(thread.id)
            payload = {
                "discord_thread_id": thread_id,
                "title": title,
                "event_date": event_date_str,
                "category": category,
                "created_by_discord_id": creator_id,
                "created_by_name": creator_name,
                "created_by_avatar": creator_avatar,
            }

            # 1) 새 행을 먼저 추가 (실패하면 기존 데이터는 그대로 보존)
            res = await asyncio.to_thread(
                requests.post, REST, json=payload, timeout=10,
                headers={**HEADERS, "Prefer": "return=representation"},
            )
            if res.status_code not in (200, 201):
                print(f"❌ [DB 저장 실패]: HTTP {res.status_code} - {res.text}")
                return
            new_id = res.json()[0]["id"]

            # 2) 같은 스레드의 이전 행(이름 변경 전 데이터, 중복 행)을 모두 삭제
            #    ※ schedules 에는 UPDATE 정책이 없어서 PATCH/upsert 대신 insert + delete 를 사용
            del_res = await asyncio.to_thread(
                requests.delete, REST, timeout=10,
                params={"discord_thread_id": f"eq.{thread_id}", "id": f"neq.{new_id}"},
                headers={**HEADERS, "Prefer": "return=representation"},
            )
            removed = len(del_res.json()) if del_res.status_code in (200, 204) and del_res.text else 0

            formatted_time = event_date_str[:16].replace("T", " ")
            print(f"✅ [DB 저장 성공] 제목: '{title}' | 일시: {formatted_time} | 카테고리: {category} | 이전 행 정리: {removed}건")

        except Exception as e:
            print(f"❌ [포스트 파싱 에러]: {e}")

    # -------------------------------------------------------------
    # 🗑️ Supabase DB 일정 삭제 (종료)
    # -------------------------------------------------------------
    async def delete_schedule_from_db(self, thread_id: int, raw_title: str):
        try:
            title, _ = split_title(raw_title)
            deleted = 0

            # 1) 스레드 ID로 삭제 (이름을 몇 번 바꿨어도 전부 같이 삭제됨)
            res = await asyncio.to_thread(
                requests.delete, REST, timeout=10,
                params={"discord_thread_id": f"eq.{thread_id}"},
                headers={**HEADERS, "Prefer": "return=representation"},
            )
            if res.status_code in (200, 204):
                deleted += len(res.json()) if res.text else 0
            else:
                print(f"❌ [DB 삭제 실패]: HTTP {res.status_code} - {res.text}")

            # 2) 예전 방식으로 저장돼 discord_thread_id 가 NULL 인 행은 제목으로 보조 삭제
            for t in {title, raw_title}:
                res2 = await asyncio.to_thread(
                    requests.delete, REST, timeout=10,
                    params={"discord_thread_id": "is.null", "title": f"eq.{t}"},
                    headers={**HEADERS, "Prefer": "return=representation"},
                )
                if res2.status_code in (200, 204) and res2.text:
                    deleted += len(res2.json())

            if deleted:
                print(f"🏁 [DB 삭제 완료] '{title}' 일정 {deleted}건이 웹 달력 DB에서 삭제되었습니다.")
            else:
                print(f"⚠️ [DB 삭제] '{title}' - 삭제할 행이 없습니다. (thread_id={thread_id})")

        except Exception as e:
            print(f"❌ [DB 삭제 에러]: {e}")


async def setup(bot):
    await bot.add_cog(ScheduleCog(bot))
