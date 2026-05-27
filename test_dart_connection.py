"""
test_dart_connection.py
DART API 연결 문제 진단 스크립트

각 네트워크 계층을 순서대로 테스트하여 어디서 실패하는지 정확히 파악한다.
실행: python test_dart_connection.py
"""

import socket
import ssl
import time
import sys

import requests
from config import get_dart_api_key, DART_BASE_URL, DART_ENDPOINTS

# config는 더 이상 모듈 수준 DART_API_KEY 상수를 노출하지 않는다(get_dart_api_key()로 통일).
DART_API_KEY = get_dart_api_key()

DART_HOST = "opendart.fss.or.kr"
DART_PORT = 443


def _header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _result(passed: bool, elapsed: float, detail: str = ""):
    tag = "\033[92mPASS\033[0m" if passed else "\033[91mFAIL\033[0m"
    print(f"  [{tag}] {elapsed:.3f}s  {detail}")


# ── 1. API 키 확인 ──────────────────────────────────────────────────────
def test_api_key():
    _header("1. API 키 확인")
    t0 = time.time()

    if not DART_API_KEY:
        _result(False, time.time() - t0, "DART_API_KEY가 비어 있음 (.env 또는 환경변수 확인)")
        return False

    masked = DART_API_KEY[:4] + "****" + DART_API_KEY[-4:]
    length = len(DART_API_KEY)
    _result(True, time.time() - t0, f"키 길이={length}, 값={masked}")

    if length != 40:
        print(f"  ⚠ 일반적인 DART API 키는 40자인데 현재 {length}자 — 키가 잘렸거나 잘못된 값일 수 있음")
    return True


# ── 2. DNS 해석 ─────────────────────────────────────────────────────────
def test_dns():
    _header("2. DNS 해석")
    t0 = time.time()
    try:
        results = socket.getaddrinfo(DART_HOST, DART_PORT, socket.AF_UNSPEC, socket.SOCK_STREAM)
        elapsed = time.time() - t0
        ips = sorted(set(r[4][0] for r in results))
        _result(True, elapsed, f"{DART_HOST} → {', '.join(ips)}")
        return True
    except socket.gaierror as e:
        _result(False, time.time() - t0, f"DNS 해석 실패: {e}")
        print("  → 인터넷 연결 또는 DNS 서버 확인 필요")
        return False


# ── 3. TCP 연결 ─────────────────────────────────────────────────────────
def test_tcp():
    _header("3. TCP 연결 (443 포트)")
    t0 = time.time()
    try:
        sock = socket.create_connection((DART_HOST, DART_PORT), timeout=15)
        elapsed = time.time() - t0
        peer = sock.getpeername()
        sock.close()
        _result(True, elapsed, f"연결 성공: {peer[0]}:{peer[1]}")
        return True
    except (socket.timeout, OSError) as e:
        _result(False, time.time() - t0, f"TCP 연결 실패: {e}")
        print("  → 방화벽, 프록시, 또는 DART 서버 다운 가능성")
        return False


# ── 4. TLS 핸드셰이크 ──────────────────────────────────────────────────
def test_tls():
    _header("4. TLS/SSL 핸드셰이크")
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((DART_HOST, DART_PORT), timeout=15)
        wrapped = ctx.wrap_socket(raw, server_hostname=DART_HOST)
        elapsed = time.time() - t0
        cert = wrapped.getpeercert()
        cn = dict(x[0] for x in cert.get("subject", ((("commonName", "?"),),)))
        not_after = cert.get("notAfter", "?")
        _result(True, elapsed, f"TLS OK — CN={cn.get('commonName','?')}, 만료={not_after}")
        wrapped.close()
        return True
    except Exception as e:
        _result(False, time.time() - t0, f"TLS 실패: {type(e).__name__}: {e}")
        print("  → SSL 인증서 문제이거나 중간자 프록시 간섭 가능성")
        return False


# ── 5. HTTP API 키 유효성 (가벼운 요청) ────────────────────────────────
def test_api_auth():
    _header("5. HTTP API 키 유효성 검증 (company.json)")
    t0 = time.time()
    try:
        # 존재하지 않는 corp_code로 요청 → status "013"(데이터 없음)이면 키는 유효
        resp = requests.get(
            DART_ENDPOINTS["company_info"],
            params={"crtfc_key": DART_API_KEY, "corp_code": "00000000"},
            timeout=30,
        )
        elapsed = time.time() - t0

        print(f"  HTTP {resp.status_code} / Content-Type: {resp.headers.get('Content-Type', '?')}")

        if resp.status_code != 200:
            _result(False, elapsed, f"HTTP {resp.status_code}")
            print(f"  응답 본문(처음 500자): {resp.text[:500]}")
            return False

        data = resp.json()
        status = data.get("status", "?")
        message = data.get("message", "?")
        print(f"  API status={status}, message={message}")

        if status == "000":
            _result(True, elapsed, "API 키 유효 + 데이터 반환됨 (예상 밖)")
        elif status == "013":
            _result(True, elapsed, "API 키 유효 (데이터 없음 — 정상)")
        elif status == "010":
            _result(False, elapsed, "API 키가 등록되지 않았거나 만료됨")
            print("  → DART Open API 사이트에서 API 키 상태 확인 필요")
            return False
        elif status == "020":
            _result(False, elapsed, "일일 트래픽 초과")
            print("  → DART API 일일 요청 한도(10,000건) 초과")
            return False
        elif status == "011":
            _result(False, elapsed, "사용할 수 없는 API 키")
            return False
        else:
            _result(True, elapsed, f"알 수 없는 status={status} — 연결 자체는 됨")
        return True

    except requests.ConnectionError as e:
        _result(False, time.time() - t0, f"ConnectionError: {e}")
        return False
    except requests.Timeout as e:
        _result(False, time.time() - t0, f"Timeout: {e}")
        return False
    except Exception as e:
        _result(False, time.time() - t0, f"{type(e).__name__}: {e}")
        return False


# ── 6. 전체 엔드포인트 응답 확인 ───────────────────────────────────────
def test_all_endpoints():
    _header("6. 전체 엔드포인트 응답 확인")

    endpoints = {
        "corpCode.xml":          f"{DART_BASE_URL}/corpCode.xml",
        "company.json":          DART_ENDPOINTS["company_info"],
        "list.json":             DART_ENDPOINTS["disclosure_list"],
        "fnlttSinglAcntAll.json":DART_ENDPOINTS["financial_stmt"],
        "index.json":            DART_ENDPOINTS["doc_index"],
        "document.xml":          f"{DART_BASE_URL}/document.xml",
    }

    all_ok = True
    for name, url in endpoints.items():
        t0 = time.time()
        try:
            resp = requests.get(
                url,
                params={"crtfc_key": DART_API_KEY},
                timeout=30,
                stream=True,  # 본문을 다 받지 않고 헤더만 확인
            )
            elapsed = time.time() - t0
            ct = resp.headers.get("Content-Type", "?")[:40]
            _result(resp.status_code == 200, elapsed, f"{name} → HTTP {resp.status_code} ({ct})")
            resp.close()
            if resp.status_code != 200:
                all_ok = False
        except Exception as e:
            elapsed = time.time() - t0
            _result(False, elapsed, f"{name} → {type(e).__name__}: {e}")
            all_ok = False

    return all_ok


# ── 7. corpCode.xml ZIP 다운로드 테스트 ────────────────────────────────
def test_corp_code_download():
    _header("7. corpCode.xml ZIP 다운로드 (실제 데이터)")
    t0 = time.time()
    try:
        resp = requests.get(
            f"{DART_BASE_URL}/corpCode.xml",
            params={"crtfc_key": DART_API_KEY},
            timeout=(15, 180),
        )
        elapsed = time.time() - t0
        ct = resp.headers.get("Content-Type", "?")
        size = len(resp.content)
        size_mb = size / (1024 * 1024)

        print(f"  HTTP {resp.status_code} / Content-Type: {ct}")
        print(f"  응답 크기: {size_mb:.2f} MB")

        if resp.status_code != 200:
            _result(False, elapsed, f"HTTP {resp.status_code}")
            print(f"  응답 본문(처음 500자): {resp.text[:500]}")
            return False

        if "xml" in ct.lower() and size < 1000:
            # XML 에러 응답일 가능성
            print(f"  응답 본문: {resp.text[:500]}")
            _result(False, elapsed, "ZIP이 아닌 XML 에러 응답")
            return False

        if "zip" in ct.lower() or size > 100_000:
            _result(True, elapsed, f"ZIP 다운로드 성공 ({size_mb:.2f} MB)")
            return True

        _result(False, elapsed, f"예상치 못한 응답 형식: {ct}")
        print(f"  응답 본문(처음 500자): {resp.text[:500]}")
        return False

    except requests.ConnectionError as e:
        _result(False, time.time() - t0, f"ConnectionError: {e}")
        print("  → 네트워크 연결 문제")
        return False
    except requests.Timeout as e:
        _result(False, time.time() - t0, f"Timeout: {e}")
        print("  → 서버 응답 지연 (해외 서버에서 접속 시 흔함)")
        return False
    except Exception as e:
        _result(False, time.time() - t0, f"{type(e).__name__}: {e}")
        return False


# ── main ────────────────────────────────────────────────────────────────
def main():
    print("DART API 연결 진단 시작")
    print(f"대상 호스트: {DART_HOST}")
    print(f"BASE URL: {DART_BASE_URL}")

    tests = [
        ("API 키",          test_api_key),
        ("DNS",             test_dns),
        ("TCP",             test_tcp),
        ("TLS",             test_tls),
        ("API 키 유효성",   test_api_auth),
        ("엔드포인트 순회", test_all_endpoints),
        ("ZIP 다운로드",    test_corp_code_download),
    ]

    results = []
    for name, fn in tests:
        try:
            ok = fn()
        except Exception as e:
            print(f"  [예상치 못한 오류] {type(e).__name__}: {e}")
            ok = False
        results.append((name, ok))

    # 요약
    _header("진단 요약")
    for name, ok in results:
        tag = "\033[92mPASS\033[0m" if ok else "\033[91mFAIL\033[0m"
        print(f"  {name:20s} [{tag}]")

    failed = [name for name, ok in results if not ok]
    if not failed:
        print("\n  ✅ 모든 테스트 통과 — DART API 연결에 문제 없음")
        print("  → 오류가 간헐적이라면 DART 서버 부하 또는 일시적 네트워크 문제일 수 있음")
    else:
        print(f"\n  ❌ 실패 항목: {', '.join(failed)}")
        first_fail = failed[0]
        if first_fail == "API 키":
            print("  → .env 파일에 DART_API_KEY가 올바르게 설정되어 있는지 확인")
        elif first_fail == "DNS":
            print("  → DNS 해석 실패 — 인터넷 연결 또는 DNS 서버 문제")
        elif first_fail == "TCP":
            print("  → TCP 연결 실패 — 방화벽/프록시 차단 또는 DART 서버 다운")
        elif first_fail == "TLS":
            print("  → TLS 실패 — SSL 인증서 문제 또는 프록시 간섭")
        elif first_fail == "API 키 유효성":
            print("  → DART에는 연결되지만 API 키가 무효 — DART 사이트에서 키 상태 확인")
        elif first_fail == "ZIP 다운로드":
            print("  → 다른 API는 되는데 ZIP 다운로드만 실패 — 타임아웃 또는 서버 부하")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
