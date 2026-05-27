"""
scripts/capture_fixture.py
실제 DART 보고서를 회귀 테스트 fixture로 동결(freeze)하는 캡처 도구.

워크플로우(틀렸던 보고서를 테스트 자산으로 누적):
  1. 실제 보고서를 한 번만 네트워크로 가져와 원본 입력(JSON/XML)을 저장한다.
  2. 저장된 원본으로 현재 추출기를 오프라인 실행해 golden 결과를 만든다.
  3. golden.json을 사람이 검토(값이 원문과 맞는지) 후 커밋하면 그 동작이 잠긴다.
  이후 추출 로직이 회귀하면 test_regression.py가 즉시 잡아낸다.

사용법:
    python scripts/capture_fixture.py <기업명> <연도> --id <fixture_id> \
        [--description "설명"] [--no-depreciation]

예:
    python scripts/capture_fixture.py 삼성전자 2023 --id samsung_2023_path_a
    python scripts/capture_fixture.py 만전식품 2024 --id manjeon_2024_path_b

주의: DART_API_KEY가 .env/환경변수/secrets에 설정되어 있어야 한다(네트워크 필요).
golden.json은 반드시 사람이 원문과 대조해 검토한 뒤 커밋할 것.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 저장소 루트를 import 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import FS_DIV, get_dart_api_key  # noqa: E402
from dart_api.corp_search import get_corp_code  # noqa: E402
from dart_api.report_finder import find_report  # noqa: E402
from dart_api.xml_utils import download_dart_document  # noqa: E402
from financial.api_extractor import fetch_full_financial_statement  # noqa: E402
from tests import _harness  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

_REPORT_TYPE_TO_FS_DIV = {"audit_consol": "CFS", "audit_separate": "OFS"}


def _capture_path_a(fixture_dir: Path, corp_code: str, year: int, report: dict) -> dict:
    reprt_code = report["reprt_code"]
    chosen_div, rows = None, None
    for div in (FS_DIV["consolidated"], FS_DIV["separate"]):
        try:
            candidate = fetch_full_financial_statement(corp_code, str(year), reprt_code, div)
        except Exception as e:  # noqa: BLE001
            print(f"  {div} 조회 실패: {e}")
            continue
        if candidate:
            chosen_div, rows = div, candidate
            break
    if rows is None:
        raise SystemExit("경로A 재무제표를 가져오지 못했습니다.")
    print(f"  재무제표 기준 선택: {chosen_div} ({len(rows)}행)")
    (fixture_dir / "raw_financial_cfs.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    raw = {"financial_json": "raw_financial_cfs.json"}
    run = {"financials": True, "depreciation": False}
    # 감가상각은 document.xml에서 추출하므로 본문도 함께 저장한다.
    xml = download_dart_document(report["rcept_no"])
    if xml:
        (fixture_dir / "raw_input.xml").write_bytes(xml)
        raw["xml"] = "raw_input.xml"
        run["depreciation"] = True

    return {
        "path": "A",
        "corp_code": corp_code,
        "year": year,
        "reprt_code": reprt_code,
        "requested_div": chosen_div,
        "fs_div": chosen_div,
        "rcept_no": report["rcept_no"],
        "report_type": report.get("report_type"),
        "raw": raw,
        "run": run,
    }


def _capture_path_b(fixture_dir: Path, report: dict) -> dict:
    xml = download_dart_document(report["rcept_no"])
    if not xml:
        raise SystemExit("document.xml 다운로드 실패")
    (fixture_dir / "raw_input.xml").write_bytes(xml)
    report_type = report.get("report_type")
    return {
        "path": "B",
        "rcept_no": report["rcept_no"],
        "report_type": report_type,
        "fs_div": _REPORT_TYPE_TO_FS_DIV.get(report_type, "CFS"),
        "strict_scope": False,
        "raw": {"xml": "raw_input.xml"},
        "run": {"financials": True, "depreciation": True},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="실제 DART 보고서를 회귀 fixture로 캡처")
    ap.add_argument("corp_name")
    ap.add_argument("year", type=int)
    ap.add_argument("--id", required=True, help="fixture 디렉터리 이름")
    ap.add_argument("--description", default="", help="fixture 설명(보고서 형식 특징)")
    ap.add_argument("--no-depreciation", action="store_true", help="감가상각 추출 생략")
    args = ap.parse_args()

    if not get_dart_api_key():
        raise SystemExit("DART_API_KEY가 설정되어 있지 않습니다(.env/환경변수 확인).")

    corp_code = get_corp_code(args.corp_name)
    if not corp_code:
        raise SystemExit(f"'{args.corp_name}' 기업코드를 찾지 못했습니다.")
    print(f"기업: {args.corp_name} (corp_code={corp_code})")

    report = find_report(corp_code, args.year)
    if not report:
        raise SystemExit(f"{args.year}년 보고서를 찾지 못했습니다.")
    print(f"보고서: {report['report_nm']} (path={report['path']}, rcept_no={report['rcept_no']})")

    fixture_dir = FIXTURES_DIR / args.id
    fixture_dir.mkdir(parents=True, exist_ok=True)

    if report["path"] == "A":
        manifest = _capture_path_a(fixture_dir, corp_code, args.year, report)
    else:
        manifest = _capture_path_b(fixture_dir, report)

    if args.no_depreciation:
        manifest["run"]["depreciation"] = False

    manifest = {
        "id": args.id,
        "kind": "real",
        "description": args.description or f"{args.corp_name} {args.year} {report['report_nm']}",
        **manifest,
    }
    (fixture_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # 저장된 원본으로 오프라인 추출 → golden 생성
    golden = _harness.build_result(manifest, fixture_dir)
    (fixture_dir / "golden.json").write_text(
        json.dumps(golden, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"\n캡처 완료: {fixture_dir}")
    print("  - manifest.json / 원본 입력 / golden.json 생성됨")
    print("  >> golden.json의 값이 원문 보고서와 일치하는지 반드시 검토 후 커밋하세요.")
    print(f"  EBITDA={golden.get('ebitda')}, fs_div={golden.get('fs_div')}")


if __name__ == "__main__":
    main()
