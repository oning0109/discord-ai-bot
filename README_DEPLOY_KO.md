# 디스호스트 배포 메모

## 실행
- 시작 파일: `main.py`
- 의존성 파일: `requirements.txt`
- Python 패키지는 `requirements.txt` 기준으로 설치하세요.

## 필수 환경변수
- `DISCORD_TOKEN`: Discord Developer Portal에서 새로 발급한 봇 토큰
- `SUPABASE_URL`: 기존 Supabase 프로젝트 URL
- `SUPABASE_KEY`: 해당 프로젝트의 anon/publishable 키 (service_role 키는 사용하지 마세요)
- `DB_PATH`: 기본 `mmr_data.db`
- `DEFAULT_PREFIX`: 기본 `!`

`.env` 파일은 저장소에 올리지 마세요. 기존 토큰은 이미 노출된 것으로 보고 재발급하세요.
