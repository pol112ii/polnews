#!/usr/bin/env python3
"""링크 변환 기능을 실데이터 일부로 검증하는 스크립트 (CI 테스트용)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scrape import DATA_DIR, resolve_google_links

data_files = sorted(DATA_DIR.glob("????-??-??.json"), reverse=True)
if not data_files:
    print("데이터 파일 없음 — 테스트 생략")
    sys.exit(0)

data = json.loads(data_files[0].read_text(encoding="utf-8"))
samples = [dict(a) for a in data.get("raw", [])
           if "news.google.com" in a.get("link", "")][:5]
if not samples:
    print("구글 링크 없음 — 테스트 생략")
    sys.exit(0)

print(f"{len(samples)}건 변환 시도:")
resolved = resolve_google_links(samples)
for a in samples:
    mark = "✅" if "news.google.com" not in a["link"] else "❌"
    print(f"  {mark} {a['title'][:40]}")
    print(f"     → {a['link'][:100]}")

if resolved == 0:
    print("변환 실패: 0건")
    sys.exit(1)
print(f"성공 {resolved}/{len(samples)}건")
