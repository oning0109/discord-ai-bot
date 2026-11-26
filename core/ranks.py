"""티어/MMR 변환과 DB 행 → 게임 프로필 변환 (순수 함수만)"""
import json

GAMES = ("발로란트", "옵치")          # MMR 을 쓰는 게임 (롤은 랜덤 팀만)
LOBBY_GAMES = ("발로란트", "옵치", "롤")
GAME_ALIASES = {
    "발로": "발로란트", "발로란트": "발로란트", "valorant": "발로란트",
    "옵치": "옵치", "오버워치": "옵치", "overwatch": "옵치",
    "롤": "롤", "lol": "롤", "칼바람": "롤",
}


def normalize_game(text) -> str | None:
    return GAME_ALIASES.get(str(text or "").strip().lower())


# ── 발로란트 ─────────────────────────────────────────
VAL_TIERS = {
    "아이언": 100, "브론즈": 200, "실버": 300, "골드": 400, "플래티넘": 500,
    "다이아몬드": 600, "초월자": 650, "불멸": 800, "레디언트": 1000,
}
VAL_ROLES = ["타격대", "척후대", "감시자", "전략가"]

# ── 오버워치 ─────────────────────────────────────────
OW_ROLES = ("tank", "deal", "heal")
OW_ROLE_NAME = {"tank": "탱커", "deal": "딜러", "heal": "힐러"}
OW_ROLE_EMOJI = {"tank": "🛡️", "deal": "⚔️", "heal": "💉"}
OW_PREF_NAME = {"main": "주", "sub": "부", "no": "안 함"}

# 옵치 점수 ↔ 티어 (옛 경쟁전 점수 기준: 등급 시작점 + 디비전당 100점)
OW_RANKS = [
    OW_RANKS = [
    ("bronze", "브론즈", 500), ("silver", "실버", 1000), ("gold", "골드", 1500),
    ("platinum", "플래티넘", 2000), ("emerald", "에메랄드", 2500), ("diamond", "다이아", 3000),
    ("master", "마스터", 3500), ("grandmaster", "그랜드마스터", 4000), ("champion", "챔피언", 4500),
]
]
OW_RANK_KO = {k: ko for k, ko, _ in OW_RANKS}
OW_RANK_BASE = {k: base for k, _, base in OW_RANKS}


def ow_tier(mmr: int) -> str:
    """옵치 점수 → '다이아 3' 같은 티어 이름"""
    mmr = int(mmr or 0)
    key, ko, base = OW_RANKS[0]
    for k, name, b in OW_RANKS:
        if mmr >= b:
            key, ko, base = k, name, b
    div = 5 - min(max((mmr - base) // 100, 0), 4)
    return f"{ko} {div}"


def ow_rank_mmr(rank_key: str, division: int = 3) -> int:
    """자가 신고 티어 → 시작 점수 (기본은 디비전 3 = 등급 중간)"""
    return OW_RANK_BASE[rank_key] + (5 - min(max(int(division), 1), 5)) * 100


def ow_rank_label(rank: dict | None) -> str:
    """OverFast 응답의 랭크 → '다이아 3' (재미용 표시)"""
    if not rank:
        return "배치 안 됨"
    return f"{OW_RANK_KO.get(str(rank.get('division', '')).lower(), rank.get('division', '?'))} {rank.get('tier', '')}".strip()


OVERFAST_ROLE_MAP = {"tank": "tank", "damage": "deal", "support": "heal"}


def normalize_ow_role(text) -> str | None:
    t = str(text or "").strip().lower()
    if t in ("탱", "탱커", "tank"):
        return "tank"
    if t in ("딜", "딜러", "dps", "deal", "damage"):
        return "deal"
    if t in ("힐", "힐러", "heal", "support", "서포터"):
        return "heal"
    return None


# ── DB 행 파싱 ───────────────────────────────────────
def _json(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return v if v is not None else default


def _wr(w: int, l: int) -> float:
    return round(w / (w + l) * 100, 1) if w + l else 50.0


def parse_profile(row: dict | None) -> dict:
    """user_profiles 1행 → {"발로란트": {...}, "옵치": {...}} (등록된 게임만)"""
    if not row:
        return {}
    out = {}
    if row.get("val_tier"):
        w, l = row.get("val_wins") or 0, row.get("val_losses") or 0
        out["발로란트"] = {
            "tier": row["val_tier"], "mmr": row.get("val_mmr") or VAL_TIERS.get(row["val_tier"], 400),
            "roles": _json(row.get("val_roles"), []), "wins": w, "losses": l, "win_rate": _wr(w, l),
        }
    prefs = _json(row.get("ow_prefs"), None)
    if prefs and any(prefs.get(r, "no") != "no" for r in OW_ROLES):
        w, l = row.get("ow_wins") or 0, row.get("ow_losses") or 0
        ow = {"wins": w, "losses": l, "win_rate": _wr(w, l), "prefs": {r: prefs.get(r, "no") for r in OW_ROLES},
              "battletag": row.get("ow_battletag"), "ranks": _json(row.get("ow_ranks"), {}) or {}}
        for r in OW_ROLES:
            v = row.get(f"{r}_mmr")
            ow[f"{r}_mmr"] = 1000 if v is None else v
            ow[f"{r}_wins"] = row.get(f"{r}_wins") or 0
            ow[f"{r}_losses"] = row.get(f"{r}_losses") or 0
            ow[f"{r}_games"] = ow[f"{r}_wins"] + ow[f"{r}_losses"]
        out["옵치"] = ow
    elif row.get("ow_battletag"):  # 내전 등록은 안 했지만 /옵치연동 랭크는 있음
        out["_ow_display"] = {"battletag": row.get("ow_battletag"), "ranks": _json(row.get("ow_ranks"), {}) or {}}
    return out


def player_from_row(member, row: dict | None, game: str) -> dict:
    """팀 편성용 참가자 dict"""
    p = parse_profile(row).get(game, {})
    base = {"member": member, "wins": p.get("wins", 0), "losses": p.get("losses", 0), "win_rate": p.get("win_rate", 50.0)}
    if game == "옵치":
        base["prefs"] = p.get("prefs") or {r: "sub" for r in OW_ROLES}
        for r in OW_ROLES:
            base[f"{r}_mmr"] = p.get(f"{r}_mmr", 1000)
            base[f"{r}_games"] = p.get(f"{r}_games", 0)
    else:
        base.update(tier=p.get("tier", "-"), mmr=p.get("mmr", 400), roles=p.get("roles", []))
    return base
