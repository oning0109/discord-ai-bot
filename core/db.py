"""Supabase(PostgREST) 비동기 클라이언트 — 봇 전체가 이 객체 하나만 사용한다.

* aiohttp(디스코드 라이브러리에 포함)로 직접 호출하므로 디스코드 이벤트 루프를 막지 않는다.
* 승패 기록/되돌리기/채팅 집계/멤버 정리는 DB 함수(bot_*) 한 번 호출로 원자적으로 처리.
"""
import json
import logging
from typing import Any, Iterable

import aiohttp

log = logging.getLogger("rakko.db")


class DBError(Exception):
    pass


class DB:
    def __init__(self, url: str, key: str):
        self.rest = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    # ── 기본 HTTP ─────────────────────────────────────
    async def start(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _req(self, method: str, path: str, *, params=None, body=None, prefer: str | None = None) -> Any:
        await self.start()
        headers = {"Prefer": prefer} if prefer else None
        data = json.dumps(body, ensure_ascii=False) if body is not None else None
        async with self._session.request(method, f"{self.rest}/{path}", params=params, data=data, headers=headers) as r:
            text = await r.text()
            if r.status >= 400:
                raise DBError(f"{method} {path} → HTTP {r.status}: {text[:300]}")
            return json.loads(text) if text else None

    async def select(self, table: str, **params) -> list:
        return await self._req("GET", table, params=params) or []

    async def insert(self, table: str, row: dict | list, upsert_on: str | None = None) -> list:
        prefer = "return=representation"
        params = None
        if upsert_on:
            prefer += ",resolution=merge-duplicates"
            params = {"on_conflict": upsert_on}
        return await self._req("POST", table, params=params, body=row, prefer=prefer) or []

    async def update(self, table: str, values: dict, **filters) -> list:
        return await self._req("PATCH", table, params=filters, body=values, prefer="return=representation") or []

    async def delete(self, table: str, **filters) -> list:
        return await self._req("DELETE", table, params=filters, prefer="return=representation") or []

    async def rpc(self, fn: str, **args) -> Any:
        return await self._req("POST", f"rpc/{fn}", body=args)

    @staticmethod
    def in_(values: Iterable) -> str:
        return "in.(" + ",".join(str(v) for v in values) + ")"

    # ── 유저 프로필 ───────────────────────────────────
    async def get_profile(self, discord_id: int) -> dict | None:
        rows = await self.select("user_profiles", discord_id=f"eq.{discord_id}", limit=1)
        return rows[0] if rows else None

    async def get_profiles(self, discord_ids: Iterable[int]) -> dict[int, dict]:
        """여러 명을 한 번에 조회 → {discord_id(int): row}"""
        ids = list({int(i) for i in discord_ids})
        if not ids:
            return {}
        rows = await self.select("user_profiles", discord_id=self.in_(ids))
        return {int(r["discord_id"]): r for r in rows}

    async def all_profiles(self, columns: str = "*", only_members: bool = True) -> list:
        params = {"select": columns, "order": "username.asc"}
        if only_members:
            params["in_guild"] = "is.true"
        return await self.select("user_profiles", **params)

    async def update_profile(self, discord_id: int, values: dict) -> bool:
        rows = await self.update("user_profiles", {**values, "updated_at": "now()"}, discord_id=f"eq.{discord_id}")
        return bool(rows)

    async def ensure_profile(self, member) -> dict | None:
        """프로필이 없으면 만들고(채팅 0) 반환"""
        await self.upsert_members([member_row(member, 0)])
        return await self.get_profile(member.id)

    # ── 멤버 동기화 / 채팅 ────────────────────────────
    async def upsert_members(self, rows: list[dict]) -> int:
        """rows: member_row() 결과 목록. count 만큼 채팅 수 증가 (0이면 프로필 생성/갱신만)"""
        if not rows:
            return 0
        return await self.rpc("bot_track_chat_batch", p_rows=rows) or 0

    async def mark_left(self, discord_ids: Iterable[int]) -> int:
        ids = [str(i) for i in discord_ids]
        if not ids:
            return 0
        return await self.rpc("bot_mark_left", p_ids=ids) or 0

    async def purge_left(self, days: int) -> list:
        return await self.rpc("bot_purge_left", p_days=days) or []

    async def member_states(self) -> dict[int, bool]:
        rows = await self.select("user_profiles", select="discord_id,in_guild")
        out = {}
        for r in rows:
            try:
                out[int(r["discord_id"])] = bool(r.get("in_guild", True))
            except (TypeError, ValueError):
                pass
        return out

    # ── 생일 ──────────────────────────────────────────
    async def todays_birthdays(self, mmdd: str) -> list:
        return await self.select("user_profiles", select="discord_id,username", birthday=f"eq.{mmdd}", in_guild="is.true")

    # ── 경기 ──────────────────────────────────────────
    async def record_match(self, game: str, source: str, entries: list, *, winner=None, host_id=None,
                           recorded_by=None, step: int = 20, expected_a: float | None = None) -> dict:
        return await self.rpc(
            "bot_record_match",
            p_game=game, p_source=source, p_winner=winner,
            p_host=str(host_id) if host_id else None,
            p_by=str(recorded_by) if recorded_by else None,
            p_entries=entries, p_step=step, p_expected=expected_a,
        )

    async def undo_match(self, match_id: int, by: int) -> dict:
        return await self.rpc("bot_undo_match", p_id=match_id, p_by=str(by))

    async def get_match(self, match_id: int) -> dict | None:
        rows = await self.select("matches", id=f"eq.{match_id}", limit=1)
        return rows[0] if rows else None

    async def latest_match(self) -> dict | None:
        rows = await self.select("matches", undone="is.false", order="id.desc", limit=1)
        return rows[0] if rows else None

    async def recent_matches(self, game: str, limit: int = 10, with_expected: bool = False) -> list:
        params = {"select": "id,created_at,winner,entries,expected_a", "game": f"eq.{game}",
                  "undone": "is.false", "order": "id.desc", "limit": limit}
        if with_expected:
            params["expected_a"] = "not.is.null"
            params["winner"] = "not.is.null"
        return await self.select("matches", **params)

    async def user_matches(self, discord_id: int, game: str | None = None, limit: int = 10) -> list:
        params = {
            "select": "id,created_at,game,source,winner,entries",
            "undone": "is.false",
            "entries": "cs." + json.dumps([{"discord_id": str(discord_id)}]),
            "order": "id.desc",
            "limit": limit,
        }
        if game:
            params["game"] = f"eq.{game}"
        return await self.select("matches", **params)

    # ── 대기실 ────────────────────────────────────────
    async def load_lobbies(self) -> list:
        return await self.select("bot_lobbies")

    async def save_lobby(self, lobby: dict):
        row = {k: lobby[k] for k in ("message_id", "guild_id", "channel_id", "host_id", "game", "status", "participants", "teams")}
        row["updated_at"] = "now()"
        await self.insert("bot_lobbies", row, upsert_on="message_id")

    async def delete_lobby(self, message_id: int):
        await self.delete("bot_lobbies", message_id=f"eq.{message_id}")

    async def delete_old_lobbies(self, hours: int) -> int:
        from datetime import datetime, timedelta, timezone
        cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return len(await self.delete("bot_lobbies", updated_at=f"lt.{cut}"))

    # ── 일정 ──────────────────────────────────────────
    async def upsert_schedule(self, payload: dict) -> int:
        """스레드 ID 기준 1행 유지. 반환: 정리된 중복 행 수"""
        tid = payload["discord_thread_id"]
        rows = await self.select("schedules", select="id", discord_thread_id=f"eq.{tid}", order="id.asc")
        if rows:
            keep = rows[0]["id"]
            await self.update("schedules", payload, id=f"eq.{keep}")
            if len(rows) > 1:
                await self.delete("schedules", discord_thread_id=f"eq.{tid}", id=f"neq.{keep}")
            return len(rows) - 1
        await self.insert("schedules", payload)
        return 0

    async def delete_schedule(self, thread_id: int, titles: Iterable[str]) -> int:
        n = len(await self.delete("schedules", discord_thread_id=f"eq.{thread_id}"))
        for t in set(titles):  # 예전 방식(스레드 ID 없이 저장된 행)은 제목으로 보조 삭제
            n += len(await self.delete("schedules", discord_thread_id="is.null", title=f"eq.{t}"))
        return n


def team_of(member, role_team_map: dict) -> str | None:
    for role in getattr(member, "roles", []):
        if role.id in role_team_map:
            return role_team_map[role.id]
    return None


def member_row(member, count: int, role_team_map: dict | None = None) -> dict:
    if role_team_map is None:
        import config
        role_team_map = config.ROLE_TEAM_MAP
    return {
        "discord_id": str(member.id),
        "username": member.global_name or member.name,
        "avatar_url": str(member.display_avatar.url),
        "team_id": team_of(member, role_team_map),
        "count": count,
    }
