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

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
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
                     "경찰 인사", "경찰 수사",
                     # 경찰이 직접 언급되지 않아도 참고할 사건·사고·치안 전반
                     "참사", "교통사고", "화재 사고", "실종",
                     "강력범죄", "음주운전", "보이스피싱"],
        "sections": [
            ("인사·조직", ["인사", "승진", "총경", "경무관", "치안감",
                         "치안정감", "경찰청장", "서장", "발령", "조직개편",
                         "정원", "임용"]),
            ("수사·사건", ["수사", "검거", "구속", "입건", "체포", "송치",
                         "압수수색", "혐의", "피의자", "영장", "마약",
                         "살인", "사기", "폭행", "스토킹", "보이스피싱",
                         "음주운전", "강력범죄"]),
            ("사건사고·안전", ["참사", "사고", "화재", "실종", "사망", "추락",
                           "붕괴", "익사", "구조", "안전"]),
            ("정책·행정", ["정책", "법안", "개정", "국회", "예산", "제도",
                         "훈령", "치안", "협약", "간담회", "대책", "조례"]),
        ],
        "etc_section": "기타",
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
        # 해외 뉴스: 구글 뉴스 영어판 검색 + 유명 매체 RSS 직접 구독.
        # 제목은 구글 번역(무료 웹 엔드포인트)으로 한국어로 바꿔 보여준다.
        "intl": {
            "title": "해외 주식·증시 스크랩",
            "queries": ["stock market", "Federal Reserve", "Wall Street",
                         "Nasdaq", "S&P 500 earnings"],
            "feeds": [
                ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
                ("Investing.com", "https://www.investing.com/rss/news.rss"),
                ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
                ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
                ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
            ],
            # 해외 기사는 영어 원문 제목(title_en) 기준으로 분류한다
            "sections": [
                ("시황", ["market", "stocks", "Dow", "Nasdaq", "S&P",
                        "futures", "Wall Street", "rally", "selloff",
                        "close", "trading"]),
                ("연준·경제", ["Fed", "Federal Reserve", "rate", "inflation",
                             "economy", "GDP", "jobs", "tariff", "Treasury",
                             "recession", "dollar"]),
                ("종목·기업", ["earnings", "shares", "stock price", "IPO",
                             "dividend", "merger", "acquisition", "CEO",
                             "revenue", "profit", "guidance"]),
            ],
            "etc_section": "기타",
        },
    },
]

RETENTION_DAYS = 14    # 날짜별 브리핑 보존 기간 (지나면 자동 삭제)
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
def fetch(url: str, attempts: int = 3, timeout: int = 30) -> bytes:
    """지수 백오프 재시도를 포함한 HTTP GET."""
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last_error = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise last_error  # type: ignore[misc]


def strip_source_suffix(title: str, source: str) -> str:
    """제목 끝에 남은 '- 언론사명'을 제거한다(여러 번 붙어 있어도 안전).

    언론사명과 정확히 일치할 때만 떼므로 이미 깨끗한 제목에 다시 적용해도
    바뀌지 않는다(멱등). 저장된 기존 데이터를 정리할 때도 그대로 쓴다.
    """
    while source:
        stripped = None
        for sep in (" - ", " – ", "- "):
            if title.endswith(sep + source):
                stripped = title[: -(len(sep) + len(source))].rstrip()
                break
        if stripped is None or not stripped:
            break
        title = stripped
    return title


# ── 네이버 뉴스 검색 API (국내 수집 기본, 키 없으면 구글로 폴백) ──────
# GitHub Secrets(NAVER_CLIENT_ID / NAVER_CLIENT_SECRET)에 키가 있으면
# 네이버로 수집한다. 네이버는 기사 요약(description)과 네이버 뉴스 링크를
# 함께 주므로 구글 링크 변환·별도 요약 수집이 필요 없어 더 빠르고 안정적이다.
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "").strip()
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
NAVER_DISPLAY = 100   # 검색어당 최대 100건(네이버 상한)

# 네이버는 언론사명을 안 주므로 원문 도메인으로 유추한다
DOMAIN_TO_SOURCE = {
    "chosun.com": "조선일보", "joongang.co.kr": "중앙일보", "donga.com": "동아일보",
    "hani.co.kr": "한겨레", "khan.co.kr": "경향신문", "yna.co.kr": "연합뉴스",
    "yonhapnewstv.co.kr": "연합뉴스TV", "newsis.com": "뉴시스", "news1.kr": "뉴스1",
    "kbs.co.kr": "KBS", "imbc.com": "MBC", "sbs.co.kr": "SBS", "jtbc.co.kr": "JTBC",
    "ytn.co.kr": "YTN", "mbn.co.kr": "MBN", "channela.co.kr": "채널A",
    "mt.co.kr": "머니투데이", "hankyung.com": "한국경제", "mk.co.kr": "매일경제",
    "sedaily.com": "서울경제", "fnnews.com": "파이낸셜뉴스", "edaily.co.kr": "이데일리",
    "asiae.co.kr": "아시아경제", "seoul.co.kr": "서울신문", "kmib.co.kr": "국민일보",
    "segye.com": "세계일보", "munhwa.com": "문화일보", "hankookilbo.com": "한국일보",
    "kukinews.com": "쿠키뉴스", "nocutnews.co.kr": "노컷뉴스", "ohmynews.com": "오마이뉴스",
    "newspim.com": "뉴스핌", "dt.co.kr": "디지털타임스", "etnews.com": "전자신문",
    "inews24.com": "아이뉴스24", "newdaily.co.kr": "뉴데일리", "biz.chosun.com": "조선비즈",
    "heraldcorp.com": "헤럴드경제", "moneys.co.kr": "머니S", "pressian.com": "프레시안",
    "kyeongin.com": "경인일보", "kwnews.co.kr": "강원일보", "joongdo.co.kr": "중도일보",
    "wowtv.co.kr": "한국경제TV", "zdnet.co.kr": "지디넷코리아", "wikitree.co.kr": "위키트리",
}


def naver_enabled() -> bool:
    return bool(NAVER_ID and NAVER_SECRET)


# 더 구체적인(긴) 도메인을 먼저 매칭하도록 정렬 (biz.chosun.com > chosun.com)
_DOMAINS_BY_LEN = sorted(DOMAIN_TO_SOURCE, key=len, reverse=True)


def source_from_link(link: str) -> str:
    host = urllib.parse.urlparse(link).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if "naver.com" in host:      # 네이버 링크는 원문 언론사를 알 수 없음
        return ""
    for dom in _DOMAINS_BY_LEN:
        if host == dom or host.endswith("." + dom):
            return DOMAIN_TO_SOURCE[dom]
    parts = host.split(".")
    return parts[0] if parts and parts[0] else host


def clean_naver_text(s: str) -> str:
    """네이버 응답의 <b> 태그·HTML 엔티티를 제거해 깨끗한 문자열로."""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def fetch_articles_naver(queries: list[str]) -> list[dict]:
    """네이버 뉴스 검색 API로 기사를 모아 반환한다.

    인증 실패(잘못된 키)면 예외를 올려 상위에서 구글로 폴백하게 한다.
    개별 검색어의 일시 오류(타임아웃 등)는 건너뛰고 계속 진행한다.
    """
    seen_links: set[str] = set()
    articles: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)

    for query in queries:
        url = (NAVER_NEWS_URL + "?query=" + urllib.parse.quote(query)
               + f"&display={NAVER_DISPLAY}&sort=date")
        req = urllib.request.Request(url, headers={
            "X-Naver-Client-Id": NAVER_ID,
            "X-Naver-Client-Secret": NAVER_SECRET,
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):     # 키가 틀림 → 전체 폴백
                raise
            print(f"[warn] 네이버 '{query}' 실패(HTTP {e.code}), 건너뜀")
            continue
        except Exception as e:
            print(f"[warn] 네이버 '{query}' 실패({e}), 건너뜀")
            continue

        for it in data.get("items", []):
            title = clean_naver_text(it.get("title", ""))
            orig = (it.get("originallink") or "").strip()
            link = (it.get("link") or orig).strip()   # 네이버 뉴스 링크 우선
            pub = it.get("pubDate")
            desc = clean_naver_text(it.get("description", ""))
            if not title or not link or not pub:
                continue
            try:
                published = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                continue
            if published < cutoff or link in seen_links:
                continue
            seen_links.add(link)

            source = source_from_link(orig or link)
            title = strip_source_suffix(title, source)
            art = {
                "title": title,
                "link": link,
                "source": source,
                "published": published.astimezone(KST).isoformat(),
            }
            if desc and len(desc) >= 25:   # 네이버가 준 요약을 그대로 사용
                art["summary"] = (desc[:SUMMARY_MAX_LEN].rstrip() + "…"
                                  if len(desc) > SUMMARY_MAX_LEN else desc)
            articles.append(art)

    return articles


def fetch_articles(queries: list[str], lang: str = "ko") -> list[dict]:
    """국내(ko)는 네이버 우선·구글 폴백, 해외(en)는 구글을 쓴다."""
    if lang == "ko" and naver_enabled():
        try:
            arts = fetch_articles_naver(queries)
            if arts:
                return arts
            print("[warn] 네이버 결과 0건 → 구글로 폴백")
        except Exception as e:
            print(f"[warn] 네이버 수집 실패({e}) → 구글로 폴백")
    return fetch_articles_google(queries, lang)


def fetch_articles_google(queries: list[str], lang: str = "ko") -> list[dict]:
    """구글 뉴스 RSS에서 검색어별 기사를 모아 반환한다.

    같은 링크(=같은 기사)는 하나만 남기되, 다른 언론사가 쓴 같은 사건
    기사는 나중에 묶어서 세야 하므로 여기서는 제거하지 않는다.
    """
    locale = ("hl=ko&gl=KR&ceid=KR:ko" if lang == "ko"
              else "hl=en-US&gl=US&ceid=US:en")
    seen_links: set[str] = set()
    articles: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)

    for query in queries:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&" + locale
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
            # 일부 언론사(머니투데이 등)는 원문 제목 자체에도 "- 언론사"를
            # 붙여서 이중으로 남는 경우가 있다 → 남아 있으면 마저 제거
            title = strip_source_suffix(title, source)

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


def fetch_feed_articles(feeds: list[tuple[str, str]]) -> list[dict]:
    """언론사 RSS 피드를 직접 구독해 기사를 모아 반환한다.

    피드가 봇을 차단하거나 형식이 달라도 개별 실패로 그치고
    나머지 피드는 계속 수집한다.
    """
    seen_links: set[str] = set()
    articles: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)

    for source_name, url in feeds:
        try:
            xml = fetch(url, attempts=2, timeout=20)
            root = ElementTree.fromstring(xml)
        except Exception as e:
            print(f"[warn] 피드 '{source_name}' 수집 실패: {e}")
            continue

        count = 0
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate")
            if not title or not link:
                continue
            # 피드마다 날짜 형식이 제각각이라(RFC822/ISO/없음) 최대한 읽고,
            # 못 읽으면 버리지 말고 수집 시각으로 간주한다
            # (직접 피드는 어차피 최신 기사만 담고 있고, 병합 단계에서
            #  중복이 걸러지므로 안전하다)
            published = None
            if pub:
                for parse in (parsedate_to_datetime, datetime.fromisoformat):
                    try:
                        published = parse(pub.strip())
                        break
                    except (TypeError, ValueError):
                        continue
            if published is None:
                published = datetime.now(timezone.utc)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published < cutoff or link in seen_links:
                continue
            seen_links.add(link)
            articles.append({
                "title": title,
                "link": link,
                "source": source_name,
                "published": published.astimezone(KST).isoformat(),
            })
            count += 1
        print(f"피드 '{source_name}': {count}건")

    return articles


# ── 해외 기사 제목 번역 ─────────────────────────────────────────────
# 구글 번역의 무료 웹 엔드포인트(키·계정 불필요)로 제목만 번역한다.
# 비공식 엔드포인트라 언젠가 막힐 수 있으므로, 실패하면 영어 원문을
# 그대로 두어 브리핑 자체는 항상 정상 동작하게 한다.
TRANSLATE_URL = ("https://translate.googleapis.com/translate_a/single"
                 "?client=gtx&sl=en&tl=ko&dt=t&q=")
TRANSLATE_MAX_PER_RUN = 300


def _translate_one(article: dict) -> None:
    # 요청이 몰리면 구글이 일부를 거절하므로 재시도(백오프 포함)를 둔다
    try:
        data = json.loads(fetch(
            TRANSLATE_URL + urllib.parse.quote(article["title"]),
            attempts=3, timeout=15,
        ))
        ko = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
        if ko:
            article["title_en"] = article["title"]
            article["title"] = ko
    except Exception:
        pass  # 원문 제목 유지 (다음 실행에서 자동 재시도)


def translate_titles(articles: list[dict]) -> int:
    """아직 번역되지 않은 기사 제목을 한국어로 바꾼다. 성공 건수 반환.

    한 번 번역된 기사는 title_en 이 생겨 다음 실행에서 건너뛴다.
    """
    todo = [a for a in articles if "title_en" not in a][:TRANSLATE_MAX_PER_RUN]
    if not todo:
        return 0
    # 번역 엔드포인트는 링크 변환보다 요청 제한이 빡빡해 스레드를 줄인다
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_translate_one, todo))
    return sum(1 for a in todo if "title_en" in a)


# ── 구글 뉴스 링크를 실제 기사 주소로 변환 ──────────────────────────
# 구글 뉴스 RSS의 링크는 600자가 넘는 리다이렉트 주소라서 메신저에
# 붙여넣으면 중간에 끊긴다. 구글의 내부 디코딩 API(batchexecute)를
# 이용해 짧은 원문 기사 URL로 바꿔 저장한다.
GOOGLE_LINK_RE = re.compile(
    r"^https://news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)"
)
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
RESOLVE_CHUNK = 20          # batchexecute 한 번에 물어볼 기사 수
RESOLVE_MAX_PER_RUN = 200   # 주제당 한 번 실행에서 해석할 최대 기사 수
RESOLVE_WORKERS = 8         # 서명 조회 병렬 스레드 수


def _decoding_params(art_id: str) -> tuple[str, str] | None:
    """기사 페이지에서 디코딩에 필요한 서명(sg)과 타임스탬프(ts)를 꺼낸다."""
    try:
        html = fetch(f"https://news.google.com/articles/{art_id}",
                     attempts=1, timeout=15).decode("utf-8", "replace")
    except Exception:
        return None
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not sg or not ts:
        return None
    return sg.group(1), ts.group(1)


def _batch_decode(entries: list[tuple[str, str, str]]) -> dict[int, str]:
    """(art_id, ts, sg) 목록을 batchexecute로 원문 URL로 변환한다.

    각 요청에 인덱스 id를 부여하고, 응답에 에코된 인덱스(item[6])로
    URL을 되짚어 {요청 인덱스: url} 딕셔너리를 만든다.
    이렇게 하면 구글이 응답을 뒤섞거나 일부를 빼먹어도 제목과 링크가
    어긋나지 않는다. (인덱스 없이 순서로 매핑하던 버그 수정)
    """
    reqs = [
        [
            "Fbv4je",
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
            'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,'
            f'null,0],"{art_id}",{ts},"{sg}"]',
            None,
            str(i),  # 요청 인덱스 — 응답에서 그대로 되돌아온다
        ]
        for i, (art_id, ts, sg) in enumerate(entries)
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
    results: dict[int, str] = {}
    for item in json.loads(chunk):
        if not (isinstance(item, list) and len(item) > 6
                and item[0] == "wrb.fr" and item[1] == "Fbv4je"):
            continue
        try:
            idx = int(item[6])          # 요청 때 부여한 인덱스
            url = json.loads(item[2])[1]
            if isinstance(url, str) and url.startswith("http"):
                results[idx] = url
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            continue
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
    no_params = 0
    for start in range(0, len(targets), RESOLVE_CHUNK):
        batch = targets[start:start + RESOLVE_CHUNK]
        # 서명 조회는 기사당 요청 1번이 필요해 병렬로 처리한다
        with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
            params_list = list(pool.map(
                lambda t: _decoding_params(t[1]), batch
            ))
        entries, arts = [], []
        for (art, art_id), params in zip(batch, params_list):
            if params:
                entries.append((art_id, params[1], params[0]))
                arts.append(art)
            else:
                no_params += 1
        if not entries:
            continue
        try:
            url_by_idx = _batch_decode(entries)
        except Exception as e:
            print(f"[warn] 링크 일괄 변환 실패({len(entries)}건): {e}")
            continue
        # 인덱스로 정확히 매핑 — 응답이 누락/재정렬돼도 안전
        for i, art in enumerate(arts):
            url = url_by_idx.get(i)
            if url:
                art["link"] = url
                resolved += 1
    # 어디서 실패하는지 보이도록 요약을 남긴다 (운영 진단용)
    if resolved < len(targets):
        print(f"[info] 변환 대상 {len(targets)}건 중 성공 {resolved}건 "
              f"(서명 조회 실패 {no_params}건)")
    return resolved


# ── 기사 한두 줄 요약 ───────────────────────────────────────────────
# AI가 아니라 각 기사 페이지에 언론사가 심어둔 메타 요약(og:description,
# 보통 기사 첫 한두 문장)을 가져온다. 비용·키가 전혀 들지 않는다.
SUMMARY_MAX_PER_RUN = 60    # 한 번 실행에서 새로 요약을 가져올 최대 기사 수
SUMMARY_MAX_LEN = 150

_META_DESC_RES = [
    re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description', re.I),
    re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', re.I),
]


# 기사 내용이 아니라 사이트 소개 문구를 og:description에 넣는 언론사 대응
_SLOGAN_HINTS = ["뉴스통신사", "종합 일간지", "인터넷 신문", "뉴스 포털",
                  "국민의 알권리", "신뢰받는", "빠르고 정확한", "대표 언론"]
# "(서울=연합뉴스) 홍길동 기자 =" 류의 바이라인 접두부
_BYLINE_RE = re.compile(r"^[\[(][^\])]{2,20}[\])]\s*(?:[\w가-힣]{2,5}\s*기자\s*=?\s*)?")


def extract_meta_summary(page_html: str) -> str | None:
    """기사 HTML에서 메타 요약을 꺼내 다듬는다."""
    for pattern in _META_DESC_RES:
        m = pattern.search(page_html)
        if not m:
            continue
        text = html.unescape(m.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        text = _BYLINE_RE.sub("", text).strip()
        if len(text) < 25:   # 슬로건 같은 짧은 문구는 배제
            continue
        if any(h in text[:40] for h in _SLOGAN_HINTS):
            continue          # 기사 요약이 아니라 사이트 소개 문구
        if len(text) > SUMMARY_MAX_LEN:
            text = text[:SUMMARY_MAX_LEN].rstrip() + "…"
        return text
    return None


def _fetch_summary(link: str) -> str | None:
    try:
        page = fetch(link, attempts=1, timeout=10).decode("utf-8", "replace")
    except Exception:
        return None
    return extract_meta_summary(page)


def add_summaries(issues: list[dict], raw_by_link: dict[str, dict]) -> int:
    """화면에 보이는 이슈에 요약을 붙인다. 성공 건수 반환.

    원본 기사(raw)에도 저장해서 다음 실행부터는 다시 가져오지 않는다.
    구글 링크(미변환)는 요약 페이지가 아니므로 건너뛴다.
    """
    todo = [i for i in issues
            if not i.get("summary")
            and i.get("link", "").startswith("http")
            and "news.google.com" not in i["link"]][:SUMMARY_MAX_PER_RUN]
    if not todo:
        return 0

    with ThreadPoolExecutor(max_workers=RESOLVE_WORKERS) as pool:
        results = list(pool.map(lambda i: _fetch_summary(i["link"]), todo))

    added = 0
    for issue, summary in zip(todo, results):
        if summary:
            issue["summary"] = summary
            art = raw_by_link.get(issue["link"])
            if art is not None:
                art["summary"] = summary
            added += 1
    return added


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


def cluster_articles(articles: list[dict],
                     title_key: str = "title") -> list[dict]:
    """제목이 비슷한 기사끼리 묶어 이슈(클러스터) 목록을 만든다.

    반환되는 각 이슈는 대표 기사 정보에 더해:
    - mention_count: 보도한 언론사 수(중복 제거)
    - sources: 언론사 목록

    해외 기사는 번역 품질과 무관하게 묶이도록 영어 원문(title_en)
    기준으로 비교할 수 있다(title_key="title_en").
    """
    ordered = sorted(articles, key=lambda a: a["published"])  # 최초 보도 순
    clusters: list[dict] = []

    for art in ordered:
        grams = bigrams(normalize(art.get(title_key) or art["title"]))
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
        issue = {
            "title": rep["title"],
            "link": rep["link"],
            "source": rep["source"],
            "published": rep["published"],
            "latest": items[-1]["published"],
            "mention_count": max(len(sources), 1),
            "sources": sources[:12],
        }
        if rep.get("title_en"):
            issue["title_en"] = rep["title_en"]
        if rep.get("summary"):   # 이전 실행에서 가져온 요약 재사용
            issue["summary"] = rep["summary"]
        issues.append(issue)
    return issues


# ── 섹션 분류 ───────────────────────────────────────────────────────
def categorize(
    issues: list[dict],
    section_defs: list[tuple[str, list[str]]],
    etc_section: str,
    title_key: str = "title",
) -> tuple[list[dict], list[dict]]:
    """이슈를 (주요 이슈 TOP N, 섹션별 목록)으로 나눈다.

    해외 이슈는 영어 원문(title_key="title_en") 기준으로 키워드를 맞춘다.
    """
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
        match_text = (issue.get(title_key) or issue["title"]).lower()
        for name, keywords in section_defs:
            if any(kw.lower() in match_text for kw in keywords):
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
def load_existing(path: Path) -> dict:
    """오늘 파일이 이미 있으면 전체 데이터를 꺼내 온다(누적 병합용)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("sample"):  # 샘플 데이터는 병합하지 않고 버린다
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] 기존 파일 읽기 실패, 새로 시작: {e}")
        return {}


def merge_key(art: dict) -> tuple[str, str]:
    """병합용 중복 판정 키. 링크는 원문 변환 후, 해외 기사 제목은 번역 후
    바뀔 수 있으므로 (영어 원문이 있으면 그것 기준) 제목+언론사 조합을 쓴다."""
    title = art.get("title_en") or art.get("title", "")
    return (normalize(title), art.get("source", ""))


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

    prev = load_existing(out_path)

    fresh = fetch_articles(topic["queries"])
    existing = prev.get("raw", [])
    raw = merge_articles(existing, fresh)
    print(f"신규 {len(fresh)}건 + 기존 {len(existing)}건 → 병합 {len(raw)}건")

    # 이전 실행에서 저장된 제목에 언론사명이 남아 있으면 정리한다
    for art in raw:
        art["title"] = strip_source_suffix(art["title"], art.get("source", ""))

    resolved = resolve_google_links(raw)
    if resolved:
        print(f"구글 뉴스 링크 {resolved}건을 원문 URL로 변환")

    issues = cluster_articles(raw)
    top_issues, sections = categorize(
        issues, topic["sections"], topic["etc_section"]
    )

    # 화면에 보이는 이슈에 한두 줄 요약 부착 (기사 메타 요약, 비용 없음)
    displayed = top_issues + [a for s in sections for a in s["articles"]]
    raw_by_link = {a["link"]: a for a in raw}
    added = add_summaries(displayed, raw_by_link)
    if added:
        print(f"요약 {added}건 추가")

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

    # 해외 뉴스 (주제에 intl 설정이 있을 때만)
    intl_cfg = topic.get("intl")
    if intl_cfg:
        print("--- 🌍 해외 ---")
        intl_fresh = (fetch_articles(intl_cfg["queries"], lang="en")
                      + fetch_feed_articles(intl_cfg["feeds"]))
        intl_existing = (prev.get("intl") or {}).get("raw", [])
        intl_raw = merge_articles(intl_existing, intl_fresh)
        print(f"신규 {len(intl_fresh)}건 + 기존 {len(intl_existing)}건 "
              f"→ 병합 {len(intl_raw)}건")

        resolved = resolve_google_links(intl_raw)
        if resolved:
            print(f"구글 뉴스 링크 {resolved}건을 원문 URL로 변환")
        translated = translate_titles(intl_raw)
        if translated:
            print(f"제목 {translated}건 번역")

        intl_issues = cluster_articles(intl_raw, title_key="title_en")
        intl_top, intl_sections = categorize(
            intl_issues, intl_cfg["sections"], intl_cfg["etc_section"],
            title_key="title_en",
        )
        briefing["intl"] = {
            "title": intl_cfg["title"],
            "top_issues": intl_top,
            "sections": intl_sections,
            "total_articles": len(intl_raw),
            "total_issues": len(intl_issues),
            "raw": sorted(intl_raw, key=lambda a: a["published"],
                          reverse=True),
        }

    topic_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(briefing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{out_path} 저장 (이슈 {len(issues)}개, 주요 이슈 {len(top_issues)}개)")

    # 보존 기간이 지난 날짜 파일은 자동 삭제해 사이트를 가볍게 유지한다
    cutoff = (datetime.strptime(today, "%Y-%m-%d")
              - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for old in topic_dir.glob("????-??-??.json"):
        if old.stem < cutoff:
            old.unlink()
            print(f"보존 기간 경과로 삭제: {old.name}")

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
    # (속보 탭은 항상 맨 앞)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "topics.json").write_text(
        json.dumps(
            {"topics": [BREAKING_META] + [
                {k: t[k] for k in ("id", "label", "emoji", "title")}
                for t in TOPICS
            ]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


# ── 속보 (30분마다 24시간 수집, 굴러가는 24시간 창) ─────────────────
BREAKING_META = {"id": "breaking", "label": "속보", "emoji": "🔴",
                 "title": "속보 타임라인"}
BREAKING_FILE_NAME = "breaking.json"
BREAKING_QUERIES = ["속보", "단독", "긴급"]
BREAKING_MARKERS = ("속보", "단독", "긴급")   # 제목 앞머리에 이 표기가 있어야 속보로 인정
BREAKING_WINDOW_HOURS = 24
BREAKING_MAX_ITEMS = 80
# 스포츠·연예 기사는 속보 탭 성격과 안 맞아 제외한다
BREAKING_EXCLUDE = ["월드컵", "올림픽", "프로야구", "K리그", "축구 대표팀",
                     "홈런", "골 폭발", "전속계약", "아이돌", "콘서트",
                     "앨범", "예능", "드라마", "박스오피스", "열애"]


def is_breaking_title(title: str) -> bool:
    """제목 앞머리(8자 이내)에 속보 표기가 있고, 스포츠·연예가 아닌 것만.

    '메시, 득점 순위 단독 1위'처럼 표기가 제목 중간에 있는 일반 기사를
    거르기 위해 앞머리만 본다.
    """
    if not any(m in title[:8] for m in BREAKING_MARKERS):
        return False
    return not any(x in title for x in BREAKING_EXCLUDE)

# 속보가 어느 분야 이야기인지 아이콘을 붙이기 위한 힌트 키워드
TOPIC_HINTS = {
    "police": ["경찰", "치안", "검거", "해경", "지구대", "파출소"],
    "realestate": ["부동산", "아파트", "전세", "청약", "재건축", "집값",
                    "분양", "재개발"],
    "stock": ["증시", "코스피", "코스닥", "주가", "나스닥", "주식",
              "상장", "금리", "환율"],
}


def run_breaking() -> None:
    """속보만 가볍게 수집해 docs/data/breaking.json 을 갱신한다.

    날짜별 파일이 아니라 '최근 24시간' 단일 파일로 관리해서
    자정이 지나도 타임라인이 끊기지 않는다.
    """
    now = datetime.now(KST)
    out_path = DATA_DIR / BREAKING_FILE_NAME
    print("=== 🔴 속보 ===")

    fresh = fetch_articles(BREAKING_QUERIES)
    # 앞머리에 속보 표기가 없는 일반 기사와 스포츠·연예는 제외
    fresh = [a for a in fresh if is_breaking_title(a["title"])]

    existing: list[dict] = []
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
            if not prev.get("sample"):
                existing = prev.get("raw", [])
        except (json.JSONDecodeError, OSError):
            pass

    raw = merge_articles(existing, fresh)
    # 24시간 창 밖으로 밀려난 기사는 떨어뜨린다
    cutoff = now - timedelta(hours=BREAKING_WINDOW_HOURS)
    kept = []
    for art in raw:
        try:
            if datetime.fromisoformat(art["published"]) >= cutoff:
                kept.append(art)
        except (ValueError, KeyError):
            continue
    raw = kept
    print(f"신규 {len(fresh)}건 + 기존 {len(existing)}건 → 24시간 창 {len(raw)}건")

    for art in raw:
        art["title"] = strip_source_suffix(art["title"], art.get("source", ""))

    resolved = resolve_google_links(raw)
    if resolved:
        print(f"구글 뉴스 링크 {resolved}건을 원문 URL로 변환")

    issues = cluster_articles(raw)
    issues.sort(key=lambda i: i["published"], reverse=True)  # 최신 속보 먼저
    for issue in issues:
        issue["topics"] = [tid for tid, kws in TOPIC_HINTS.items()
                           if any(k in issue["title"] for k in kws)]

    # 타임라인에 보이는 속보에도 한두 줄 요약 부착
    raw_by_link = {a["link"]: a for a in raw}
    added = add_summaries(issues[:BREAKING_MAX_ITEMS], raw_by_link)
    if added:
        print(f"요약 {added}건 추가")

    data = {
        "generated_at": now.isoformat(),
        "window_hours": BREAKING_WINDOW_HOURS,
        "items": issues[:BREAKING_MAX_ITEMS],
        "total_articles": len(raw),
        "raw": sorted(raw, key=lambda a: a["published"], reverse=True),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{out_path} 저장 (이슈 {len(issues)}개)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "breaking":
        run_breaking()
    else:
        main()
