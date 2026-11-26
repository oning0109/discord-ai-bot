"""삭발봇 대화 (OpenAI API) — /물어봐 또는 @삭발봇 멘션

* 인격(말투·규칙)은 prompts/persona.txt 에서 수정 (코드 수정 불필요, 봇 재시작 시 반영)
* 날씨는 Open-Meteo(무료, 키 불필요)에서 실제 값을 받아와서 답한다
* 비용 보호: 1인 하루 질문 수 제한 + 짧은 답변 + 연속 질문 쿨타임
"""
import datetime
import json
import logging
import os
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("rakko.ask")
KST = datetime.timezone(datetime.timedelta(hours=9))
PERSONA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "persona.txt")

# 주요 도시는 좌표를 바로 쓰고, 나머지는 Open-Meteo 지명 검색
CITY_COORDS = {
    "서울": (37.5665, 126.9780), "부산": (35.1796, 129.0756), "대구": (35.8714, 128.6014),
    "인천": (37.4563, 126.7052), "광주": (35.1595, 126.8526), "대전": (36.3504, 127.3845),
    "울산": (35.5384, 129.3114), "세종": (36.4800, 127.2890), "제주": (33.4996, 126.5312),
    "수원": (37.2636, 127.0286), "성남": (37.4200, 127.1265), "고양": (37.6584, 126.8320),
    "용인": (37.2411, 127.1776), "창원": (35.2280, 128.6811), "청주": (36.6424, 127.4890),
    "전주": (35.8242, 127.1480), "천안": (36.8151, 127.1139), "포항": (36.0190, 129.3435),
    "강릉": (37.7519, 128.8761), "춘천": (37.8813, 127.7298),
}
WEATHER_CODE = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "약한 이슬비", 53: "이슬비", 55: "강한 이슬비", 61: "약한 비", 63: "비", 65: "강한 비",
    66: "어는 비", 67: "어는 비", 71: "약한 눈", 73: "눈", 75: "많은 눈", 77: "싸락눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기", 85: "눈 소나기", 86: "강한 눈 소나기",
    95: "뇌우", 96: "우박 동반 뇌우", 99: "우박 동반 뇌우",
}
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "도시의 현재 날씨와 오늘~모레 예보를 가져온다. 날씨 질문엔 반드시 사용.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "도시 이름 (예: 서울, 부산, Tokyo). 말 안 하면 서울"}},
            "required": ["city"],
        },
    },
}]


def load_persona() -> str:
    try:
        with open(PERSONA_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "너는 디스코드 봇 삭발봇이다. 짧고 친근하게 답한다."


class Ask(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.persona = load_persona()
        self.session: aiohttp.ClientSession | None = None
        self.usage: dict[int, int] = {}         # 오늘 질문 수 (봇 재시작 시 초기화)
        self.usage_day = ""
        self.last_at: dict[int, float] = {}     # 쿨타임
        self.history: dict[int, list] = {}      # 유저별 최근 대화 (짧게 기억)

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ── 날씨 도구 ─────────────────────────────────────
    async def get_weather(self, city: str) -> dict:
        city = (city or "서울").strip()
        coords = CITY_COORDS.get(city.removesuffix("시"))
        name = city
        if not coords:
            async with self.session.get("https://geocoding-api.open-meteo.com/v1/search",
                                        params={"name": city, "count": 1, "language": "ko"}) as r:
                res = (await r.json()).get("results") or []
            if not res:
                return {"error": f"'{city}' 위치를 찾지 못함"}
            coords, name = (res[0]["latitude"], res[0]["longitude"]), res[0].get("name", city)
        params = {
            "latitude": coords[0], "longitude": coords[1], "timezone": "Asia/Seoul", "forecast_days": 3,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        }
        async with self.session.get("https://api.open-meteo.com/v1/forecast", params=params) as r:
            d = await r.json()
        cur, daily = d.get("current", {}), d.get("daily", {})
        days = []
        for i, day in enumerate(daily.get("time", [])):
            days.append({
                "날짜": day, "날씨": WEATHER_CODE.get(daily["weather_code"][i], "알 수 없음"),
                "최고": daily["temperature_2m_max"][i], "최저": daily["temperature_2m_min"][i],
                "강수확률": daily["precipitation_probability_max"][i],
            })
        return {"도시": name, "현재": {"기온": cur.get("temperature_2m"), "체감": cur.get("apparent_temperature"),
                                     "날씨": WEATHER_CODE.get(cur.get("weather_code"), "알 수 없음"),
                                     "풍속(km/h)": cur.get("wind_speed_10m")}, "예보": days}

    # ── OpenAI 호출 ───────────────────────────────────
    async def chat(self, messages: list) -> dict:
        # 답변 길이 옵션 이름이 서비스마다 다름 (OpenAI: max_completion_tokens / Mistral·Groq 등: max_tokens)
        token_key = "max_completion_tokens" if "api.openai.com" in config.OPENAI_BASE_URL else "max_tokens"
        body = {"model": config.OPENAI_MODEL, "messages": messages, "tools": TOOLS,
                token_key: config.ASK_MAX_TOKENS}
        if config.ASK_REASONING_EFFORT:
            body["reasoning_effort"] = config.ASK_REASONING_EFFORT
        async with self.session.post(
            f"{config.OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            json=body,
        ) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"OpenAI {r.status}: {(data.get('error') or {}).get('message', data)}")
            return data["choices"][0]["message"]

    async def answer(self, user: discord.abc.User, question: str) -> str:
        now_kst = datetime.datetime.now(KST)
        system = f"{self.persona}\n\n[상황]\n지금은 {now_kst:%Y-%m-%d %H:%M} (한국 시간). 질문한 사람: {user.display_name}"
        past = self.history.get(user.id, [])
        messages = [{"role": "system", "content": system}, *past, {"role": "user", "content": question[:500]}]

        for _ in range(3):  # 도구 호출 → 결과 전달 → 최종 답 (최대 3번)
            msg = await self.chat(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = (msg.get("content") or "").strip() or "…잠깐 머리가 하얘졌다. 다시 물어봐 줘."
                self.history[user.id] = (past + [{"role": "user", "content": question[:500]},
                                                 {"role": "assistant", "content": text}])[-6:]
                return text[:1500]
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                    result = await self.get_weather(args.get("city", "서울")) if c["function"]["name"] == "get_weather" else {"error": "없는 도구"}
                except Exception as e:
                    result = {"error": f"날씨 조회 실패: {e}"}
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result, ensure_ascii=False)})
        return "…생각이 너무 길어졌다. 질문을 조금 짧게 해 줘."

    # ── 사용 제한 ─────────────────────────────────────
    async def check_limit(self, user: discord.abc.User) -> str | None:
        today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
        if today != self.usage_day:
            self.usage, self.usage_day = {}, today
        if await self.bot.is_owner(user):
            return None
        if time.monotonic() - self.last_at.get(user.id, 0) < config.ASK_COOLDOWN:
            return "⏳ 숨 좀 고르고. 몇 초 뒤에 다시 물어봐."
        if self.usage.get(user.id, 0) >= config.ASK_DAILY_LIMIT:
            return f"🧘 오늘 질문은 {config.ASK_DAILY_LIMIT}번까지다. 내일 다시 와라… 이것도 수련이다."
        return None

    async def handle(self, user, question: str) -> str:
        if not config.OPENAI_API_KEY:
            return "❌ 대화 기능이 아직 설정되지 않았어요. (OPENAI_API_KEY)"
        if not question.strip():
            return "할 말이 있으면 말해 봐."
        blocked = await self.check_limit(user)
        if blocked:
            return blocked
        self.last_at[user.id] = time.monotonic()
        self.usage[user.id] = self.usage.get(user.id, 0) + 1
        try:
            return await self.answer(user, question)
        except Exception as e:
            log.error("대화 실패: %s", e)
            self.usage[user.id] -= 1  # 실패한 질문은 횟수에서 빼 준다
            return "…지금은 대답하기 어렵다. 잠시 후에 다시 물어봐."

    # ── 진입점 ────────────────────────────────────────
    @app_commands.command(name="물어봐", description="삭발봇에게 짧게 물어봅니다. (날씨도 가능)")
    @app_commands.describe(question="질문 (예: 내일 서울 날씨 어때?)")
    @app_commands.rename(question="질문")
    async def ask_cmd(self, interaction: discord.Interaction, question: str):
        await interaction.response.defer(thinking=True)
        reply = await self.handle(interaction.user, question)
        await interaction.followup.send(f"> {question[:200]}\n{reply}", allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.bot.user or self.bot.user not in message.mentions:
            return
        if message.content.startswith(config.PREFIX):
            return
        question = message.content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
        async with message.channel.typing():
            reply = await self.handle(message.author, question)
        await message.reply(reply, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Ask(bot))
