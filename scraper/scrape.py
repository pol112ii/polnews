#!/usr/bin/env python3
"""경찰 관련 뉴스를 구글 뉴스 RSS에서 수집해 docs/data/ 아래 JSON으로 저장한다.

외부 패키지 없이 표준 라이브러리만 사용한다.
매일 새벽 GitHub Actions 에서 실행되는 것을 전제로 하며,
최근 24시간 이내에 발행된 기사만 모은다.
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"

# 수집에 사용할 검색어. 필요하면 자유롭게 추가/삭제하면 된다.
QUERIES = [
    "경찰",
    "경찰청",
    "해양경찰",
    "자치경찰",
    "경찰 인사",
]

# 제목 키워드로 섹션을 나눈다. 위에서부터 먼저 매칭되는 섹션에 들어간다.
SECTIONS = [
    ("인사·조직", ["인사", "승진", "총경", "경무관", "치안감", "치안정감",
                 "경찰청장", "서장", "발령", "조직개편", "정원"]),
    ("수사·사건", ["수사", "검거", "구속", "입건", "체포", "송치", "압수수색",
                 "혐의", "피의자", "검찰", "영장", "마약", "살인", "사기"]),
    ("정책·행정", ["정책", "법안", "개정", "국회", "예산", "제도", "훈령",
                 "치안", "협약", "간담회", "대책"]),
]
ETC_SECTION = "사건사고·기타"

MAX_PER_SECTION = 15
HOURS_WINDOW = 24

USER_AGENT = "Mozilla/5.0 (compatible; polnews-scraper/1.0)"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_articles() -> list[dict]:
    """구글 뉴스 RSS에서 검색어별 기사를 모아 중복을 제거해 반환한다."""
    seen_titles: set[str] = set()
    articles: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)

    for query in QUERIES:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=ko&gl=KR&ceid=KR:ko"
        )
        try:
            xml = fetch(url)
        except Exception as e:  # 검색어 하나가 실패해도 나머지는 계속한다
            print(f"[warn] '{query}' 수집 실패: {e}")
            continue

        root = ElementTree.fromstring(xml)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate")
            source = item.findtext("source") or ""

            if not title or not link or not pub:
                continue
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                continue
            if published < cutoff:
                continue

            # 구글 뉴스 제목은 "제목 - 언론사" 형태라 언론사를 떼어낸다
            if source and title.endswith(f"- {source}"):
                title = title[: -(len(source) + 2)].strip()

            key = normalize(title)
            if key in seen_titles:
                continue
            seen_titles.add(key)

            articles.append({
                "title": title,
                "link": link,
                "source": source.strip(),
                "published": published.astimezone(KST).isoformat(),
            })

    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles


def normalize(title: str) -> str:
    """중복 판정용으로 제목에서 공백/특수문자를 제거한다."""
    return re.sub(r"[\s\W]+", "", title)


def categorize(articles: list[dict]) -> list[dict]:
    sections = {name: [] for name, _ in SECTIONS}
    sections[ETC_SECTION] = []

    for art in articles:
        placed = False
        for name, keywords in SECTIONS:
            if any(kw in art["title"] for kw in keywords):
                if len(sections[name]) < MAX_PER_SECTION:
                    sections[name].append(art)
                placed = True
                break
        if not placed and len(sections[ETC_SECTION]) < MAX_PER_SECTION:
            sections[ETC_SECTION].append(art)

    return [
        {"name": name, "articles": arts}
        for name, arts in sections.items()
        if arts
    ]


WEATHER_EMOJI = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌦️",
    61: "🌧️", 63: "🌧️", 65: "🌧️",
    66: "🌧️", 67: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "❄️", 77: "❄️",
    80: "🌦️", 81: "🌧️", 82: "⛈️",
    85: "🌨️", 86: "❄️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


def fetch_weather() -> dict | None:
    """서울 기준 3일 예보를 open-meteo(무료, 키 불필요)에서 가져온다."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=37.5665&longitude=126.978"
        "&daily=temperature_2m_min,temperature_2m_max,weather_code"
        "&timezone=Asia%2FSeoul&forecast_days=3"
    )
    try:
        data = json.loads(fetch(url))
    except Exception as e:
        print(f"[warn] 날씨 수집 실패: {e}")
        return None

    daily = data.get("daily", {})
    days = []
    for i, date_str in enumerate(daily.get("time", [])):
        days.append({
            "date": date_str,
            "min": round(daily["temperature_2m_min"][i]),
            "max": round(daily["temperature_2m_max"][i]),
            "emoji": WEATHER_EMOJI.get(daily["weather_code"][i], "🌡️"),
        })
    return {"location": "서울", "days": days} if days else None


def main() -> None:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    articles = fetch_articles()
    briefing = {
        "date": today,
        "generated_at": now.isoformat(),
        "weather": fetch_weather(),
        "sections": categorize(articles),
        "total": len(articles),
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{today}.json"
    out.write_text(json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{out} 저장 (기사 {len(articles)}건)")

    # 날짜 목록 인덱스 갱신
    dates = sorted(
        (p.stem for p in DATA_DIR.glob("????-??-??.json")),
        reverse=True,
    )
    (DATA_DIR / "index.json").write_text(
        json.dumps({"dates": dates}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"index.json 갱신 (총 {len(dates)}일)")


if __name__ == "__main__":
    main()
