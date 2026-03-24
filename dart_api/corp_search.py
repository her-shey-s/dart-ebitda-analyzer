"""
dart_api/corp_search.py
기업 검색 및 corp_code 매핑

DART API의 전체 기업 코드 ZIP을 다운로드/캐싱하고,
기업명 또는 사업자등록번호로 corp_code를 조회한다.
"""

import io
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

import requests
import pandas as pd

from config import DART_API_KEY, DART_ENDPOINTS, REQUEST_TIMEOUT
from utils.cache import get_cache, set_cache


# 메모리 캐시 (프로세스 생애 동안 유지)
_corp_df: Optional[pd.DataFrame] = None


def _download_corp_code_zip() -> pd.DataFrame:
    """
    DART에서 전체 기업 코드 ZIP을 다운로드하고 DataFrame으로 변환한다.

    Returns:
        corp_code, corp_name, stock_code, modify_date 컬럼을 가진 DataFrame

    Raises:
        requests.HTTPError: API 호출 실패 시
        zipfile.BadZipFile: 압축 파일 손상 시
    """
    url = DART_ENDPOINTS["corp_code"]
    resp = requests.get(url, params={"crtfc_key": DART_API_KEY}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_data = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_data)
    records = []
    for item in root.findall("list"):
        records.append({
            "corp_code":   item.findtext("corp_code", "").strip(),
            "corp_name":   item.findtext("corp_name", "").strip(),
            "stock_code":  item.findtext("stock_code", "").strip(),
            "modify_date": item.findtext("modify_date", "").strip(),
        })

    return pd.DataFrame(records)


def load_corp_list(force_refresh: bool = False) -> pd.DataFrame:
    """
    전체 기업 목록을 로드한다. SQLite 캐시 → 메모리 순으로 우선 사용.

    Args:
        force_refresh: True이면 캐시를 무시하고 DART에서 재다운로드

    Returns:
        기업 코드 DataFrame
    """
    global _corp_df
    if _corp_df is not None and not force_refresh:
        return _corp_df

    cache_key = "corp_code_list"
    if not force_refresh:
        cached = get_cache(cache_key)
        if cached is not None:
            _corp_df = cached
            return _corp_df

    _corp_df = _download_corp_code_zip()
    set_cache(cache_key, _corp_df, ttl_hours=24)
    return _corp_df


def search_corp_by_name(name: str, exact: bool = False) -> pd.DataFrame:
    """
    기업명으로 기업을 검색한다.

    Args:
        name:  검색할 기업명 (부분 일치 가능)
        exact: True이면 완전 일치만 반환

    Returns:
        매칭된 기업 행들로 이루어진 DataFrame
    """
    df = load_corp_list()
    if exact:
        return df[df["corp_name"] == name].reset_index(drop=True)
    return df[df["corp_name"].str.contains(name, na=False)].reset_index(drop=True)


def get_corp_code(corp_name: str) -> Optional[str]:
    """
    기업명으로 corp_code를 반환한다. 완전 일치 우선, 없으면 첫 번째 부분 일치.

    Args:
        corp_name: 기업 정식명칭

    Returns:
        corp_code 문자열, 찾지 못하면 None
    """
    exact = search_corp_by_name(corp_name, exact=True)
    if not exact.empty:
        return exact.iloc[0]["corp_code"]

    partial = search_corp_by_name(corp_name, exact=False)
    if not partial.empty:
        return partial.iloc[0]["corp_code"]

    return None


def get_company_info(corp_code: str) -> dict:
    """
    DART API로 기업 기본정보를 조회한다.

    Args:
        corp_code: DART 기업 고유코드 (8자리)

    Returns:
        기업정보 딕셔너리 (corp_name, ceo_nm, corp_cls, jurir_no 등)

    Raises:
        requests.HTTPError: API 오류 시
        ValueError: 조회 결과가 없을 때
    """
    url = DART_ENDPOINTS["company_info"]
    resp = requests.get(
        url,
        params={"crtfc_key": DART_API_KEY, "corp_code": corp_code},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        raise ValueError(f"DART API 오류: {data.get('message', '알 수 없는 오류')}")

    return data
