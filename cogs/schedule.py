"""귤봇 포럼 포스트 → 사이트 일정 캘린더(schedules)

* '모집마감' 태그: 제목/태그가 바뀔 때마다 스레드 ID 기준으로 1행 유지 (중복 생기지 않음)
* '종료' 태그: DB에서 삭제
"""
import logging
import re
from datetime import datetime

import discord
from discord.ext import commands

import config

log = logging.getLogger("rakko.schedule")

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



class Schedule(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.game_by_tag = {v: k for k, v in config.GAME_TAG_IDS.items()}

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload: discord.RawThreadUpdateEvent):
        # raw 이벤트의 payload.thread 는 '변경 전' 상태라 제목/태그는 payload.data 에서 읽는다
        if payload.parent_id != config.SCHEDULE_FORUM_ID:
            return
        name = payload.data.get("name") or ""
        tags = {int(t) for t in payload.data.get("applied_tags", [])}
        try:
            if config.STATE_TAG_IDS["종료"] in tags:
                title, _ = split_title(name)
                n = await self.db.delete_schedule(payload.thread_id, {title, name})
                log.info("🏁 일정 삭제 '%s' (%d건)", title, n)
            elif config.STATE_TAG_IDS["모집마감"] in tags:
                thread = payload.thread or self.bot.get_channel(payload.thread_id) or await self.bot.fetch_channel(payload.thread_id)
                await self.save(thread, name, tags)
        except Exception as e:
            log.error("일정 동기화 실패 (%s): %s", name, e)

    async def save(self, thread: discord.Thread, raw_title: str, tags: set):
        title, date_src = split_title(raw_title)
        event_date = parse_event_date(date_src)
        if not event_date:
            log.info("ℹ️ 일정 저장 건너뜀 '%s' — 제목에 날짜/시간이 없음", title)
            return
        category = next((self.game_by_tag[t] for t in tags if t in self.game_by_tag), "기타")

        creator_id, creator_name, creator_avatar = str(thread.owner_id or 0), "귤봇", ""
        try:
            starter = await thread.fetch_message(thread.id)
            if starter.author and not starter.author.bot:
                creator_id = str(starter.author.id)
                creator_name = starter.author.display_name
                creator_avatar = starter.author.display_avatar.url
            elif starter.embeds:
                for f in starter.embeds[0].fields:
                    if "함께하는 친구들" in f.name or "참가자" in f.name:
                        first = f.value.split("\n")[0]
                        if "👑" in first:
                            creator_name = re.sub(r"<@!?\d+>|1\.\s*👑\s*", "", first).strip() or creator_name
        except discord.HTTPException:
            pass

        removed = await self.db.upsert_schedule({
            "discord_thread_id": str(thread.id), "title": title, "event_date": event_date, "category": category,
            "created_by_discord_id": creator_id, "created_by_name": creator_name, "created_by_avatar": creator_avatar,
        })
        log.info("✅ 일정 저장 '%s' %s [%s] (중복 정리 %d건)", title, event_date[:16].replace("T", " "), category, removed)


async def setup(bot):
    await bot.add_cog(Schedule(bot))
