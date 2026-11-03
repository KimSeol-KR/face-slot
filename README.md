# FACE.SLOT

사진을 올리고 버튼을 누르면 슬롯머신 릴이 돌아가며, 얼굴만 가상의 인물로 바뀝니다.

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env          # GEMINI_API_KEY 입력 (없으면 모의 모드)
export $(grep -v '^#' .env | xargs)
uvicorn main:app --reload
```

http://localhost:8000 접속. 카메라는 HTTPS 또는 localhost에서만 동작합니다.

## Railway 배포

1. 이 폴더를 GitHub에 올리고 Railway에서 연결
2. Variables에 `GEMINI_API_KEY` 추가 (`MAX_DAILY_SPINS`로 일일 비용 상한 조절)
3. Procfile이 시작 명령을 담당

## 구조

- `main.py`: 업로드 검증 → 랜덤 속성 5개 추출 → Gemini 이미지 편집 → 결과 반환
- `static/index.html`: 촬영/첨부, 동의 체크, 릴 연출, 전후 비교, AI 표시가 들어간 저장

## 운영 전 체크

- 랜덤 풀은 `main.py`의 `REELS`에서 수정 (라벨은 화면, prompt는 모델 전달용)
- 모델명은 `GEMINI_IMAGE_MODEL`로 교체 가능. 배포 전에 실제 키로 결과 품질을 한 번 확인할 것
- IP 레이트리밋은 메모리 기반이라 인스턴스가 여러 개면 따로 계산됨
