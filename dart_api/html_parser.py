"""
dart_api/html_parser.py
경로B: 감사보고서 HTML 파싱

감사보고서 원문 HTML을 다운로드하고 BeautifulSoup + pandas로
재무제표 테이블을 파싱하여 항목별 금액을 추출한다.
파싱 실패 시 None을 반환하며, 호출자(app.py)가 Gemini로 폴백한다.
"""

import re
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup

from config import FINANCIAL_ITEMS, MAX_HTML_SIZE_MB, REQUEST_TIMEOUT


def download_html(url: str) -> Optional[str]:
    """
    주어진 URL에서 HTML을 다운로드한다.

    Args:
        url: 감사보고서 원문 HTML URL

    Returns:
        HTML 문자열 또는 None (크기 초과 / 오류)
    """
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()

        # 크기 제한
        content = b""
        limit = MAX_HTML_SIZE_MB * 1024 * 1024
        for chunk in resp.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > limit:
                return None  # 너무 큰 파일은 건너뜀

        return content.decode("utf-8", errors="replace")
    except requests.RequestException:
        return None


def extract_tables_from_html(html: str) -> list[pd.DataFrame]:
    """
    HTML에서 모든 <table> 태그를 파싱하여 DataFrame 리스트로 반환한다.

    Args:
        html: HTML 문자열

    Returns:
        pandas DataFrame 리스트 (파싱 실패한 테이블은 제외)
    """
    soup = BeautifulSoup(html, "lxml")
    tables = []
    for table_tag in soup.find_all("table"):
        try:
            df_list = pd.read_html(str(table_tag), thousands=",")
            tables.extend(df_list)
        except Exception:
            continue
    return tables


def _normalize_text(text: str) -> str:
    """공백·특수문자를 제거하여 키워드 매칭을 단순화한다."""
    return re.sub(r"[\s\u3000\xa0\u200b]+", "", text)


def find_item_in_table(df: pd.DataFrame, keywords: list[str]) -> Optional[float]:
    """
    DataFrame의 첫 번째 컬럼에서 키워드를 찾고, 당기 금액(보통 두 번째 숫자 컬럼)을 반환한다.

    Args:
        df:       재무제표 테이블 DataFrame
        keywords: 매칭할 키워드 리스트 (config.FINANCIAL_ITEMS의 keywords)

    Returns:
        금액(float) 또는 None
    """
    if df.empty or df.shape[1] < 2:
        return None

    label_col = df.iloc[:, 0].astype(str)
    # 숫자가 있는 컬럼만 추출 (당기 = 첫 번째 숫자 컬럼)
    numeric_cols = [c for c in df.columns[1:] if pd.to_numeric(df[c], errors="coerce").notna().any()]
    if not numeric_cols:
        return None
    amount_col = numeric_cols[0]

    norm_keywords = [_normalize_text(kw) for kw in keywords]

    for idx, label in enumerate(label_col):
        norm_label = _normalize_text(label)
        if any(kw in norm_label for kw in norm_keywords):
            raw = df.iloc[idx][amount_col]
            return _to_float(raw)

    return None


def _to_float(value) -> Optional[float]:
    """
    테이블 셀 값을 float으로 변환한다. 괄호 표기(음수)를 지원한다.

    Args:
        value: 테이블 셀 원본 값 (str, int, float)

    Returns:
        float 또는 None
    """
    if pd.isna(value):
        return None
    text = str(value).replace(",", "").strip()
    # 괄호 → 음수: (123) → -123
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


def parse_financial_data_from_html(html: str) -> dict[str, Optional[float]]:
    """
    HTML에서 config.FINANCIAL_ITEMS에 정의된 모든 항목을 파싱한다.

    여러 테이블을 순회하며 각 항목을 찾고, 첫 번째로 매칭된 값을 사용한다.

    Args:
        html: 감사보고서 원문 HTML 문자열

    Returns:
        항목명 → 금액(float) 딕셔너리.
        찾지 못한 항목은 None.
    """
    tables = extract_tables_from_html(html)
    result: dict[str, Optional[float]] = {item["name"]: None for item in FINANCIAL_ITEMS}

    for item in FINANCIAL_ITEMS:
        for df in tables:
            value = find_item_in_table(df, item["keywords"])
            if value is not None:
                result[item["name"]] = value
                break  # 이 항목은 찾았으므로 다음 항목으로

    return result


def is_parse_successful(result: dict[str, Optional[float]], min_found: int = 4) -> bool:
    """
    파싱 결과의 성공 여부를 판단한다.

    Args:
        result:    parse_financial_data_from_html() 반환값
        min_found: 성공으로 간주할 최소 항목 수

    Returns:
        True이면 파싱 성공, False이면 Gemini 폴백 필요
    """
    found = sum(1 for v in result.values() if v is not None)
    return found >= min_found
