# balancer.py
import math
import random
import itertools
from functools import lru_cache

def calculate_adjusted_mmr(player: dict, base_mmr: float | None = None) -> float:
    """
    기본 MMR에 승률 가중치를 반영
    (3판 이상 진행 후 승률 50% 기준 ±10%당 MMR ±30점 조정)
    """
    if base_mmr is None:
        base_mmr = player['mmr']
    wins = player.get('wins', 0)
    losses = player.get('losses', 0)
    total_games = wins + losses
    win_rate = player.get('win_rate', 50.0)

    if total_games >= 3:
        win_rate_modifier = (win_rate - 50.0) * 3.0
        return max(50.0, base_mmr + win_rate_modifier)
    return float(base_mmr)


def calculate_expected_win_rate(avg_mmr_a: float, avg_mmr_b: float) -> tuple[float, float]:
    """Elo 공식을 활용한 A팀과 B팀의 예상 승률 계산"""
    if avg_mmr_a == 0 and avg_mmr_b == 0:
        return 50.0, 50.0

    prob_a = 1.0 / (1.0 + math.pow(10, (avg_mmr_b - avg_mmr_a) / 400.0))
    prob_b = 1.0 - prob_a

    return round(prob_a * 100, 1), round(prob_b * 100, 1)


# ───────────────────────── 다시 섞기 지원 (조합 식별 / 후보 선택) ─────────────────────────
def _pid(p: dict):
    """참가자 식별자: 디스코드 멤버 ID (없으면 객체 id)"""
    m = p.get("member")
    return m.id if m is not None and hasattr(m, "id") else id(p)


def split_key(team_a: list, team_b: list) -> frozenset:
    """'누가 어느 팀인지'만 나타내는 키 (A/B 위치가 바뀐 같은 조합은 같은 키)"""
    return frozenset((frozenset(_pid(p) for p in team_a), frozenset(_pid(p) for p in team_b)))


def _choose(candidates: list, avoid: set, tolerance: float, diff_idx: int):
    """
    candidates: [(key, split, payload)]  key는 작을수록 좋음, key[diff_idx]가 팀 평균 MMR 차이.
    - tolerance == 0 : 가장 좋은 key와 완전히 같은 후보들 중 무작위 (기존 동작)
    - tolerance  > 0 : 가장 좋은 차이 + tolerance 이내 후보들 중 무작위
    - avoid          : 이미 보여준 조합(split) 제외. 전부 제외돼 남는 게 없으면 전체에서 고름
    """
    fresh = [c for c in candidates if c[1] not in avoid] or candidates
    best = min(fresh, key=lambda c: c[0])
    if tolerance <= 0:
        pool = [c for c in fresh if c[0] == best[0]]
    else:
        pool = [
            c for c in fresh
            if c[0][:diff_idx] == best[0][:diff_idx] and c[0][diff_idx] <= best[0][diff_idx] + tolerance
        ]
    return random.choice(pool)


# ───────────────────────── 오버워치 (탱/딜/힐 포지션 배정) ─────────────────────────
OW_ROLE_ORDER = ("tank", "deal", "heal")
OW_ROLE_LABEL = {"tank": "탱", "deal": "딜", "heal": "힐"}
OW_TEAM_SLOTS = ("tank", "deal", "heal", "deal", "heal")  # 5인 팀 = 탱1 딜2 힐2 (인원이 적으면 앞에서부터)
OW_MAX_PLAYERS = 10
OW_EXCLUDED_MMR = 1000  # 이 점수인 포지션은 "안 가는 포지션"으로 보고 편성에서 제외


def _ow_role_mmr(p: dict, role: str) -> int:
    v = p.get(f"{role}_mmr")
    return 1000 if v is None else v


def get_ow_eligible_roles(p: dict) -> list:
    """1000점이 아닌 포지션만 배정 가능. 셋 다 1000점이면(미등록/올라운더) 전부 가능."""
    roles = [r for r in OW_ROLE_ORDER if _ow_role_mmr(p, r) != OW_EXCLUDED_MMR]
    return roles if roles else list(OW_ROLE_ORDER)


@lru_cache(maxsize=None)
def _slot_perms(size: int) -> tuple:
    return tuple(sorted(set(itertools.permutations(OW_TEAM_SLOTS[:size]))))


def balance_overwatch_teams(players: list, avoid: set | None = None, tolerance: float = 0):
    """
    포지션 배정 + 팀 분배를 한 번에 계산.
    우선순위: ① 미선택 포지션(1000점) 배정 인원 최소 → ② 팀 평균 MMR 차이 최소
              → ③ 포지션별(탱/딜/힐) 점수 차이 최소. 동점이면 무작위.
    """
    n = len(players)
    if n > OW_MAX_PLAYERS:
        raise ValueError(f"오버워치는 최대 {OW_MAX_PLAYERS}명(5vs5)까지만 팀을 짤 수 있어요. (현재 {n}명)")

    half = n // 2
    eligible = [set(get_ow_eligible_roles(p)) for p in players]
    adj = [
        {r: calculate_adjusted_mmr(p, base_mmr=_ow_role_mmr(p, r)) for r in OW_ROLE_ORDER}
        for p in players
    ]

    cache = {}

    def options(team: tuple) -> list:
        if team in cache:
            return cache[team]
        res = []
        for perm in _slot_perms(len(team)):
            off = 0
            total = 0.0
            per = {"tank": 0.0, "deal": 0.0, "heal": 0.0}
            for idx, role in zip(team, perm):
                if role not in eligible[idx]:
                    off += 1
                v = adj[idx][role]
                total += v
                per[role] += v
            res.append((off, total, (per["tank"], per["deal"], per["heal"]), perm))
        cache[team] = res
        return res

    candidates = []  # (key, split, payload)  — 조합(split)마다 가장 좋은 포지션 배정만 후보로 둔다
    for combo in itertools.combinations(range(n), half):
        if n % 2 == 0 and 0 not in combo:  # 좌우 대칭 중복 제거
            continue
        rest = tuple(i for i in range(n) if i not in combo)
        split = frozenset((
            frozenset(_pid(players[i]) for i in combo),
            frozenset(_pid(players[i]) for i in rest),
        ))
        opts_a = options(combo)
        opts_b = options(rest)

        best_key = None
        ties = []
        for oa in opts_a:
            for ob in opts_b:
                off = oa[0] + ob[0]
                diff = round(abs(oa[1] / len(combo) - ob[1] / len(rest)), 1)
                if best_key is not None and (off, diff) > best_key[:2]:
                    continue
                role_diff = sum(abs(x - y) for x, y in zip(oa[2], ob[2]))
                key = (off, diff, role_diff)
                if best_key is None or key < best_key:
                    best_key = key
                    ties = [(combo, rest, oa[3], ob[3])]
                elif key == best_key:
                    ties.append((combo, rest, oa[3], ob[3]))
        for t in ties:
            candidates.append((best_key, split, t))

    chosen_key, _, (combo, rest, perm_a, perm_b) = _choose(candidates, avoid or set(), tolerance, diff_idx=1)

    def build(team: tuple, perm: tuple) -> list:
        members = []
        for idx, role in zip(team, perm):
            p = players[idx]
            p["assigned_role"] = role
            p["adj_mmr"] = adj[idx][role]
            p["off_role"] = role not in eligible[idx]
            members.append(p)
        members.sort(key=lambda p: OW_ROLE_ORDER.index(p["assigned_role"]))
        return members

    return build(combo, perm_a), build(rest, perm_b), chosen_key[1]


def balance_teams_with_roles(players: list, game: str, avoid: set | None = None, tolerance: float = 0):
    """승률 보정 MMR 기반 균등 팀 분배"""
    if game == "옵치":
        return balance_overwatch_teams(players, avoid, tolerance)

    n = len(players)
    half = n // 2

    # 모든 참가자 승률 반영 MMR 계산
    for p in players:
        p['adj_mmr'] = calculate_adjusted_mmr(p)

    candidates = []
    for combo in itertools.combinations(range(n), half):
        if n % 2 == 0 and 0 not in combo:  # 좌우 대칭 중복 제거
            continue
        rest = tuple(i for i in range(n) if i not in combo)
        team_a = [players[i] for i in combo]
        team_b = [players[i] for i in rest]
        avg_a = sum(p['adj_mmr'] for p in team_a) / len(team_a) if team_a else 0
        avg_b = sum(p['adj_mmr'] for p in team_b) / len(team_b) if team_b else 0
        diff = round(abs(avg_a - avg_b), 1)
        candidates.append(((diff,), split_key(team_a, team_b), (team_a, team_b)))

    key, _, (team_a, team_b) = _choose(candidates, avoid or set(), tolerance, diff_idx=0)
    return team_a, team_b, key[0]