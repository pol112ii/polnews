# 🚔 경찰 뉴스 브리핑 (polnews)

경찰 관련 뉴스 기사를 **매일 새벽 5시(한국시간)** 에 자동으로 수집해서
웹 페이지로 보여주는 프로젝트입니다. 서버 비용 없이 GitHub 만으로 돌아갑니다.

## 어떻게 동작하나요?

```
매일 새벽 5시 (GitHub Actions cron)
  → scraper/scrape.py 실행
      · 구글 뉴스 RSS에서 경찰 관련 키워드로 최근 24시간 기사 수집
      · 서울 날씨 3일 예보 수집 (open-meteo, 무료)
      · docs/data/YYYY-MM-DD.json 으로 저장 후 자동 커밋
  → GitHub Pages가 docs/ 폴더를 웹사이트로 제공
```

- **스크래퍼**: `scraper/scrape.py` — 파이썬 표준 라이브러리만 사용 (설치할 패키지 없음)
- **자동 실행**: `.github/workflows/scrape.yml` — 매일 20:00 UTC(= KST 05:00) cron
- **웹앱**: `docs/index.html` — 날짜별 브리핑 조회, 텍스트 복사(메신저 공유용) 기능

## 처음 설정하기 (한 번만)

1. **이 브랜치를 main에 머지**합니다.
   (GitHub Actions의 예약 실행(cron)은 **기본 브랜치에서만** 동작합니다)
2. 저장소 **Settings → Pages** 에서
   - Source: `Deploy from a branch`
   - Branch: `main`, 폴더: `/docs` 선택 → Save
3. **Actions 탭 → "아침 뉴스 스크랩" → Run workflow** 를 눌러 한 번 수동 실행합니다.
   (샘플 데이터가 실제 기사로 교체됩니다)
4. 몇 분 뒤 `https://pol112ii.github.io/polnews/` 에서 확인하세요.

이후에는 매일 새벽 5시에 자동으로 새 브리핑이 올라옵니다.

## 키워드/섹션 바꾸기

`scraper/scrape.py` 상단의 두 목록만 고치면 됩니다.

- `QUERIES`: 구글 뉴스에서 검색할 검색어 목록
- `SECTIONS`: 제목 키워드에 따라 기사를 나눌 섹션 정의

## 알아둘 점

- GitHub Actions 예약 실행은 정확히 5시 정각이 아니라 **수 분~수십 분 늦게**
  시작될 수 있습니다 (GitHub 공식 안내 사항). 더 정확한 시각이 필요하면
  cron을 `50 19 * * *` (04:50 KST)처럼 앞당겨 두는 방법이 있습니다.
- 구글 뉴스 RSS 링크는 구글의 리다이렉트 주소라 클릭하면 원문 기사로 이동합니다.
- 공개 저장소에서는 Actions와 Pages 모두 무료입니다.
