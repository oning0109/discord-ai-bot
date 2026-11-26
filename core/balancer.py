"""팀 밸런싱 + 오버워치 레이팅

오버워치 (5대5, 탱1 딜2 힐2)
  ① 포지션 선호: 주 / 부 / 안 함  (점수와 별개)
  ② 레이팅: 경기 전 예상 승률 E 를 계산하고, 결과와의 차이만큼 점수 변동  Δ = K × (결과 − E)
     K 는 그 포지션 경기 수가 적을수록 커서 처음 매긴 점수가 틀려도 빨리 맞춰진다
  ③ 팀 평가 함수 (낮을수록 좋음) — 모든 팀 분할 × 포지션 배정을 전부 계산해 최솟값을 고른다
       '안 함' 포지션 배정  × W_OFF
     + |예상 승률 − 50%|(%p) × W_FAIR
     + 포지션 맞대결 점수차(100점당, 탱커 가중) × W_MIRROR
     + 부 포지션 배정 × W_SUB
     + 최근 같은 팀이었던 횟수 × W_REPEAT
"""
import itertools
import math
import random
from functools import lru_cache

from core.ranks import OW_ROLES

# ───────────── 튜닝값 (밸런스가 마음에 안 들면 여기만 조절) ─────────────
ROLE_WEIGHT = {"tank": 1.3, "deal": 1.0, "heal": 1.0}  # 팀 전력 계산 시 포지션 비중 (5대5 탱커 영향 큼)
W_OFF = 1000      # '안 함' 포지션에 1명 배정될 때 벌점 (사실상 최후의 수단)
W_FAIR = 10       # 예상 승률이 50%에서 1%p 벗어날 때 벌점
W_MIRROR = 1.5    # 같은 포지션끼리 점수 차 100점당 벌점
W_SUB = 12        # 부 포지션 배정 1명당 벌점 (≈ 예상 승률 1.2%p)
W_REPEAT = 1.5    # 최근 경기에서 같은 팀이었던 쌍 1회당 벌점
OFF_ROLE_PENALTY = 500  # '안 함' 포지션을 하게 되면 본인 최고 점수에서 이만큼 낮게 계산
K_TABLE = ((5, 80), (15, 60), (10**9, 40))  # (그 포지션 경기 수 미만, K)  → 50:50 경기면 ±40 / ±30 / ±20
RESHUFFLE_TOL_OW = 25   # 다시 섞기: 최선 대비 평가 점수 이 정도까지 허용 (≈ 예상 승률 2.5%p)
RESHUFFLE_TOL_VAL = 30  # 다시 섞기(발로): 팀 평균 점수 차 허용

OW_TEAM_SLOTS = ("tank", "deal", "heal", "deal", "heal")  # 인원이 적으면 앞에서부터 (4명=탱딜힐딜)
OW_MAX_PLAYERS = 10


# ───────────────────────── 공통 ─────────────────────────
def expected_win(r_a: float, r_b: float) -> float:
    """Elo 예상 승률 (A팀 기준, 0~1)"""
    return 1.0 / (1.0 + math.pow(10, (r_b - r_a) / 400.0))


def _pid(p: dict):
    m = p.get("member")
    return m.id if m is not None and hasattr(m, "id") else id(p)


def split_key(team_a: list, team_b: list) -> frozenset:
    """누가 어느 팀인지만 나타내는 키 (A/B 뒤바뀐 같은 조합은 같은 키)"""
    return frozenset((frozenset(_pid(p) for p in team_a), frozenset(_pid(p) for p in team_b)))


def _pick(candidates: list, avoid: set, tolerance: float):
    """candidates: [(score, split, payload)]. 이미 보여준 조합은 피하고, 최고 점수 + tolerance 이내에서 무작위"""
    fresh = [c for c in candidates if c[1] not in avoid] or candidates
    best = min(c[0] for c in fresh)
    pool = [c for c in fresh if c[0] <= best + max(tolerance, 1e-9)]
    return random.choice(pool)


def _splits(n: int):
    half = n // 2
    for combo in itertools.combinations(range(n), half):
        if n % 2 == 0 and 0 not in combo:  # 좌우 대칭 중복 제거
            continue
        yield combo, tuple(i for i in range(n) if i not in combo)


# ───────────────────────── 오버워치 ─────────────────────────
def ow_effective_mmr(p: dict, role: str) -> float:
    if p["prefs"].get(role, "no") != "no":
        return float(p.get(f"{role}_mmr", 1000))
    playable = [p.get(f"{r}_mmr", 1000) for r in OW_ROLES if p["prefs"].get(r, "no") != "no"]
    return float(max(max(playable, default=1000) - OFF_ROLE_PENALTY, 0))


def ow_team_rating(team: list, roles: list) -> float:
    total = sum(ROLE_WEIGHT[r] * ow_effective_mmr(p, r) for p, r in zip(team, roles))
    return total / max(sum(ROLE_WEIGHT[r] for r in roles), 1e-9)


def k_factor(games: int) -> int:
    for limit, k in K_TABLE:
        if games < limit:
            return k
    return K_TABLE[-1][1]


@lru_cache(maxsize=None)
def _slot_perms(size: int) -> tuple:
    return tuple(sorted(set(itertools.permutations(OW_TEAM_SLOTS[:size]))))


def pair_counts_from_matches(matches: list) -> dict:
    """최근 경기들에서 같은 팀이었던 쌍 → 횟수"""
    counts = {}
    for m in matches:
        sides = {}
        for e in m.get("entries") or []:
            if e.get("side") in ("A", "B"):
                sides.setdefault(e["side"], []).append(int(e["discord_id"]))
        for ids in sides.values():
            for a, b in itertools.combinations(sorted(ids), 2):
                counts[(a, b)] = counts.get((a, b), 0) + 1
    return counts


def _repeat(team_ids: list, pair_counts: dict) -> int:
    return sum(pair_counts.get((a, b), 0) for a, b in itertools.combinations(sorted(team_ids), 2))


def evaluate_ow(team_a: list, roles_a: list, team_b: list, roles_b: list, pair_counts: dict | None = None) -> dict:
    """주어진 배치의 평가 (점수가 낮을수록 좋음)"""
    def stats(team, roles):
        off = sum(1 for p, r in zip(team, roles) if p["prefs"].get(r, "no") == "no")
        sub = sum(1 for p, r in zip(team, roles) if p["prefs"].get(r) == "sub")
        per = {r: [] for r in OW_ROLES}
        for p, r in zip(team, roles):
            per[r].append(ow_effective_mmr(p, r))
        return off, sub, per

    off_a, sub_a, per_a = stats(team_a, roles_a)
    off_b, sub_b, per_b = stats(team_b, roles_b)
    r_a, r_b = ow_team_rating(team_a, roles_a), ow_team_rating(team_b, roles_b)
    e_a = expected_win(r_a, r_b)
    mirror = 0.0
    for r in OW_ROLES:
        a, b = sorted(per_a[r], reverse=True), sorted(per_b[r], reverse=True)
        for x, y in zip(a, b):
            mirror += ROLE_WEIGHT[r] * abs(x - y) / 100
    rep = 0
    if pair_counts:
        rep = _repeat([_pid(p) for p in team_a], pair_counts) + _repeat([_pid(p) for p in team_b], pair_counts)
    score = (W_OFF * (off_a + off_b) + W_FAIR * abs(e_a - 0.5) * 100
             + W_MIRROR * mirror + W_SUB * (sub_a + sub_b) + W_REPEAT * rep)
    return {"score": score, "expected_a": e_a, "rating_a": r_a, "rating_b": r_b,
            "off": off_a + off_b, "sub": sub_a + sub_b, "repeat": rep}


def _annotate(team: list, roles: list) -> list:
    out = []
    for p, r in zip(team, roles):
        p["assigned_role"] = r
        p["pref_on_role"] = p["prefs"].get(r, "no")
        p["eff_mmr"] = ow_effective_mmr(p, r)
        out.append(p)
    out.sort(key=lambda p: OW_ROLES.index(p["assigned_role"]))
    return out


def balance_overwatch(players: list, avoid: set | None = None, tolerance: float = 0, pair_counts: dict | None = None):
    n = len(players)
    if n > OW_MAX_PLAYERS:
        raise ValueError(f"오버워치는 최대 {OW_MAX_PLAYERS}명(5대5)까지 팀을 짤 수 있어요. (현재 {n}명)")
    if n < 2:
        raise ValueError("최소 2명이 필요해요.")
    pair_counts = pair_counts or {}

    # 팀 하나 × 포지션 배정별로 미리 계산 (분할마다 재사용)
    cache = {}

    def team_options(idx: tuple):
        if idx in cache:
            return cache[idx]
        opts = []
        for perm in _slot_perms(len(idx)):
            off = sub = 0
            wsum = wtot = 0.0
            per = {r: [] for r in OW_ROLES}
            for i, r in zip(idx, perm):
                p = players[i]
                pref = p["prefs"].get(r, "no")
                off += pref == "no"
                sub += pref == "sub"
                v = ow_effective_mmr(p, r)
                wsum += ROLE_WEIGHT[r] * v
                wtot += ROLE_WEIGHT[r]
                per[r].append(v)
            for r in OW_ROLES:
                per[r].sort(reverse=True)
            opts.append((off, sub, wsum / wtot, per, perm))
        cache[idx] = opts
        return opts

    candidates = []
    for ia, ib in _splits(n):
        rep = 0
        if pair_counts:
            rep = (_repeat([_pid(players[i]) for i in ia], pair_counts)
                   + _repeat([_pid(players[i]) for i in ib], pair_counts))
        best = None
        for oa in team_options(ia):
            for ob in team_options(ib):
                base = W_OFF * (oa[0] + ob[0]) + W_SUB * (oa[1] + ob[1]) + W_REPEAT * rep
                if best is not None and base >= best[0]:
                    continue
                e_a = expected_win(oa[2], ob[2])
                mirror = sum(ROLE_WEIGHT[r] * abs(x - y) / 100 for r in OW_ROLES for x, y in zip(oa[3][r], ob[3][r]))
                score = base + W_FAIR * abs(e_a - 0.5) * 100 + W_MIRROR * mirror
                if best is None or score < best[0]:
                    best = (score, oa[4], ob[4])
        split = frozenset((frozenset(_pid(players[i]) for i in ia), frozenset(_pid(players[i]) for i in ib)))
        candidates.append((round(best[0], 6), split, (ia, ib, best[1], best[2])))

    _, _, (ia, ib, pa, pb) = _pick(candidates, avoid or set(), tolerance)
    team_a = _annotate([players[i] for i in ia], list(pa))
    team_b = _annotate([players[i] for i in ib], list(pb))
    info = evaluate_ow(team_a, [p["assigned_role"] for p in team_a], team_b, [p["assigned_role"] for p in team_b], pair_counts)
    return team_a, team_b, info


def reannotate_ow(team_a: list, team_b: list, pair_counts: dict | None = None) -> dict:
    """수동으로 자리를 바꾼 뒤 다시 평가"""
    team_a[:] = _annotate(team_a, [p["assigned_role"] for p in team_a])
    team_b[:] = _annotate(team_b, [p["assigned_role"] for p in team_b])
    return evaluate_ow(team_a, [p["assigned_role"] for p in team_a], team_b, [p["assigned_role"] for p in team_b], pair_counts)


def ow_rating_deltas(team_a: list, team_b: list, winner: str) -> tuple[float, dict]:
    """경기 결과 → (A팀 예상 승률, {discord_id: mmr 변동}). '안 함' 포지션으로 뛴 사람은 점수 변동 없음"""
    e_a = expected_win(
        ow_team_rating(team_a, [p["assigned_role"] for p in team_a]),
        ow_team_rating(team_b, [p["assigned_role"] for p in team_b]),
    )
    deltas = {}
    for team, side, e in ((team_a, "A", e_a), (team_b, "B", 1 - e_a)):
        s = 1.0 if winner == side else 0.0
        for p in team:
            role = p["assigned_role"]
            if p["prefs"].get(role, "no") == "no":
                deltas[_pid(p)] = 0
            else:
                deltas[_pid(p)] = round(k_factor(p.get(f"{role}_games", 0)) * (s - e))
    return e_a, deltas


# ───────────────────────── 발로란트 ─────────────────────────
def calculate_adjusted_mmr(player: dict) -> float:
    """발로란트: 티어 점수는 경기로 안 변하므로 승률로 보정 (3판 이상, 50% 기준 ±1%p 당 ±3점)"""
    base = player["mmr"]
    if player.get("wins", 0) + player.get("losses", 0) >= 3:
        return max(50.0, base + (player.get("win_rate", 50.0) - 50.0) * 3.0)
    return float(base)


def balance_valorant(players: list, avoid: set | None = None, tolerance: float = 0):
    for p in players:
        p["adj_mmr"] = calculate_adjusted_mmr(p)
    candidates = []
    for ia, ib in _splits(len(players)):
        a, b = [players[i] for i in ia], [players[i] for i in ib]
        diff = abs(sum(p["adj_mmr"] for p in a) / len(a) - sum(p["adj_mmr"] for p in b) / len(b))
        candidates.append((round(diff, 1), split_key(a, b), (a, b)))
    _, _, (a, b) = _pick(candidates, avoid or set(), tolerance)
    avg = lambda t: sum(p["adj_mmr"] for p in t) / len(t)
    return a, b, {"expected_a": expected_win(avg(a), avg(b)), "diff": abs(avg(a) - avg(b))}


# ───────────────────────── 롤 (랜덤) ─────────────────────────
def balance_random(players: list, avoid: set | None = None):
    for _ in range(30):
        shuffled = random.sample(players, len(players))
        a, b = shuffled[: len(players) // 2], shuffled[len(players) // 2:]
        if split_key(a, b) not in (avoid or set()):
            break
    return a, b, {"expected_a": None}


def balance(players: list, game: str, avoid: set | None = None, reshuffle: bool = False, pair_counts: dict | None = None):
    """reshuffle=True 면 이미 보여준 조합을 피하고, 최선과 비슷한 조합 중 무작위"""
    if game == "옵치":
        return balance_overwatch(players, avoid, RESHUFFLE_TOL_OW if reshuffle else 0, pair_counts)
    if game == "발로란트":
        return balance_valorant(players, avoid, RESHUFFLE_TOL_VAL if reshuffle else 0)
    return balance_random(players, avoid)
