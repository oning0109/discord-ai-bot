import json
import os
from supabase import Client, create_client

from balancer import OW_EXCLUDED_MMR

OW_MMR_STEP = 20  # 옵치 경기 1회당 포지션 MMR 증감 (승 +20 / 패 -20)

TIER_SCORES = {
    "아이언": 100,
    "브론즈": 200,
    "실버": 300,
    "골드": 400,
    "플래티넘": 500,
    "에메랄드": 550,
    "다이아몬드": 600,
    "초월자": 650,
    "마스터": 700,
    "그랜드마스터": 800,
    "챌린저": 900,
    "레디언트": 1000,
    "0tier": 3500,
    "1tier": 2800,
    "2tier": 2300,
    "3tier": 1800,
    "4tier": 1300,
    "5tier": 1000,
}


class DBManager:

    def __init__(self):
        url = os.getenv(
            "SUPABASE_URL", "https://tdatedpggtutuqrywguk.supabase.co"
        )
        key = os.getenv("SUPABASE_KEY")
        if not key:
            raise RuntimeError("SUPABASE_KEY 환경변수가 설정되지 않았습니다.")

        self.supabase: Client = create_client(url, key)

        # 💡 내전 대기실(Lobby) 메모리 저장소
        self.lobbies = {}

    # ==========================================
    # 🎮 내전 대기실 (Lobby) 관련 메서드
    # ==========================================
    def create_lobby(
        self, message_id: int, channel_id: int, host_id: int, game: str
    ):
        """새로운 내전 대기실 생성"""
        msg_key = int(message_id)
        self.lobbies[msg_key] = {
            "message_id": msg_key,
            "channel_id": int(channel_id),
            "host_id": int(host_id),
            "selected_game": game,
            "participants": [],  # 참가자 유저 ID 목록 (int)
        }

    def get_lobby(self, message_id: int) -> dict:
        """대기실 정보 조회"""
        return self.lobbies.get(int(message_id))

    def update_lobby_game(self, message_id: int, game: str):
        """대기실 내전 종목 변경"""
        lobby = self.get_lobby(message_id)
        if lobby:
            lobby["selected_game"] = game

    def get_lobby_participants(self, message_id: int) -> list:
        """대기실 참가자 ID 목록 가져오기"""
        lobby = self.get_lobby(message_id)
        return lobby.get("participants", []) if lobby else []

    def add_participant(self, message_id: int, user_id: int):
        """참가자 추가"""
        lobby = self.get_lobby(message_id)
        if lobby:
            uid_int = int(user_id)
            if uid_int not in lobby["participants"]:
                lobby["participants"].append(uid_int)

    def remove_participant(self, message_id: int, user_id: int):
        """참가자 제거"""
        lobby = self.get_lobby(message_id)
        if lobby:
            uid_int = int(user_id)
            if uid_int in lobby["participants"]:
                lobby["participants"].remove(uid_int)

    def delete_lobby(self, message_id: int):
        """대기실 폭파 / 종료시 삭제"""
        self.lobbies.pop(int(message_id), None)

    # ==========================================
    # 👤 유저 프로필 및 전적 관련 메서드
    # ==========================================
    def _get_user_row(self, user_id: int):
        """discord_id 또는 user_id로 유저 단일 행 안전하게 조회"""
        uid_str = str(user_id)

        try:
            res = (
                self.supabase.table("user_profiles")
                .select("*")
                .eq("discord_id", uid_str)
                .execute()
            )
            if res.data:
                return res.data[0]
        except Exception as e:
            print(f"⚠️ discord_id 조회 예외: {e}")

        try:
            res = (
                self.supabase.table("user_profiles")
                .select("*")
                .eq("user_id", uid_str)
                .execute()
            )
            if res.data:
                return res.data[0]
        except Exception as e:
            print(f"⚠️ user_id 조회 예외: {e}")

        return None

    def get_user_all_games(self, user_id: int) -> dict:
        """단 1번의 DB 조회로 모든 게임 프로필 데이터 파싱"""
        row = self._get_user_row(user_id)
        if not row:
            return {}

        result = {}

        # 1. 발로란트
        if row.get("val_tier"):
            wins = row.get("val_wins") or 0
            losses = row.get("val_losses") or 0
            total = wins + losses
            roles = row.get("val_roles") or []
            if isinstance(roles, str):
                try:
                    roles = json.loads(roles)
                except Exception:
                    roles = []
            result["발로란트"] = {
                "tier": row.get("val_tier", "5tier"),
                "mmr": row.get("val_mmr", 1000),
                "roles": roles,
                "wins": wins,
                "losses": losses,
                "win_rate": (
                    round((wins / total) * 100, 1) if total > 0 else 50.0
                ),
            }

        # 2. 롤
        if row.get("lol_tier"):
            wins = row.get("lol_wins") or 0
            losses = row.get("lol_losses") or 0
            total = wins + losses
            roles = row.get("lol_roles") or []
            if isinstance(roles, str):
                try:
                    roles = json.loads(roles)
                except Exception:
                    roles = []
            result["롤"] = {
                "tier": row.get("lol_tier", "5tier"),
                "mmr": row.get("lol_mmr", 1000),
                "roles": roles,
                "wins": wins,
                "losses": losses,
                "win_rate": (
                    round((wins / total) * 100, 1) if total > 0 else 50.0
                ),
            }

        # 3. 오버워치
        tank_m = row.get("tank_mmr")
        deal_m = row.get("deal_mmr")
        heal_m = row.get("heal_mmr")
        if tank_m is not None or deal_m is not None or heal_m is not None:
            ow_wins = row.get("ow_wins") or row.get("wins") or 0
            ow_losses = row.get("ow_losses") or row.get("losses") or 0
            ow_total = ow_wins + ow_losses
            result["옵치"] = {
                "tank_mmr": tank_m or 1000,
                "deal_mmr": deal_m or 1000,
                "heal_mmr": heal_m or 1000,
                "tank_wins": row.get("tank_wins") or 0,
                "tank_losses": row.get("tank_losses") or 0,
                "deal_wins": row.get("deal_wins") or 0,
                "deal_losses": row.get("deal_losses") or 0,
                "heal_wins": row.get("heal_wins") or 0,
                "heal_losses": row.get("heal_losses") or 0,
                "wins": ow_wins,
                "losses": ow_losses,
                "win_rate": (
                    round((ow_wins / ow_total) * 100, 1)
                    if ow_total > 0
                    else 50.0
                ),
            }

        return result

    def has_user_profile(self, user_id: int, game: str) -> bool:
        """게임별 프로필 존재 여부 (유연한 예외 및 하위 호환 처리 적용)"""
        row = self._get_user_row(user_id)
        if not row:
            return False

        # 1. 단일 행 컬럼 확인 (val_tier, lol_tier)
        if game == "발로란트" and row.get("val_tier"):
            return True
        elif game == "롤" and row.get("lol_tier"):
            return True
        elif game in ["옵치", "오버워치"] and (
            row.get("tank_mmr") is not None or row.get("deal_mmr") is not None
        ):
            return True

        # 2. 하위 호환: 혹시라도 game 컬럼에 별도로 나뉘어 등록되어 있는 경우 교차 확인
        try:
            uid_str = str(user_id)
            res = (
                self.supabase.table("user_profiles")
                .select("*")
                .or_(f"discord_id.eq.{uid_str},user_id.eq.{uid_str}")
                .eq("game", game)
                .execute()
            )
            if res.data and len(res.data) > 0:
                return True
        except Exception:
            pass

        return False

    def get_user_profile(self, user_id: int, game: str) -> dict:
        """특정 게임 프로필 조회"""
        all_games = self.get_user_all_games(user_id)
        return all_games.get(game, {})

    def set_user_profile(self, user_id: int, game: str, tier: str, roles: list):
        """프로필 설정 (1행 구조로 저장)"""
        uid_str = str(user_id)
        score = TIER_SCORES.get(tier, 1000)

        if game == "발로란트":
            payload = {
                "val_tier": tier,
                "val_mmr": score,
                "val_roles": roles,
                "updated_at": "now()",
            }
        elif game == "롤":
            payload = {
                "lol_tier": tier,
                "lol_mmr": score,
                "lol_roles": roles,
                "updated_at": "now()",
            }
        else:
            return

        row = self._get_user_row(user_id)
        if row:
            target_id = row.get("user_id") or uid_str
            try:
                self.supabase.table("user_profiles").update(payload).eq(
                    "discord_id", uid_str
                ).execute()
            except Exception:
                self.supabase.table("user_profiles").update(payload).eq(
                    "user_id", target_id
                ).execute()
        else:
            payload["user_id"] = uid_str
            payload["discord_id"] = uid_str
            payload["username"] = f"User_{uid_str}"
            self.supabase.table("user_profiles").insert(payload).execute()

    def record_ow_match_result(self, user_id: int, role: str, is_win: bool, count: int = 1):
        """오버워치 결과 반영: 전체 승패 + 포지션별 승패 + 포지션 MMR(±OW_MMR_STEP × count).
        성공하면 반영 결과 dict, 실패하면 None"""
        row = self._get_user_row(user_id)
        if not row:
            return None

        role_col_map = {
            "tank": "tank_mmr",
            "탱커": "tank_mmr",
            "deal": "deal_mmr",
            "딜러": "deal_mmr",
            "heal": "heal_mmr",
            "힐러": "heal_mmr",
        }
        target_col = role_col_map.get(role, "deal_mmr")
        prefix = target_col.replace("_mmr", "")  # tank / deal / heal
        wins_col, losses_col = f"{prefix}_wins", f"{prefix}_losses"

        curr_mmr = row.get(target_col)
        curr_mmr = 1000 if curr_mmr is None else curr_mmr
        ow_wins = row.get("ow_wins") or 0
        ow_losses = row.get("ow_losses") or 0
        role_wins = row.get(wins_col) or 0
        role_losses = row.get(losses_col) or 0

        if is_win:
            new_mmr = curr_mmr + OW_MMR_STEP * count
            ow_wins += count
            role_wins += count
        else:
            new_mmr = max(0, curr_mmr - OW_MMR_STEP * count)
            ow_losses += count
            role_losses += count

        # 실제로 플레이한 포지션이 딱 1000점이 되면 '안 하는 포지션'으로 오인되므로 1점 비켜준다
        if new_mmr == OW_EXCLUDED_MMR:
            new_mmr += 1 if is_win else -1

        payload = {
            target_col: new_mmr,
            "ow_wins": ow_wins,
            "ow_losses": ow_losses,
            wins_col: role_wins,
            losses_col: role_losses,
            "updated_at": "now()",
        }

        try:
            self.supabase.table("user_profiles").update(payload).eq(
                "user_id", row["user_id"]
            ).execute()
            return {
                "role": prefix,
                "mmr": new_mmr,
                "mmr_delta": new_mmr - curr_mmr,
                "role_wins": role_wins,
                "role_losses": role_losses,
                "wins": ow_wins,
                "losses": ow_losses,
            }
        except Exception as e:
            print(f"❌ record_ow_match_result 에러: {e}")
            return None

    MATCH_COLUMNS = {
        "발로란트": ("val_wins", "val_losses"),
        "롤": ("lol_wins", "lol_losses"),
        "옵치": ("ow_wins", "ow_losses"),
    }

    def add_match_record(self, user_id: int, game: str, wins: int = 0, losses: int = 0):
        """승/패 횟수를 누적 추가하고 (총 승, 총 패)를 반환. 실패하면 None"""
        cols = self.MATCH_COLUMNS.get(game)
        row = self._get_user_row(user_id)
        if not cols or not row:
            return None
        w_col, l_col = cols
        new_w = (row.get(w_col) or 0) + wins
        new_l = (row.get(l_col) or 0) + losses
        try:
            self.supabase.table("user_profiles").update(
                {w_col: new_w, l_col: new_l, "updated_at": "now()"}
            ).eq("user_id", row["user_id"]).execute()
            return new_w, new_l
        except Exception as e:
            print(f"❌ add_match_record 에러: {e}")
            return None

    def record_match_result(self, user_id: int, game: str, is_win: bool) -> bool:
        """발로란트/롤 경기 결과 1건 반영 (승패 횟수만 누적)"""
        res = self.add_match_record(
            user_id, game, wins=1 if is_win else 0, losses=0 if is_win else 1
        )
        return res is not None

    # ───────────────────────── 경기 이력 (matches 테이블) ─────────────────────────
    def save_match(self, game: str, source: str, recorded_by, entries: list, winner=None, host_id=None):
        """경기 1건을 저장하고 경기 번호(id)를 반환. 실패하면 None"""
        payload = {
            "game": game,
            "source": source,  # 'lobby'(봇 대기실) / 'manual'(!승패추가)
            "winner": winner,  # 'A' / 'B' / None
            "recorded_by": str(recorded_by) if recorded_by is not None else None,
            "host_id": str(host_id) if host_id is not None else None,
            "entries": entries,
        }
        try:
            res = self.supabase.table("matches").insert(payload).execute()
            return res.data[0]["id"] if res.data else None
        except Exception as e:
            print(f"❌ save_match 에러: {e}")
            return None

    def get_match(self, match_id: int):
        try:
            res = self.supabase.table("matches").select("*").eq("id", match_id).limit(1).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"❌ get_match 에러: {e}")
            return None

    def get_latest_match(self):
        """되돌리기 안 된 가장 최근 경기"""
        try:
            res = (
                self.supabase.table("matches")
                .select("*")
                .eq("undone", False)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"❌ get_latest_match 에러: {e}")
            return None

    def get_user_matches(self, user_id: int, game: str = None, limit: int = 10):
        """유저가 참여한 최근 경기 목록(최신순). 조회 실패 시 None, 없으면 []"""
        try:
            q = (
                self.supabase.table("matches")
                .select("id,created_at,game,source,winner,entries")
                .eq("undone", False)
                .filter("entries", "cs", json.dumps([{"discord_id": str(user_id)}]))
            )
            if game:
                q = q.eq("game", game)
            res = q.order("id", desc=True).limit(limit).execute()
            return res.data or []
        except Exception as e:
            print(f"❌ get_user_matches 에러: {e}")
            return None

    def _reverse_entry(self, game: str, e: dict) -> bool:
        """경기 기록 1명분을 원래대로 되돌린다 (승패 횟수, 옵치 포지션 전적/MMR)"""
        cols = self.MATCH_COLUMNS.get(game)
        row = self._get_user_row(int(e["discord_id"]))
        if not cols or not row:
            return False

        w_col, l_col = cols
        count = int(e.get("count", 1))
        is_win = bool(e.get("is_win"))
        payload = {}

        col = w_col if is_win else l_col
        payload[col] = max(0, (row.get(col) or 0) - count)

        role = e.get("role")
        if game == "옵치" and role in ("tank", "deal", "heal"):
            mmr_col = f"{role}_mmr"
            cur = row.get(mmr_col)
            cur = 1000 if cur is None else cur
            payload[mmr_col] = max(0, cur - int(e.get("mmr_delta", 0)))
            rc = f"{role}_wins" if is_win else f"{role}_losses"
            payload[rc] = max(0, (row.get(rc) or 0) - count)

        payload["updated_at"] = "now()"
        try:
            self.supabase.table("user_profiles").update(payload).eq("user_id", row["user_id"]).execute()
            return True
        except Exception as ex:
            print(f"❌ _reverse_entry 에러: {ex}")
            return False

    def undo_match(self, match_id: int, undone_by) -> dict:
        """경기 결과를 취소. 한 명이라도 실패하면 이미 되돌린 사람은 표시해 두어 재시도해도 중복 반영되지 않는다."""
        match = self.get_match(match_id)
        if not match:
            return {"ok": False, "error": "not_found"}
        if match.get("undone"):
            return {"ok": False, "error": "already_undone"}

        entries = match.get("entries") or []
        failed = []
        for e in entries:
            if e.get("reverted"):
                continue
            if self._reverse_entry(match["game"], e):
                e["reverted"] = True
            else:
                failed.append(e.get("name") or e.get("discord_id"))

        payload = {"entries": entries}
        if not failed:
            payload.update({"undone": True, "undone_at": "now()", "undone_by": str(undone_by)})
        try:
            self.supabase.table("matches").update(payload).eq("id", match_id).execute()
        except Exception as e:
            print(f"❌ undo_match 상태 저장 에러: {e}")
            return {"ok": False, "error": "db", "failed": failed}
        return {"ok": not failed, "failed": failed, "match": match}

    def force_update_profile(self, user_id: int, payload: dict) -> bool:
        """[소유자용] 지정한 컬럼 값을 그대로 덮어쓰기 (MMR/티어 강제 수정)"""
        row = self._get_user_row(user_id)
        if not row:
            return False
        data = {**payload, "updated_at": "now()"}
        try:
            self.supabase.table("user_profiles").update(data).eq(
                "user_id", row["user_id"]
            ).execute()
            return True
        except Exception as e:
            print(f"❌ force_update_profile 에러: {e}")
            return False

    def get_all_user_profiles(self) -> list:
        """전체 유저 프로필 목록 가져오기"""
        try:
            res = self.supabase.table("user_profiles").select("*").execute()
            return res.data or []
        except Exception as e:
            print(f"❌ get_all_user_profiles 에러: {e}")
            return []

    # ==========================================
    # 🎂 생일 축하 관련 메서드
    # ==========================================
    def set_birthday(self, user_id: int, birthday_str: str) -> bool:
        """유저 생일 등록/수정 (MM-DD)"""
        uid_str = str(user_id)
        try:
            row = self._get_user_row(user_id)
            if row:
                self.supabase.table("user_profiles").update(
                    {"birthday": birthday_str}
                ).or_(f"discord_id.eq.{uid_str},user_id.eq.{uid_str}").execute()
            else:
                payload = {
                    "user_id": uid_str,
                    "discord_id": uid_str,
                    "username": f"User_{uid_str}",
                    "birthday": birthday_str,
                }
                self.supabase.table("user_profiles").insert(payload).execute()
            return True
        except Exception as e:
            print(f"❌ 생일 등록 실패: {e}")
            return False

    def get_todays_birthdays(self, today_str: str) -> list:
        """오늘(MM-DD) 생일인 유저 목록 조회"""
        try:
            res = (
                self.supabase.table("user_profiles")
                .select("discord_id, username")
                .eq("birthday", today_str)
                .execute()
            )
            return res.data or []
        except Exception as e:
            print(f"❌ 생일 유저 조회 실패: {e}")
            return []