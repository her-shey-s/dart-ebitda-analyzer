"""
dart_api/corp_search.py
기업 검색 및 corp_code 매핑

DART API의 전체 기업 코드 ZIP을 다운로드/캐싱하고,
기업명으로 corp_code를 조회한다.

캐싱 전략:
  1. 메모리 캐시  : 프로세스 생애 동안 유지 (가장 빠름)
  2. 로컬 파일 캐시: data/corp_codes.pkl — 하루에 한 번만 재다운로드
  3. DART API    : 캐시 없거나 만료 시 다운로드
"""

import io
import os
import pickle
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Optional

import requests
import pandas as pd

from config import DART_API_KEY, DART_ENDPOINTS, REQUEST_TIMEOUT


# ── 상수 ──────────────────────────────────────────────────────────────────
DATA_DIR = "data"
CACHE_FILE = os.path.join(DATA_DIR, "corp_codes.pkl")
CACHE_TTL_SEC = 24 * 3600  # 24시간

# 메모리 캐시
_corp_df: Optional[pd.DataFrame] = None


# ── 내부 유틸 ──────────────────────────────────────────────────────────────

def _ensure_data_dir() -> None:
    """data/ 폴더가 없으면 생성한다."""
    os.makedirs(DATA_DIR, exist_ok=True)


def _is_cache_fresh() -> bool:
    """로컬 캐시 파일이 존재하고 TTL 이내인지 확인한다."""
    if not os.path.exists(CACHE_FILE):
        return False
    age = time.time() - os.path.getmtime(CACHE_FILE)
    return age < CACHE_TTL_SEC


def _load_file_cache() -> Optional[pd.DataFrame]:
    """로컬 pkl 파일에서 DataFrame을 로드한다."""
    try:
        with open(CACHE_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_file_cache(df: pd.DataFrame) -> None:
    """DataFrame을 로컬 pkl 파일에 저장한다."""
    _ensure_data_dir()
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(df, f)


# ── 공개 함수 ──────────────────────────────────────────────────────────────

def download_corp_codes(force: bool = False) -> pd.DataFrame:
    """
    DART API에서 전체 기업 코드 ZIP을 다운로드하고 DataFrame으로 반환한다.

    캐시 우선 순위: 메모리 → 로컬 파일(24h 이내) → DART API

    Args:
        force: True이면 캐시를 무시하고 DART에서 재다운로드

    Returns:
        corp_code, corp_name, stock_code, modify_date 컬럼을 가진 DataFrame

    Raises:
        requests.HTTPError: API 호출 실패 시
        zipfile.BadZipFile: 응답이 유효한 ZIP이 아닐 때
        RuntimeError: DART_API_KEY가 설정되지 않았을 때
    """
    global _corp_df

    if not DART_API_KEY:
        raise RuntimeError("DART_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")

    # 1. 메모리 캐시
    if _corp_df is not None and not force:
        return _corp_df

    # 2. 로컬 파일 캐시
    if not force and _is_cache_fresh():
        df = _load_file_cache()
        if df is not None:
            _corp_df = df
            return _corp_df

    # 3. DART API 다운로드
    resp = requests.get(
        DART_ENDPOINTS["corp_code"],
        params={"crtfc_key": DART_API_KEY},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_data = zf.read("CORPCODE.xml")

    root = ET.fromstring(xml_data)
    records = [
        {
            "corp_code":   item.findtext("corp_code", "").strip(),
            "corp_name":   item.findtext("corp_name", "").strip(),
            "stock_code":  item.findtext("stock_code", "").strip(),
            "modify_date": item.findtext("modify_date", "").strip(),
        }
        for item in root.findall("list")
    ]

    df = pd.DataFrame(records)
    _save_file_cache(df)
    _corp_df = df
    return df


def search_corp(name: str) -> pd.DataFrame:
    """
    기업명으로 기업을 검색한다. 정확 일치 결과를 우선 반환하고,
    없으면 부분 일치 결과를 반환한다.

    Args:
        name: 검색할 기업명

    Returns:
        매칭된 기업 DataFrame (corp_code, corp_name, stock_code, modify_date).
        정확 일치가 있으면 해당 행들만, 없으면 부분 일치 행들.
        아무것도 없으면 빈 DataFrame.
    """
    df = download_corp_codes()

    exact = df[df["corp_name"] == name].reset_index(drop=True)
    if not exact.empty:
        return exact

    partial = df[df["corp_name"].str.contains(name, na=False, regex=False)].reset_index(drop=True)
    return partial


def get_corp_code(name: str) -> Optional[str]:
    """
    기업명으로 corp_code를 반환한다. 정확 일치만 허용한다.

    Args:
        name: 기업 정식명칭 (정확 일치)

    Returns:
        corp_code 문자열(8자리), 찾지 못하면 None
    """
    df = download_corp_codes()
    matched = df[df["corp_name"] == name]
    if matched.empty:
        return None
    return matched.iloc[0]["corp_code"]


def get_company_info(corp_code: str) -> dict:
    """
    DART API로 기업 기본정보를 조회한다.

    Args:
        corp_code: DART 기업 고유코드 (8자리)

    Returns:
        기업정보 딕셔너리 (corp_name, ceo_nm, corp_cls, jurir_no 등)

    Raises:
        requests.HTTPError: API 오류 시
        ValueError: API가 오류 상태 코드를 반환할 때
    """
    resp = requests.get(
        DART_ENDPOINTS["company_info"],
        params={"crtfc_key": DART_API_KEY, "corp_code": corp_code},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        raise ValueError(f"DART API 오류: {data.get('message', '알 수 없는 오류')}")

    return data


# ── 하위 호환 별칭 (app.py에서 사용 중) ────────────────────────────────────

def search_corp_by_name(name: str, exact: bool = False) -> pd.DataFrame:
    """search_corp()의 하위 호환 래퍼."""
    df = download_corp_codes()
    if exact:
        return df[df["corp_name"] == name].reset_index(drop=True)
    return df[df["corp_name"].str.contains(name, na=False, regex=False)].reset_index(drop=True)
