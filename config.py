"""
config.py
프로젝트 전역 설정: 추출 항목 정의, API 엔드포인트, 기타 상수
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API 키 ──────────────────────────────────────────────────────────────
DART_API_KEY = os.getenv("DART_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── DART API 엔드포인트 ──────────────────────────────────────────────────
DART_BASE_URL = "https://opendart.fss.or.kr/api"
DART_ENDPOINTS = {
    "corp_code":      f"{DART_BASE_URL}/corpCode.xml",       # 전체 기업 코드 다운로드
    "company_info":   f"{DART_BASE_URL}/company.json",       # 기업 기본정보
    "disclosure_list":f"{DART_BASE_URL}/list.json",          # 공시 목록
    "financial_stmt": f"{DART_BASE_URL}/fnlttSinglAcntAll.json",  # 단일회사 전체 재무제표 (경로A)
    "major_account":  f"{DART_BASE_URL}/fnlttSinglAcnt.json",     # 주요계정 (교차검증용)
    "doc_index":      f"{DART_BASE_URL}/index.json",         # 보고서 원문 문서 목록
}

# ── 재무제표 구분 코드 ────────────────────────────────────────────────────
FS_DIV = {
    "consolidated":   "CFS",   # 연결재무제표
    "separate":       "OFS",   # 별도(개별)재무제표
}

# ── 보고서 유형 코드 ──────────────────────────────────────────────────────
REPORT_CODES = {
    "annual":          "11011",  # 사업보고서
    "semi_annual":     "11012",  # 반기보고서
    "q1":              "11013",  # 1분기 보고서
    "q3":              "11014",  # 3분기 보고서
    "audit_consol":    "11004",  # 연결감사보고서
    "audit_separate":  "11005",  # 감사보고서
}

# ── 추출 항목 정의 ────────────────────────────────────────────────────────
# 각 항목:
#   name        : 표시 이름
#   dart_code   : DART API 계정과목코드 (경로A)
#   fs_type     : 재무제표 유형 ('BS'=재무상태표, 'IS'=손익계산서)
#   keywords    : 감사보고서 HTML 파싱 시 매칭할 키워드 목록 (경로B)
#   sign        : 부호 처리 (양수 = 1, 음수 가능 = -1)

FINANCIAL_ITEMS = [
    # ── 재무상태표 항목 ──────────────────────────────────────────────────
    {
        "name":      "총자산",
        "dart_code": "ifrs-full_Assets",
        "fs_type":   "BS",
        "keywords":  ["자산총계", "총자산", "자산 총계"],
        "sign":      1,
    },
    {
        "name":      "총부채",
        "dart_code": "ifrs-full_Liabilities",
        "fs_type":   "BS",
        "keywords":  ["부채총계", "총부채", "부채 총계"],
        "sign":      1,
    },
    {
        "name":      "자본총계",
        "dart_code": "ifrs-full_Equity",
        "fs_type":   "BS",
        "keywords":  ["자본총계", "총자본", "자본 총계"],
        "sign":      1,
    },
    {
        "name":      "이익잉여금",
        "dart_code": "ifrs-full_RetainedEarnings",
        "fs_type":   "BS",
        "keywords":  ["이익잉여금", "결손금", "이익잉여금(결손금)", "미처분이익잉여금"],
        "sign":      1,
    },
    # ── 손익계산서 항목 ──────────────────────────────────────────────────
    {
        "name":      "매출액",
        "dart_code": "ifrs-full_Revenue",
        "fs_type":   "IS",
        "keywords":  ["매출액", "영업수익", "수익(매출액)", "매출", "수익"],
        "sign":      1,
    },
    {
        "name":      "매출원가",
        "dart_code": "ifrs-full_CostOfSales",
        "fs_type":   "IS",
        "keywords":  ["매출원가", "영업비용", "매출액의 원가"],
        "sign":      1,
    },
    {
        "name":      "매출총이익",
        "dart_code": "ifrs-full_GrossProfit",
        "fs_type":   "IS",
        "keywords":  ["매출총이익", "매출총손실", "매출총이익(손실)"],
        "sign":      1,
    },
    {
        "name":      "영업이익",
        "dart_code": "dart_OperatingIncomeLoss",
        "fs_type":   "IS",
        "keywords":  ["영업이익", "영업손실", "영업이익(손실)", "영업손익"],
        "sign":      1,
    },
    {
        "name":      "당기순이익",
        "dart_code": "ifrs-full_ProfitLoss",
        "fs_type":   "IS",
        "keywords":  ["당기순이익", "당기순손실", "당기순이익(손실)", "분기순이익"],
        "sign":      1,
    },
]

# 항목명 → 설정 딕셔너리 빠른 조회
ITEM_MAP: dict = {item["name"]: item for item in FINANCIAL_ITEMS}

# ── 검증 규칙 ─────────────────────────────────────────────────────────────
# 회계 항등식: (좌변 항목명 리스트, 우변 항목명 리스트, 허용 오차 비율)
ACCOUNTING_IDENTITIES = [
    # 자산 = 부채 + 자본
    (["총자산"], ["총부채", "자본총계"], 0.01),
    # 매출총이익 = 매출액 - 매출원가
    (["매출총이익"], ["매출액", "매출원가"], 0.01),   # 매출원가는 빼는 방향으로 rules.py에서 처리
]

# ── 기타 상수 ─────────────────────────────────────────────────────────────
CACHE_DB_PATH = "cache.db"
REQUEST_TIMEOUT = 15          # 초
MAX_HTML_SIZE_MB = 10         # HTML 다운로드 최대 크기
GEMINI_MODEL = "gemini-1.5-flash"
