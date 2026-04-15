"""
감사보고서 XML 원문(ZIP) 다운로드 간이 스크립트

사용법:
    python fetch_raw_xml.py                     # 만전식품, 최근 연도
    python fetch_raw_xml.py 삼성전자 2023
"""

import os
import sys

import requests

from config import get_dart_api_key, DART_BASE_URL, REQUEST_TIMEOUT
from dart_api.corp_search import get_corp_code, search_corp
from dart_api.report_finder import find_report

OUTPUT_DIR = "output"


def main():
    corp_name = sys.argv[1] if len(sys.argv) > 1 else "만전식품"
    year = int(sys.argv[2]) if len(sys.argv) > 2 else 2024

    # 1) corp_code 조회
    corp_code = get_corp_code(corp_name)
    if not corp_code:
        print(f"'{corp_name}' 정확 일치 없음. 부분 검색 결과:")
        print(search_corp(corp_name).to_string(index=False))
        return

    print(f"기업: {corp_name} (corp_code={corp_code})")

    # 2) 보고서 탐색
    report = find_report(corp_code, year)
    if not report:
        print(f"{year}년도 보고서를 찾을 수 없습니다.")
        return

    rcept_no = report["rcept_no"]
    print(f"보고서: {report['report_nm']} (rcept_no={rcept_no}, path={report['path']})")

    # 3) document.xml API로 ZIP 다운로드
    resp = requests.get(
        f"{DART_BASE_URL}/document.xml",
        params={"crtfc_key": get_dart_api_key(), "rcept_no": rcept_no},
        timeout=30,
    )
    resp.raise_for_status()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{corp_name}_{year}_{rcept_no}.zip"
    filepath = os.path.join(OUTPUT_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(resp.content)

    print(f"저장 완료: {filepath} ({len(resp.content):,} bytes)")


if __name__ == "__main__":
    main()
