#!/usr/bin/env python3
"""경찰 관련 뉴스를 구글 뉴스 RSS에서 수집해 docs/data/ 아래 JSON으로 저장한다.

동작 방식
---------
- 외부 패키지 없이 파이썬 표준 라이브러리만 사용한다.
- GitHub Actions에서 매시간(KST 05~23시) 실행되는 것을 전제로 한다.
- 같은 날짜 파일이 이미 있으면 기존 기사와 병합해 누적한다.
  (하루 동안 여러 번 실행돼도 아침에 수집된 기사가 사라지지 않는다)
- 여러 언론사가 보도한 같은 사건은 제목 유사도로 묶어서
  "언급 수(보도 언론사 수)"를 계산하고, 많이 보도된 순으로
  '오늘의 주요 이슈'를 뽑는다.
"""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"

# ── 설정 ────────────────────────────────────────────────────────────
# 주제(탭) 정의. queries는 구글 뉴스 검색어, sections는 제목 키워드로
# 나눌 섹션(위에서부터 먼저 매칭되는 곳에 들어감). 자유롭게 수정하면 된다.
TOPICS = [
    {
        "id": "police",
        "label": "경찰",
        "emoji": "🚔",
        "title": "경찰관련 기사 스크랩",
        "queries": ["경찰", "경찰청", "해양경찰", "자치경찰",
                     "경찰 인사", "경찰 수사"],
        "sections": [
            ("인사·조직", ["인사", "승진", "총경", "경무관", "치안감",
                         "치안정감", "경찰청장", "서장", "발령", "조직개편",
                         "정원", "임용"]),
            ("수사·사건", ["수사", "검거", "구속", "입건", "체포", "송치",
                         "압수수색", "혐의", "피의자", "영장", "마약",
                         "살인", "사기", "폭행"]),
            ("정책·행정", ["정책", "법안", "개정", "국회", "예산", "제도",
                         "훈령", "치안", "협약", "간담회", "대책", "조례"]),
        ],
        "etc_section": "사건사고·기타",
    },
    {
        "id": "realestate",
        "label": "부동산",
        "emoji": "🏠",
        "title": "부동산 기사 스크랩",
        "queries": ["부동산", "아파트값", "전세", "청약", "재건축",
                     "부동산 정책"],
        "sections": [
            ("정책·규제", ["정책", "규제", "대책", "세금", "종부세", "양도세",
                         "취득세", "대출", "LTV", "DSR", "국회", "법안",
                         "공급", "정부"]),
            ("시장·시세", ["집값", "아파트값", "매매", "전세", "월세", "시세",
                         "상승", "하락", "급등", "급락", "거래", "실거래",
                         "매물", "경매"]),
            ("분양·개발", ["분양", "청약", "재건축", "재개발", "입주", "착공",
                         "신도시", "GTX", "역세권", "개발", "정비사업"]),
        ],
        "etc_section": "기타",
    },
    {
        "id": "stock",
        "label": "주식",
        "emoji": "📈",
        "title": "주식·증시 기사 스크랩",
        "queries": ["증시", "코스피", "코스닥", "주가", "미국 증시",
                     "상장"],
        "sections": [
            ("시황", ["코스피", "코스닥", "증시", "나스닥", "다우", "S&P",
                    "마감", "개장", "장중", "급등", "급락", "환율", "외국인"]),
            ("종목·기업", ["실적", "영업이익", "주가", "목표주가", "상장",
                         "IPO", "공모", "배당", "자사주", "인수", "합병",
                         "수주"]),
            ("정책·금리", ["금리", "연준", "Fed", "한은", "한국은행",
                         "금융당국", "금감원", "공매도", "세제", "밸류업"]),
        ],
        "etc_section": "기타",
    },
]

MAX_PER_SECTION = 15   # 섹션당 최대 이슈(묶음) 수
TOP_ISSUE_COUNT = 5    # 주요 이슈로 뽑을 개수
TOP_ISSUE_MIN_SOURCES = 2  # 최소 몇 개 언론사가 보도해야 주요 이슈 후보인지
HOURS_WINDOW = 24      # 최근 몇 시간 이내 기사만 수집할지
# 제목 바이그램 유사도가 이 이상이면 같은 사건으로 묶음.
# 실측: 같은 사건의 다른 제목은 0.38~0.43, 무관한 사건은 0.0 수준이라
# 0.35면 같은 사건은 묶이고 오합병 위험은 낮다.
SIMILARITY_THRESHOLD = 0.35

USER_AGENT = "Mozilla/5.0 (compatible; polnews-scraper/2.0)"


# ── 수집 ────────────────────────────────────────────────────────────
def fetch(url: str, attempts: int = 3) -> bytes:
    """지수 백오프 재시도를 포함한 HTTP GET."""
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            last_error = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise last_error  # type: ignore[misc]


def fetch_articles(queries: list[str]) -> list[dict]:
    """구글 뉴스 RSS에서 검색어별 기사를 모아 반환한다.

    같은 링크(=같은 기사)는 하나만 남기되, 다른 언론사가 쓴 같은 사건
    기사는 나중에 묶어서 세야 하므로 여기서는 제거하지 않는다.
    """
    seen_links: set[str] = set()
    articles: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)

    for query in queries:
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

        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as e:
            print(f"[warn] '{query}' RSS 파싱 실패: {e}")
            continue

        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate")
            source = (item.findtext("source") or "").strip()

            if not title or not link or not pub:
                continue
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                continue
            if published < cutoff:
                continue

            # 구글 뉴스 제목은 항상 "제목 - 언론사" 형태라 마지막 " - " 뒤를
            # 떼어낸다. source 태그와 표기가 달라도(약칭 등) 확실히 제거되도록
            # endswith 비교 대신 rsplit을 쓴다.
            if " - " in title:
                head, _, tail = title.rpartition(" - ")
                if head and len(tail) <= 25:  # 뒤쪽이 언론사명일 때만
                    title = head.strip()

            if link in seen_links:
                continue
            seen_links.add(link)

            articles.append({
                "title": title,
                "link": link,
                "source": source,
                "published": published.astimezone(KST).isoformat(),
            })

    return articles


# ── 구글 뉴스 링크를 실제 기사 주소로 변환 ──────────────────────────
# 구글 뉴스 RSS의 링크는 600자가 넘는 리다이렉트 주소라서 메신저에
# 붙여넣으면 중간에 끊긴다. 구글의 내부 디코딩 API(batchexecute)를
# 이용해 짧은 원문 기사 URL로 바꿔 저장한다.
GOOGLE_LINK_RE = re.compile(
    r"^https://news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)"
)
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
RESOLVE_CHUNK = 20          # batchexecute 한 번에 물어볼 기사 수
RESOLVE_MAX_PER_RUN = 350   # 한 번 실행에서 해석할 최대 기사 수


def _decoding_params(art_id: str) -> tuple[str, str] | None:
    """기사 페이지에서 디코딩에 필요한 서명(sg)과 타임스탬프(ts)를 꺼낸다."""
    try:
        html = fetch(f"https://news.google.com/articles/{art_id}",
                     attempts=2).decode("utf-8", "replace")
    except Exception:
        return None
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not sg or not ts:
        return None
    return sg.group(1), ts.group(1)


def _batch_decode(entries: list[tuple[str, str, str]]) -> list[str | None]:
    """(art_id, ts, sg) 목록을 batchexecute로 한꺼번에 원문 URL로 변환한다."""
    reqs = [
        [
            "Fbv4je",
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
            'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,'
            f'null,0],"{art_id}",{ts},"{sg}"]',
        ]
        for art_id, ts, sg in entries
    ]
    payload = "f.req=" + urllib.parse.quote(json.dumps([reqs]))
    req = urllib.request.Request(
        BATCH_URL,
        data=payload.encode(),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")

    # 응답은 ")]}'" 프리앰블 뒤에 JSON 덩어리가 오는 형태
    chunk = text.split("\n\n")[1]
    results: list[str | None] = []
    for item in json.loads(chunk):
        if not (isinstance(item, list) and len(item) > 2
                and item[0] == "wrb.fr" and item[1] == "Fbv4je"):
            continue
        try:
            url = json.loads(item[2])[1]
            results.append(url if isinstance(url, str)
                           and url.startswith("http") else None)
        except (json.JSONDecodeError, IndexError, TypeError):
            results.append(None)
    return results


def resolve_google_links(articles: list[dict]) -> int:
    """구글 뉴스 링크를 원문 URL로 바꾼다(제자리 수정). 성공 건수를 반환.

    실패한 기사는 구글 링크를 그대로 둔다. 이미 변환된 기사(원문 URL)는
    건드리지 않으므로, 매시간 실행 시 새 기사만 추가 비용이 든다.
    """
    targets = []
    for art in articles:
        m = GOOGLE_LINK_RE.match(art["link"])
        if m:
            targets.append((art, m.group(1)))
        if len(targets) >= RESOLVE_MAX_PER_RUN:
            break
    if not targets:
        return 0

    resolved = 0
    for start in range(0, len(targets), RESOLVE_CHUNK):
        batch = targets[start:start + RESOLVE_CHUNK]
        entries, arts = [], []
        for art, art_id in batch:
            params = _decoding_params(art_id)
            time.sleep(0.05)  # 과도한 요청 방지
            if params:
                entries.append((art_id, params[1], params[0]))
                arts.append(art)
        if not entries:
            continue
        try:
            urls = _batch_decode(entries)
        except Exception as e:
            print(f"[warn] 링크 일괄 변환 실패({len(entries)}건): {e}")
            continue
        for art, url in zip(arts, urls):
            if url:
                art["link"] = url
                resolved += 1
    return resolved


# ── 같은 사건 묶기(클러스터링) ──────────────────────────────────────
def normalize(title: str) -> str:
    """유사도 비교용으로 제목에서 괄호 태그·공백·기호를 제거한다."""
    t = re.sub(r"\[[^\]]*\]", " ", title)      # [단독], [속보] 등
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t.lower()


def bigrams(s: str) -> frozenset[str]:
    return frozenset(s[i:i + 2] for i in range(len(s) - 1))


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """짧은 쪽 기준 포함률. 언론사마다 제목 길이가 달라도 잘 묶인다."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter < 4:  # 우연히 겹치는 짧은 조각은 무시
        return 0.0
    return inter / min(len(a), len(b))


def cluster_articles(articles: list[dict]) -> list[dict]:
    """제목이 비슷한 기사끼리 묶어 이슈(클러스터) 목록을 만든다.

    반환되는 각 이슈는 대표 기사 정보에 더해:
    - mention_count: 보도한 언론사 수(중복 제거)
    - sources: 언론사 목록
    """
    ordered = sorted(articles, key=lambda a: a["published"])  # 최초 보도 순
    clusters: list[dict] = []

    for art in ordered:
        grams = bigrams(normalize(art["title"]))
        best, best_score = None, 0.0
        for c in clusters:
            score = similarity(grams, c["_grams"])
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= SIMILARITY_THRESHOLD:
            best["_items"].append(art)
        else:
            # 대표 바이그램은 최초 보도 기사 기준으로 고정한다
            # (묶일 때마다 갱신하면 클러스터가 엉뚱하게 커지는 것을 방지)
            clusters.append({"_grams": grams, "_items": [art]})

    issues = []
    for c in clusters:
        items = c["_items"]
        rep = items[0]  # 최초 보도 기사를 대표로
        sources = list(dict.fromkeys(i["source"] for i in items if i["source"]))
        issues.append({
            "title": rep["title"],
            "link": rep["link"],
            "source": rep["source"],
            "published": rep["published"],
            "latest": items[-1]["published"],
            "mention_count": max(len(sources), 1),
            "sources": sources[:12],
        })
    return issues


# ── 섹션 분류 ───────────────────────────────────────────────────────
def categorize(
    issues: list[dict],
    section_defs: list[tuple[str, list[str]]],
    etc_section: str,
) -> tuple[list[dict], list[dict]]:
    """이슈를 (주요 이슈 TOP N, 섹션별 목록)으로 나눈다."""
    ranked = sorted(issues, key=lambda i: (i["mention_count"], i["latest"]),
                    reverse=True)
    top = [i for i in ranked if i["mention_count"] >= TOP_ISSUE_MIN_SOURCES]
    top = top[:TOP_ISSUE_COUNT]
    top_links = {i["link"] for i in top}

    sections: dict[str, list[dict]] = {name: [] for name, _ in section_defs}
    sections[etc_section] = []

    for issue in ranked:
        if issue["link"] in top_links:
            continue  # 주요 이슈에 이미 나온 것은 중복 표시하지 않음
        for name, keywords in section_defs:
            if any(kw in issue["title"] for kw in keywords):
                bucket = sections[name]
                break
        else:
            bucket = sections[etc_section]
        if len(bucket) < MAX_PER_SECTION:
            bucket.append(issue)

    section_list = [
        {"name": name, "articles": arts}
        for name, arts in sections.items()
        if arts
    ]
    return top, section_list


# ── 날씨 ────────────────────────────────────────────────────────────
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


# ── 병합·저장 ───────────────────────────────────────────────────────
def load_existing_raw(path: Path) -> list[dict]:
    """오늘 파일이 이미 있으면 원본 기사 목록을 꺼내 온다(누적 병합용)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("sample"):  # 샘플 데이터는 병합하지 않고 버린다
            return []
        return data.get("raw", [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] 기존 파일 읽기 실패, 새로 시작: {e}")
        return []


def merge_key(art: dict) -> tuple[str, str]:
    """병합용 중복 판정 키. 링크는 원문 변환 후 바뀔 수 있으므로
    제목+언론사 조합을 쓴다."""
    return (normalize(art.get("title", "")), art.get("source", ""))


def merge_articles(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """중복을 제거하며 기존 + 신규 기사를 합친다.

    기존 항목을 우선하므로, 이미 원문 URL로 변환된 링크가
    새로 수집된 구글 링크로 되돌아가지 않는다.
    """
    merged: dict[tuple[str, str], dict] = {
        merge_key(a): a for a in existing if a.get("link")
    }
    for a in fresh:
        merged.setdefault(merge_key(a), a)
    return list(merged.values())


def migrate_legacy_police_data() -> None:
    """탭 도입 이전의 docs/data/*.json 파일을 police/ 폴더로 옮긴다.

    한 번 옮기고 나면 할 일이 없어지므로 매 실행마다 불러도 안전하다.
    """
    police_dir = DATA_DIR / "police"
    moved = 0
    for old in DATA_DIR.glob("????-??-??.json"):
        new = police_dir / old.name
        if not new.exists():
            police_dir.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            moved += 1
        else:
            old.unlink()
    legacy_index = DATA_DIR / "index.json"
    if legacy_index.exists():
        legacy_index.unlink()
    if moved:
        print(f"기존 경찰 데이터 {moved}개 파일을 police/ 로 이동")


def run_topic(topic: dict, today: str, now_iso: str,
              weather: dict | None) -> None:
    """주제 하나를 수집·정리해 docs/data/<id>/<날짜>.json 으로 저장한다."""
    topic_dir = DATA_DIR / topic["id"]
    out_path = topic_dir / f"{today}.json"
    print(f"\n=== {topic['emoji']} {topic['label']} ===")

    fresh = fetch_articles(topic["queries"])
    existing = load_existing_raw(out_path)
    raw = merge_articles(existing, fresh)
    print(f"신규 {len(fresh)}건 + 기존 {len(existing)}건 → 병합 {len(raw)}건")

    resolved = resolve_google_links(raw)
    if resolved:
        print(f"구글 뉴스 링크 {resolved}건을 원문 URL로 변환")

    issues = cluster_articles(raw)
    top_issues, sections = categorize(
        issues, topic["sections"], topic["etc_section"]
    )

    briefing = {
        "date": today,
        "generated_at": now_iso,
        "topic": {k: topic[k] for k in ("id", "label", "emoji", "title")},
        "weather": weather,
        "top_issues": top_issues,
        "sections": sections,
        "total_articles": len(raw),
        "total_issues": len(issues),
        "raw": sorted(raw, key=lambda a: a["published"], reverse=True),
    }

    topic_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{out_path} 저장 (이슈 {len(issues)}개, 주요 이슈 {len(top_issues)}개)")

    dates = sorted(
        (p.stem for p in topic_dir.glob("????-??-??.json")),
        reverse=True,
    )
    (topic_dir / "index.json").write_text(
        json.dumps({"dates": dates}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    migrate_legacy_police_data()

    # 날씨는 하루 한 번만 API를 부르고 주제끼리 공유한다
    weather = None
    first_topic_file = DATA_DIR / TOPICS[0]["id"] / f"{today}.json"
    if first_topic_file.exists():
        try:
            prev = json.loads(first_topic_file.read_text(encoding="utf-8"))
            if not prev.get("sample"):
                weather = prev.get("weather")
        except (json.JSONDecodeError, OSError):
            pass
    if weather is None:
        weather = fetch_weather()

    for topic in TOPICS:
        try:
            run_topic(topic, today, now.isoformat(), weather)
        except Exception as e:  # 한 주제가 실패해도 나머지는 저장한다
            print(f"[warn] '{topic['label']}' 처리 실패: {e}")

    # 웹앱이 탭 목록을 그릴 수 있도록 주제 메타데이터를 내보낸다
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "topics.json").write_text(
        json.dumps(
            {"topics": [
                {k: t[k] for k in ("id", "label", "emoji", "title")}
                for t in TOPICS
            ]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
