"""오버워치 인게임 경쟁전 랭크 조회 (OverFast API — 블리자드 공개 프로필을 읽어오는 무료 오픈소스 API)

* 블리자드 공식 전적 API는 없어서, 공개 커리어 프로필 기반인 OverFast를 쓴다.
* 게임 안에서 프로필이 '공개'여야 랭크가 보인다.
* 공용 서버는 초당 요청 제한이 있으니 일괄 갱신 때는 천천히 호출한다.
"""
import re
from urllib.parse import quote

import aiohttp

from core.ranks import OVERFAST_ROLE_MAP

BATTLETAG_RE = re.compile(r"^(.{2,12})[#\-](\d{3,6})$")


class OWError(Exception):
    """사용자에게 그대로 보여줄 수 있는 메시지를 담는다."""


def normalize_battletag(text: str) -> str | None:
    """'닉네임#1234' / '닉네임-1234' → '닉네임#1234' (형식이 틀리면 None)"""
    m = BATTLETAG_RE.match(str(text or "").strip())
    return f"{m.group(1)}#{m.group(2)}" if m else None


def parse_summary(data: dict) -> dict:
    """OverFast /summary 응답 → {"platform","season","tank","deal","heal","open"} (배치 안 된 역할은 None)"""
    comp = (data or {}).get("competitive") or {}
    platform = "pc" if comp.get("pc") else ("console" if comp.get("console") else None)
    out = {"platform": platform, "season": None, "tank": None, "deal": None, "heal": None, "open": None}
    if not platform:
        return out
    block = comp[platform] or {}
    out["season"] = block.get("season")
    for src, dst in list(OVERFAST_ROLE_MAP.items()) + [("open", "open")]:
        r = block.get(src)
        if r and r.get("division"):
            out[dst] = {"division": str(r["division"]).lower(), "tier": int(r.get("tier") or 5)}
    return out


class OverFast:
    def __init__(self, base_url: str):
        self.base = base_url
        self._session: aiohttp.ClientSession | None = None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_ranks(self, battletag: str) -> dict:
        tag = normalize_battletag(battletag)
        if not tag:
            raise OWError("배틀태그 형식이 올바르지 않아요. 예) `닉네임#1234`")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

        url = f"{self.base}/players/{quote(tag.replace('#', '-'))}/summary"
        try:
            async with self._session.get(url) as r:
                if r.status == 404:
                    raise OWError(f"`{tag}` 플레이어를 찾지 못했어요. 배틀태그(대소문자 포함)를 확인해 주세요.")
                if r.status == 429:
                    raise OWError("오버워치 조회 서버가 바빠요. 잠시 후 다시 시도해 주세요.")
                if r.status >= 400:
                    raise OWError(f"오버워치 조회 실패 (HTTP {r.status}). 잠시 후 다시 시도해 주세요.")
                data = await r.json()
        except aiohttp.ClientError as e:
            raise OWError(f"오버워치 조회 서버에 연결하지 못했어요: {e}") from e

        ranks = parse_summary(data)
        if data.get("privacy") == "private" or not ranks["platform"]:
            ranks["private"] = data.get("privacy") == "private"
        return ranks
