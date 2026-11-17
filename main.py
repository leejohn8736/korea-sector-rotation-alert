"""
예상 섹터 테마 & 수급 리포트 봇
- KRX Open API(유가증권/코스닥/ETF 일별매매정보)로 섹터별 거래대금 쏠림 계산
- Groq AI 요약 (실패해도 데이터 리포트는 반드시 전송)
- 텔레그램 전송
"""

import os
import sys
import json
import time
import datetime as dt
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "docs" / "data"
STATE_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

KST = dt.timezone(dt.timedelta(hours=9))

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"
KRX_ENDPOINTS = {
    "유가증권": f"{KRX_BASE}/sto/stk_bydd_trd",
    "코스닥": f"{KRX_BASE}/sto/ksq_bydd_trd",
    "ETF": f"{KRX_BASE}/etp/etf_bydd_trd",
}

# Groq 모델 (llama3-70b-8192 는 2025-08-30 종료됨). 필요하면 환경변수로 교체 가능
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

US_SECTOR_ETFS = {
    "기술(XLK)": "XLK", "금융(XLF)": "XLF", "에너지(XLE)": "XLE",
    "헬스케어(XLV)": "XLV", "산업재(XLI)": "XLI", "소비재(XLY)": "XLY",
    "필수소비재(XLP)": "XLP", "유틸리티(XLU)": "XLU", "소재(XLB)": "XLB",
    "부동산(XLRE)": "XLRE", "커뮤니케이션(XLC)": "XLC", "반도체(SOXX)": "SOXX",
}


# ------------------------------------------------------------ 1. 텔레그램
def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[텔레그램 경고] 토큰/챗ID 없음. 콘솔 출력으로 대체")
        print(text)
        return False

    for part in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data={"chat_id": chat, "text": part,
                          "disable_web_page_preview": "true"},
                    timeout=20,
                )
                if r.ok:
                    break
                print(f"텔레그램 전송 {attempt + 1}회 실패:", r.status_code, r.text[:150])
            except Exception as e:
                print(f"텔레그램 통신 에러 ({attempt + 1}회):", e)
            time.sleep(3)
        else:
            print("텔레그램 최종 전송 실패")
            return False
    return True


# ------------------------------------------------------------ 2. KRX 수집
def to_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def krx_fetch(url, key, base_date):
    """하루치 데이터. 실패/휴장이면 빈 리스트."""
    headers = {"AUTH_KEY": key}
    for method in ("get", "post"):
        try:
            if method == "get":
                r = requests.get(url, headers=headers, params={"basDd": base_date}, timeout=30)
            else:
                r = requests.post(url, headers=headers, json={"basDd": base_date}, timeout=30)
        except Exception as e:
            print(f"[KRX] {base_date} {method} 통신 예외: {e}")
            continue

        if r.status_code in (401, 403):
            print(f"[KRX] 인증/권한 오류 {r.status_code}: {r.text[:200]}")
            print("      → KRX Open API에서 해당 API '이용신청' 승인 여부를 확인하세요.")
            return None  # 날짜를 바꿔도 소용없음
        if not r.ok:
            print(f"[KRX] {base_date} {method} 실패 {r.status_code}: {r.text[:150]}")
            continue
        try:
            return r.json().get("OutBlock_1", []) or []
        except Exception as e:
            print("[KRX] JSON 파싱 실패:", e, r.text[:150])
    return []


def krx_latest_two_days(url, key):
    """가장 최근 영업일 2일치 (최신, 직전). 못 찾으면 (None, None)."""
    found = []
    day = dt.datetime.now(KST).date()
    for _ in range(12):
        if day.weekday() < 5:
            rows = krx_fetch(url, key, day.strftime("%Y%m%d"))
            if rows is None:  # 인증 오류
                return None, None
            if rows:
                found.append((day.strftime("%Y-%m-%d"), rows))
                if len(found) == 2:
                    break
        day -= dt.timedelta(days=1)
    if not found:
        return None, None
    return found[0], (found[1] if len(found) > 1 else None)


def sector_report(name, latest, prev):
    d1, rows1 = latest
    df = pd.DataFrame(rows1)
    for c in ("FLUC_RT", "ACC_TRDVAL"):
        df[c] = to_num(df[c]) if c in df else float("nan")
    df = df.dropna(subset=["ACC_TRDVAL"])
    lines = [f"■ {name} ({d1} 기준)"]

    if "SECT_TP_NM" in df.columns:  # 주식 시장: 업종별 집계
        df["SECT_TP_NM"] = df["SECT_TP_NM"].replace("", "기타").fillna("기타")
        g = df.groupby("SECT_TP_NM").apply(
            lambda x: pd.Series({
                "tv": x["ACC_TRDVAL"].sum(),
                "rt": (x["FLUC_RT"] * x["ACC_TRDVAL"]).sum() / max(x["ACC_TRDVAL"].sum(), 1),
            })
        )
        if prev:
            pdf = pd.DataFrame(prev[1])
            pdf["ACC_TRDVAL"] = to_num(pdf["ACC_TRDVAL"])
            pdf["SECT_TP_NM"] = pdf["SECT_TP_NM"].replace("", "기타").fillna("기타")
            g["prev_tv"] = pdf.groupby("SECT_TP_NM")["ACC_TRDVAL"].sum()
            g["chg"] = (g["tv"] / g["prev_tv"] - 1) * 100
        top = g.sort_values("tv", ascending=False).head(5)
        lines.append("· 거래대금 상위 업종")
        for sec, r in top.iterrows():
            extra = f", 전일비 {r['chg']:+.0f}%" if "chg" in r and pd.notna(r["chg"]) else ""
            lines.append(f"  - {sec}: {r['tv'] / 1e8:,.0f}억 (가중등락 {r['rt']:+.2f}%{extra})")
        if "chg" in g:
            surge = g.sort_values("tv", ascending=False).head(15).sort_values("chg", ascending=False).head(3)
            lines.append("· 거래대금 급증 업종(상위15 중)")
            for sec, r in surge.iterrows():
                lines.append(f"  - {sec}: 전일비 {r['chg']:+.0f}%")

    hot = df[df["ACC_TRDVAL"] >= 1e10].sort_values("FLUC_RT", ascending=False).head(5)  # 거래대금 100억+
    lines.append("· 상승률 상위(거래대금 100억+)")
    for _, r in hot.iterrows():
        lines.append(f"  - {r.get('ISU_NM', '?')}: {r['FLUC_RT']:+.2f}% / {r['ACC_TRDVAL'] / 1e8:,.0f}억")

    tv_top = df.sort_values("ACC_TRDVAL", ascending=False).head(5)
    lines.append("· 거래대금 상위 종목")
    for _, r in tv_top.iterrows():
        lines.append(f"  - {r.get('ISU_NM', '?')}: {r['ACC_TRDVAL'] / 1e8:,.0f}억 ({r['FLUC_RT']:+.2f}%)")
    return "\n".join(lines)


def collect_kr():
    key = (os.getenv("KRX_API_KEY") or "").strip()
    if not key:
        return "⚠️ KRX_API_KEY 미설정", False
    blocks, ok = [], False
    for name, url in KRX_ENDPOINTS.items():
        try:
            latest, prev = krx_latest_two_days(url, key)
            if not latest:
                blocks.append(f"■ {name}: 데이터 수신 실패 (로그 확인)")
                continue
            blocks.append(sector_report(name, latest, prev))
            ok = True
        except Exception as e:
            print(f"[KRX] {name} 처리 예외:", repr(e))
            blocks.append(f"■ {name}: 처리 중 오류 ({type(e).__name__})")
    return "\n\n".join(blocks), ok


# ------------------------------------------------------------ 3. 미국 섹터 (보조)
def collect_us():
    try:
        import yfinance as yf
        tickers = list(US_SECTOR_ETFS.values())
        px = yf.download(tickers, period="7d", progress=False, auto_adjust=True)["Close"].dropna(how="all")
        chg = (px.iloc[-1] / px.iloc[-2] - 1) * 100
        rows = sorted(((n, chg[t]) for n, t in US_SECTOR_ETFS.items() if t in chg), key=lambda x: -x[1])
        lines = [f"■ 미국 섹터 ETF ({px.index[-1].strftime('%Y-%m-%d')} 종가 기준)"]
        lines += [f"  - {n}: {v:+.2f}%" for n, v in rows]
        return "\n".join(lines)
    except Exception as e:
        print("[US] 수집 실패:", repr(e))
        return "■ 미국 섹터: 수집 실패"


# ------------------------------------------------------------ 4. Groq 요약
def analyze_with_groq(facts):
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None, "GROQ_API_KEY 미설정"

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content":
                "당신은 국내 주식 시장 전문 애널리스트입니다. 제공된 데이터에 있는 내용만 근거로, "
                "자금이 몰리는 섹터/테마와 주목할 점을 한국어로 4~5줄 요약하세요. "
                "데이터에 없는 수치나 종목은 지어내지 마세요."},
            {"role": "user", "content": facts[:6000]},
        ],
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = "알 수 없음"
    for attempt in range(3):
        try:
            print(f"🔄 Groq 호출 ({attempt + 1}/3) model={GROQ_MODEL}")
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              json=payload, headers=headers, timeout=40)
            if r.ok:
                return r.json()["choices"][0]["message"]["content"].strip(), None
            print(f"⚠️ Groq {r.status_code}: {r.text[:300]}")
            try:
                last_err = r.json().get("error", {}).get("message", r.text[:100])
            except Exception:
                last_err = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404):  # 재시도 무의미
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print("💥 Groq 예외:", last_err)
        time.sleep(5)
    return None, last_err[:150]


# ------------------------------------------------------------ 5. 메인
def run():
    markets = [m.upper() for m in sys.argv[1:]] or ["KR", "US"]
    now = dt.datetime.now(KST)
    print(f"🎯 타겟 마켓: {markets} / {now:%Y-%m-%d %H:%M KST}")

    kr_text, kr_ok, us_text = "", False, ""
    if "KR" in markets:
        kr_text, kr_ok = collect_kr()
    if "US" in markets:
        us_text = collect_us()

    facts = "\n\n".join(t for t in (kr_text, us_text) if t)

    summary, err = analyze_with_groq(facts) if facts else (None, "분석할 데이터 없음")

    msg = f"📊 [예상 섹터 테마 & 수급 리포트]\n⏰ 기준: {now:%Y-%m-%d %H:%M KST}\n\n"
    msg += facts + "\n\n"
    msg += "🤖 [Groq AI 요약]\n" + (summary if summary else f"⚠️ 요약 실패: {err}")

    print("전송 메시지:\n", msg)

    (DATA_DIR / "latest.json").write_text(
        json.dumps({"asof": now.isoformat(), "report": facts, "summary": summary},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    sent = send_telegram(msg)
    print("✅ 텔레그램 전송 성공" if sent else "❌ 텔레그램 전송 실패")
    if "KR" in markets and not kr_ok:
        sys.exit(1)  # Actions에서 실패로 표시되어 바로 알 수 있게


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("💥 치명적 에러:", repr(e))
        sys.exit(1)
