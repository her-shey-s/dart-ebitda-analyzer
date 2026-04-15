"""
dart_api/xml_utils.py
DART XML 문서 다운로드 및 파싱 공유 유틸리티

financial/ 패키지와 depreciation/ 패키지에서 공통으로 사용하는 함수들.
"""

import io
import re
import zipfile
from typing import Optional

import requests
from bs4 import BeautifulSoup

from config import get_dart_api_key, MAX_HTML_SIZE_MB, REQUEST_TIMEOUT

# ── DART document.xml API ──────────────────────────────────────────────────
_DOCUMENT_API_URL = "https://opendart.fss.or.kr/api/document.xml"


def download_dart_document(rcept_no: str) -> Optional[bytes]:
    """
    DART document.xml API로 보고서 ZIP을 다운로드하고 XML bytes를 반환한다.

    ZIP 안에 XML 파일이 여럿일 경우 가장 큰 파일을 선택한다.

    Args:
        rcept_no: 공시 접수번호

    Returns:
        XML 파일 bytes 또는 None (다운로드/압축 오류 시)
    """
    try:
        resp = requests.get(
            _DOCUMENT_API_URL,
            params={"crtfc_key": get_dart_api_key(), "rcept_no": rcept_no},
            timeout=REQUEST_TIMEOUT * 2,
            stream=True,
        )
        resp.raise_for_status()

        content = b""
        limit = MAX_HTML_SIZE_MB * 1024 * 1024
        for chunk in resp.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > limit:
                return None

        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            # 가장 큰 파일 선택 (보통 하나지만 여럿일 수 있음)
            names = zf.namelist()
            target = max(names, key=lambda n: zf.getinfo(n).file_size)
            return zf.read(target)

    except (requests.RequestException, zipfile.BadZipFile, KeyError):
        return None


def parse_dart_xml(xml_bytes: bytes) -> Optional[BeautifulSoup]:
    """
    DART XML bytes를 BeautifulSoup으로 파싱한다.

    DART XML은 dart4.xsd 커스텀 스키마를 사용하므로 lxml HTML 파서로 처리한다.

    Args:
        xml_bytes: download_dart_document() 반환값

    Returns:
        BeautifulSoup 객체 또는 None
    """
    try:
        return BeautifulSoup(xml_bytes, "lxml")
    except Exception:
        return None


def xml_table_to_rows(table_tag) -> list[list[str]]:
    """
    BeautifulSoup table 태그를 문자열 행 리스트로 변환한다.

    DART XML의 td / th / tu(금액셀) / te 모두 처리한다.

    Args:
        table_tag: BeautifulSoup의 <table> 태그

    Returns:
        [[셀1, 셀2, ...], ...] 형태의 행 리스트
    """
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = tr.find_all(["td", "th", "tu", "te"])
        row = [c.get_text(strip=True) for c in cells]
        if any(row):  # 빈 행 제외
            rows.append(row)
    return rows


def normalize_title(text: str) -> str:
    """TITLE 태그 텍스트에서 공백을 제거하여 섹션명을 정규화한다.

    DART XML의 TITLE 태그는 '재 무 상 태 표' 처럼 전각/반각 공백이 섞여 있다.
    """
    return re.sub(r"[\s\u3000\xa0\u00a0\u200b\u200c\u200d\ufeff]+", "", text)
