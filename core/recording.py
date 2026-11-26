"""경기 1판 기록 (대기실 결과 버튼 · /옵치경기기록 공용)"""
from core.balancer import ow_rating_deltas, calculate_adjusted_mmr, expected_win


async def record_full_match(db, game: str, team_a: list, team_b: list, winner: str, *,
                            host_id, recorded_by, source: str, step: int = 20) -> dict:
    """team_a/team_b: 팀 편성용 참가자 dict 목록 (옵치는 assigned_role 필요). winner: 'A' | 'B'

    반환: DB 결과 + expected_a. 롤은 기록하지 않는다.
    """
    if game == "옵치":
        e_a, deltas = ow_rating_deltas(team_a, team_b, winner)
    elif game == "발로란트":
        avg = lambda t: sum(calculate_adjusted_mmr(p) for p in t) / max(len(t), 1)
        e_a, deltas = expected_win(avg(team_a), avg(team_b)), {}
    else:
        return {"skipped": True}

    entries = []
    for side, team in (("A", team_a), ("B", team_b)):
        for p in team:
            m = p["member"]
            e = {"discord_id": str(m.id), "name": m.display_name, "side": side,
                 "role": p.get("assigned_role") if game == "옵치" else None,
                 "is_win": side == winner, "count": 1}
            if game == "옵치":
                e["mmr_delta"] = deltas.get(m.id, 0)
            entries.append(e)
    res = await db.record_match(game, source, entries, winner=winner, host_id=host_id,
                                recorded_by=recorded_by, step=step, expected_a=round(e_a, 4))
    res["expected_a"] = e_a
    return res
