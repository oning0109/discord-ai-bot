-- =====================================================================
-- 삭발봇 v2 마이그레이션
--  * 기존 테이블/데이터는 지우거나 바꾸지 않음 (컬럼·테이블·함수 추가만)
--  * 새 함수는 service_role(봇 전용 키)만 실행 가능
-- =====================================================================

-- 1) user_profiles: 서버 입·퇴장 상태 + 오버워치 배틀태그/인게임 랭크
alter table public.user_profiles
  add column if not exists in_guild    boolean not null default true,
  add column if not exists left_at     timestamptz,
  add column if not exists ow_battletag text,
  add column if not exists ow_ranks    jsonb,
  add column if not exists ow_ranks_at timestamptz,
  add column if not exists ow_prefs    jsonb;   -- {"tank":"main|sub|no","deal":..,"heal":..}

-- 기존 옵치 점수 → 포지션 선호로 변환 (한 번만, 비어 있는 사람만)
--  * 1000점(기존 '안 함' 표시)이거나, 다른 포지션은 1300점 이상인데 1000점 근처(940~1060)면 '안 함'
--  * 가장 높은 포지션 = 주, 나머지 = 부   → 본인이 /등록 으로 언제든 고칠 수 있음
update public.user_profiles u
   set ow_prefs = (
     select jsonb_object_agg(r.role,
              case when r.v = 1000 or (r.v between 940 and 1060 and r.mx >= 1300) then 'no'
                   when r.v = r.mx then 'main' else 'sub' end)
       from (select x.role, x.v, greatest(coalesce(u.tank_mmr,1000), coalesce(u.deal_mmr,1000), coalesce(u.heal_mmr,1000)) mx
               from (values ('tank', coalesce(u.tank_mmr,1000)), ('deal', coalesce(u.deal_mmr,1000)), ('heal', coalesce(u.heal_mmr,1000))) x(role, v)) r)
 where u.ow_prefs is null
   and (coalesce(u.tank_mmr,1000) <> 1000 or coalesce(u.deal_mmr,1000) <> 1000 or coalesce(u.heal_mmr,1000) <> 1000);

-- 경기 기록에 '경기 전 A팀 예상 승률' 저장 (/밸런스통계 용)
alter table public.matches add column if not exists expected_a numeric(5,4);

create index if not exists user_profiles_left_at_idx
  on public.user_profiles (left_at) where left_at is not null;

-- 2) 내전 대기실/팀 결과 저장 (봇 재시작해도 버튼이 살아 있게)
create table if not exists public.bot_lobbies (
  message_id   text primary key,
  guild_id     text,
  channel_id   text not null,
  host_id      text not null,
  game         text not null,
  status       text not null default 'waiting' check (status in ('waiting', 'teams', 'done')),
  participants text[] not null default '{}',
  teams        jsonb,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
alter table public.bot_lobbies drop constraint if exists bot_lobbies_status_check;
alter table public.bot_lobbies add constraint bot_lobbies_status_check check (status in ('waiting', 'teams', 'done'));
alter table public.bot_lobbies enable row level security;  -- 정책 없음 = 봇(service_role)만 접근
revoke all on public.bot_lobbies from anon, authenticated;

-- 3) 채팅 집계 일괄 반영 + 멤버 추가/복귀 (count 0 이면 채팅 수는 그대로, 프로필만 생성/갱신)
--    p_rows: [{discord_id, username, avatar_url, team_id, count}]
create or replace function public.bot_track_chat_batch(p_rows jsonb)
returns integer
language plpgsql security definer set search_path = public
as $$
declare
  r jsonb;
  v_cnt int;
  v_team text;
  n int := 0;
begin
  for r in select * from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) loop
    continue when coalesce(r->>'discord_id', '') = '';
    v_cnt := greatest(coalesce((r->>'count')::int, 0), 0);
    v_team := nullif(r->>'team_id', '');
    if v_team is not null and not exists (select 1 from team_scores where team_id = v_team) then
      v_team := null;
    end if;

    insert into user_profiles as u
      (user_id, discord_id, username, avatar_url, team_id, chat_count, points, in_guild, left_at, updated_at)
    values
      (r->>'discord_id', r->>'discord_id', coalesce(nullif(r->>'username', ''), '익명'),
       coalesce(r->>'avatar_url', ''), v_team, v_cnt, v_cnt, true, null, now())
    on conflict (discord_id) do update set
      username   = excluded.username,
      avatar_url = excluded.avatar_url,
      team_id    = coalesce(excluded.team_id, u.team_id),
      chat_count = coalesce(u.chat_count, 0) + v_cnt,
      points     = coalesce(u.points, 0) + v_cnt,
      in_guild   = true,
      left_at    = null,
      updated_at = now();

    if v_team is not null and v_cnt > 0 then
      update team_scores
         set weekly_chats  = coalesce(weekly_chats, 0) + v_cnt,
             monthly_chats = coalesce(monthly_chats, 0) + v_cnt,
             total_points  = coalesce(total_points, 0) + v_cnt,
             updated_at    = now()
       where team_id = v_team;
    end if;
    n := n + 1;
  end loop;
  return n;
end $$;

-- 4) 퇴장 표시 (바로 지우지 않고 유예 기간 후 bot_purge_left 가 삭제)
create or replace function public.bot_mark_left(p_ids text[])
returns integer
language sql security definer set search_path = public
as $$
  with u as (
    update user_profiles
       set in_guild = false, left_at = coalesce(left_at, now()), updated_at = now()
     where discord_id = any(p_ids) and in_guild
    returning 1
  )
  select count(*)::int from u;
$$;

-- 5) 유예 기간이 지난 퇴장자 삭제. 삭제된 유저 목록 반환
create or replace function public.bot_purge_left(p_days int)
returns jsonb
language sql security definer set search_path = public
as $$
  with d as (
    delete from user_profiles
     where not in_guild and left_at is not null
       and left_at < now() - make_interval(days => greatest(p_days, 0))
    returning discord_id, username
  )
  select coalesce(jsonb_agg(jsonb_build_object('discord_id', discord_id, 'username', username)), '[]'::jsonb) from d;
$$;

-- 6) 경기 결과 기록 (승패 + 옵치 포지션 MMR + matches 저장을 한 트랜잭션으로)
--    p_entries: [{discord_id, name, side, role(null|tank|deal|heal), is_win, count, mmr_delta?}]
--      * mmr_delta 가 있으면 그 값만큼(예상 승률 기반, 봇이 계산), 없으면 ±p_step × count
--      * role 이 없으면 승패만 반영
--    반환: {match_id, entries:[...결과 포함], failed:[이름]}
drop function if exists public.bot_record_match(text, text, text, text, text, jsonb, int);
create or replace function public.bot_record_match(
  p_game text, p_source text, p_winner text, p_host text, p_by text,
  p_entries jsonb, p_step int default 20, p_expected numeric default null)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  e jsonb;
  v_row jsonb;
  v_did text;
  v_cnt int;
  v_win boolean;
  v_role text;
  v_w text; v_l text;
  v_mcol text; v_rw text; v_rl text;
  v_old int; v_new int;
  v_out jsonb := '[]'::jsonb;
  v_failed jsonb := '[]'::jsonb;
  v_res jsonb;
  v_match bigint;
begin
  if p_game = '발로란트' then v_w := 'val_wins'; v_l := 'val_losses';
  elsif p_game = '롤' then v_w := 'lol_wins'; v_l := 'lol_losses';
  elsif p_game = '옵치' then v_w := 'ow_wins'; v_l := 'ow_losses';
  else raise exception 'unknown game %', p_game;
  end if;

  for e in select * from jsonb_array_elements(coalesce(p_entries, '[]'::jsonb)) loop
    v_did := e->>'discord_id';
    v_cnt := greatest(coalesce((e->>'count')::int, 1), 1);
    v_win := coalesce((e->>'is_win')::boolean, false);
    v_role := case when p_game = '옵치' then nullif(e->>'role', '') end;

    select to_jsonb(u) into v_row from user_profiles u where discord_id = v_did for update;
    if v_row is null then
      v_failed := v_failed || to_jsonb(coalesce(e->>'name', v_did));
      continue;
    end if;

    v_res := e || jsonb_build_object('count', v_cnt, 'role', v_role, 'mmr_delta', 0);

    execute format('update user_profiles set %1$I = coalesce(%1$I, 0) + $1, updated_at = now() where discord_id = $2',
                   case when v_win then v_w else v_l end)
      using v_cnt, v_did;

    if v_role in ('tank', 'deal', 'heal') then
      v_mcol := v_role || '_mmr';
      v_rw := v_role || '_wins';
      v_rl := v_role || '_losses';
      v_old := coalesce((v_row->>v_mcol)::int, 1000);
      if e ? 'mmr_delta' and e->>'mmr_delta' is not null then
        v_new := greatest(0, v_old + (e->>'mmr_delta')::int);
      elsif v_win then
        v_new := v_old + p_step * v_cnt;
      else
        v_new := greatest(0, v_old - p_step * v_cnt);
      end if;

      execute format('update user_profiles set %1$I = $1, %2$I = coalesce(%2$I, 0) + $2 where discord_id = $3',
                     v_mcol, case when v_win then v_rw else v_rl end)
        using v_new, v_cnt, v_did;

      v_res := v_res || jsonb_build_object(
        'mmr_delta', v_new - v_old,
        'mmr', v_new,
        'role_wins', coalesce((v_row->>v_rw)::int, 0) + case when v_win then v_cnt else 0 end,
        'role_losses', coalesce((v_row->>v_rl)::int, 0) + case when v_win then 0 else v_cnt end);
    end if;

    v_res := v_res || jsonb_build_object(
      'wins', coalesce((v_row->>v_w)::int, 0) + case when v_win then v_cnt else 0 end,
      'losses', coalesce((v_row->>v_l)::int, 0) + case when v_win then 0 else v_cnt end);
    v_out := v_out || jsonb_build_array(v_res);
  end loop;

  if jsonb_array_length(v_out) > 0 then
    insert into matches (game, source, winner, host_id, recorded_by, entries, expected_a)
    values (
      p_game, p_source, p_winner, p_host, p_by,
      (select jsonb_agg(jsonb_build_object(
          'discord_id', x->>'discord_id', 'name', x->>'name', 'side', x->'side', 'role', x->'role',
          'is_win', (x->>'is_win')::boolean, 'count', (x->>'count')::int, 'mmr_delta', (x->>'mmr_delta')::int))
         from jsonb_array_elements(v_out) x),
      p_expected)
    returning id into v_match;
  end if;

  return jsonb_build_object('match_id', v_match, 'entries', v_out, 'failed', v_failed);
end $$;

-- 7) 경기 되돌리기 (한 트랜잭션). 이미 서버를 떠나 삭제된 유저는 건너뜀
create or replace function public.bot_undo_match(p_id bigint, p_by text)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  m record;
  e jsonb;
  v_w text; v_l text;
  v_cnt int; v_win boolean; v_role text; v_delta int;
  v_skipped jsonb := '[]'::jsonb;
  n int;
begin
  select * into m from matches where id = p_id for update;
  if not found then return jsonb_build_object('ok', false, 'error', 'not_found'); end if;
  if m.undone then return jsonb_build_object('ok', false, 'error', 'already_undone'); end if;

  if m.game = '발로란트' then v_w := 'val_wins'; v_l := 'val_losses';
  elsif m.game = '롤' then v_w := 'lol_wins'; v_l := 'lol_losses';
  else v_w := 'ow_wins'; v_l := 'ow_losses';
  end if;

  for e in select * from jsonb_array_elements(coalesce(m.entries, '[]'::jsonb)) loop
    continue when coalesce((e->>'reverted')::boolean, false);  -- 예전 방식으로 일부 되돌린 기록
    v_cnt := greatest(coalesce((e->>'count')::int, 1), 1);
    v_win := coalesce((e->>'is_win')::boolean, false);
    v_role := case when m.game = '옵치' then nullif(e->>'role', '') end;
    v_delta := coalesce((e->>'mmr_delta')::int, 0);

    execute format('update user_profiles set %1$I = greatest(0, coalesce(%1$I, 0) - $1), updated_at = now() where discord_id = $2',
                   case when v_win then v_w else v_l end)
      using v_cnt, e->>'discord_id';
    get diagnostics n = row_count;
    if n = 0 then
      v_skipped := v_skipped || to_jsonb(coalesce(e->>'name', e->>'discord_id'));
      continue;
    end if;

    if v_role in ('tank', 'deal', 'heal') then
      execute format('update user_profiles set %1$I = greatest(0, coalesce(%1$I, 1000) - $1), %2$I = greatest(0, coalesce(%2$I, 0) - $2) where discord_id = $3',
                     v_role || '_mmr', v_role || case when v_win then '_wins' else '_losses' end)
        using v_delta, v_cnt, e->>'discord_id';
    end if;
  end loop;

  update matches set undone = true, undone_at = now(), undone_by = p_by where id = p_id;
  return jsonb_build_object('ok', true, 'skipped', v_skipped, 'match', to_jsonb(m));
end $$;

-- 8) 권한: 봇 키(service_role)만 실행
revoke execute on function public.bot_track_chat_batch(jsonb) from public, anon, authenticated;
revoke execute on function public.bot_mark_left(text[]) from public, anon, authenticated;
revoke execute on function public.bot_purge_left(int) from public, anon, authenticated;
revoke execute on function public.bot_record_match(text, text, text, text, text, jsonb, int, numeric) from public, anon, authenticated;
revoke execute on function public.bot_undo_match(bigint, text) from public, anon, authenticated;
grant execute on function public.bot_track_chat_batch(jsonb) to service_role;
grant execute on function public.bot_mark_left(text[]) to service_role;
grant execute on function public.bot_purge_left(int) to service_role;
grant execute on function public.bot_record_match(text, text, text, text, text, jsonb, int, numeric) to service_role;
grant execute on function public.bot_undo_match(bigint, text) to service_role;
grant all on public.bot_lobbies to service_role;


-- 9) 롤 MMR 시스템 종료 → 롤 티어/점수/포지션/승패 초기화 (경기 이력 matches 는 그대로 보존)
update public.user_profiles
   set lol_tier = null, lol_mmr = 1000, lol_roles = '[]'::jsonb, lol_wins = 0, lol_losses = 0
 where lol_tier is not null or coalesce(lol_wins, 0) + coalesce(lol_losses, 0) > 0
    or coalesce(lol_mmr, 1000) <> 1000 or coalesce(lol_roles, '[]'::jsonb) <> '[]'::jsonb;
