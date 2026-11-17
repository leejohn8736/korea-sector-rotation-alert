"""
예상 섹터 테마 & 수급 리포트 봇 (v2)
- KRX Open API(유가증권/코스닥/ETF 일별매매정보)로 시장 수급·테마별 거래대금 쏠림 계산
- Groq AI가 테마/자금 흐름을 해석 (실패해도 데이터 리포트는 전송)
- 리포트에 '생성 시각'과 '데이터 기준일'을 분 단위까지 명시
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

# Groq: llama3-70b-8192(2025-08 종료), llama-3.3-70b-versatile(접근 불가 확인)
# → 현재 production 텍스트 모델인 GPT-OSS 계열 사용. GROQ_MODEL 환경변수로 덮어쓰기 가능
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

US_SECTOR_ETFS = {
    "기술(XLK)": "XLK", "금융(XLF)": "XLF", "에너지(XLE)": "XLE",
    "헬스케어(XLV)": "XLV", "산업재(XLI)": "XLI", "경기소비재(XLY)": "XLY",
    "필수소비재(XLP)": "XLP", "유틸리티(XLU)": "XLU", "소재(XLB)": "XLB",
    "부동산(XLRE)": "XLRE", "커뮤니케이션(XLC)": "XLC", "반도체(SOXX)": "SOXX",
}

# 종목명 키워드 기반 테마 분류 (추정치. 필요하면 자유롭게 추가/수정)
THEMES = {
    "반도체": ["하이닉스", "삼성전자", "반도체", "DB하이텍", "한미반도체", "주성엔지니어링",
              "리노공업", "이오테크닉스", "원익", "심텍", "삼성전기", "SK스퀘어", "테크윙",
              "HPSP", "에스티아이", "피에스케이", "솔브레인", "동진쎄미켐", "ISC", "두산테스나",
              "영광와이케이엠씨", "와이케이엠씨", "하나마이크론", "네패스", "티씨케이"],
    "2차전지": ["에너지솔루션", "삼성SDI", "에코프로", "포스코퓨처엠", "엘앤에프", "배터리",
              "SK이노베이션", "코스모신소재", "천보", "엔켐", "SKC", "이차전지"],
    "바이오/제약": ["바이오", "제약", "셀트리온", "알테오젠", "유한양행", "한미약품", "HLB",
                "리가켐", "에이비엘", "오스코텍", "메디", "헬스케어", "파마", "랩"],
    "방산/조선": ["한화에어로", "LIG넥스원", "한국항공우주", "현대로템", "HD현대", "삼성중공업",
              "한화오션", "조선", "풍산", "STX", "방산"],
    "자동차": ["현대차", "현대모비스", "기아", "현대위아", "만도", "한온", "자동차"],
    "금융/증권": ["금융", "증권", "은행", "보험", "KB금융", "신한지주", "하나금융", "우리금융",
              "미래에셋", "키움", "메리츠", "손해보험", "화재", "벤처투자", "인베스트"],
    "AI/소프트웨어": ["네이버", "NAVER", "카카오", "더존", "파수", "솔트룩스", "AI", "소프트", "클라우드"],
    "엔터/미디어": ["하이브", "JYP", "에스엠", "와이지", "CJ ENM", "스튜디오드래곤", "엔터"],
    "원전/전력기기": ["두산에너빌리티", "한전", "LS ELECTRIC", "효성중공업", "HD현대일렉트릭",
                 "일진전기", "전선", "원자력", "LS"],
    "음식료/소비": ["삼양식품", "농심", "오리온", "CJ제일제당", "백화점", "신세계", "롯데", "화장품"],
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
    """하루치 데이터. 휴장/미제공이면 빈 리스트, 인증 오류면 None."""
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
            return None
        if not r.ok:
            print(f"[KRX] {base_date} {method} 실패 {r.status_code}: {r.text[:150]}")
            continue
        try:
            return r.json().get("OutBlock_1", []) or []
        except Exception as e:
            print("[KRX] JSON 파싱 실패:", e, r.text[:150])
    return []


def krx_latest_two_days(url, key):
    found = []
    day = dt.datetime.now(KST).date()
    for _ in range(12):
        if day.weekday() < 5:
            rows = krx_fetch(url, key, day.strftime("%Y%m%d"))
            if rows is None:
                return None, None
            if rows:
                found.append((day.strftime("%Y-%m-%d"), rows))
                if len(found) == 2:
                    break
        day -= dt.timedelta(days=1)
    if not found:
        return None, None
    return found[0], (found[1] if len(found) > 1 else None)


def make_frame(rows):
    df = pd.DataFrame(rows)
    for c in ("FLUC_RT", "ACC_TRDVAL"):
        df[c] = to_num(df[c]) if c in df else float("nan")
    if "ISU_NM" not in df:
        df["ISU_NM"] = "?"
    return df.dropna(subset=["ACC_TRDVAL"])


def eok(v):
    return f"{v / 1e8:,.0f}억"


def market_block(name, m):
    """시장별 요약 텍스트."""
    df = m["df"]
    tv = df["ACC_TRDVAL"].sum()
    w_rt = (df["FLUC_RT"].fillna(0) * df["ACC_TRDVAL"]).sum() / max(tv, 1)
    up = int((df["FLUC_RT"] > 0).sum())
    down = int((df["FLUC_RT"] < 0).sum())
    flat = len(df) - up - down

    head = f"총 거래대금 {eok(tv)}"
    if m["prev"] is not None:
        ptv = m["prev"]["ACC_TRDVAL"].sum()
        if ptv:
            head += f" (전일비 {(tv / ptv - 1) * 100:+.0f}%)"
    head += f" / 거래대금가중 등락 {w_rt:+.2f}% / 상승 {up}·하락 {down}·보합 {flat}"

    lines = [f"■ {name} (데이터 기준일 {m['date']})", f"· {head}"]

    if name == "ETF":
        nm = df["ISU_NM"].astype(str)
        inv = df[nm.str.contains("인버스")]["ACC_TRDVAL"].sum()
        lev = df[nm.str.contains("레버리지")]["ACC_TRDVAL"].sum()
        lines.append(f"· 인버스 거래대금 {eok(inv)} ({inv / max(tv, 1) * 100:.0f}%), "
                     f"레버리지 {eok(lev)} ({lev / max(tv, 1) * 100:.0f}%)")

    hot = df[df["ACC_TRDVAL"] >= 1e10].sort_values("FLUC_RT", ascending=False).head(5)
    lines.append("· 상승률 상위(거래대금 100억+)")
    for _, r in hot.iterrows():
        lines.append(f"  - {r['ISU_NM']}: {r['FLUC_RT']:+.2f}% / {eok(r['ACC_TRDVAL'])}")

    lines.append("· 거래대금 상위")
    for _, r in df.sort_values("ACC_TRDVAL", ascending=False).head(5).iterrows():
        lines.append(f"  - {r['ISU_NM']}: {eok(r['ACC_TRDVAL'])} ({r['FLUC_RT']:+.2f}%)")
    return "\n".join(lines)


def theme_block(stock_frames):
    """유가증권+코스닥 거래대금 상위 종목을 키워드로 테마 분류해 집계(추정)."""
    df = pd.concat(stock_frames, ignore_index=True)
    df = df.sort_values("ACC_TRDVAL", ascending=False).head(200).copy()

    def tag(name):
        for th, kws in THEMES.items():
            if any(k in str(name) for k in kws):
                return th
        return None

    df["theme"] = df["ISU_NM"].map(tag)
    d = df.dropna(subset=["theme"])
    if d.empty:
        return "· (테마 분류된 종목 없음)"

    rows = []
    for th, g in d.groupby("theme"):
        tv = g["ACC_TRDVAL"].sum()
        rt = (g["FLUC_RT"].fillna(0) * g["ACC_TRDVAL"]).sum() / max(tv, 1)
        names = ", ".join(g.sort_values("ACC_TRDVAL", ascending=False)["ISU_NM"].head(3))
        rows.append((th, tv, rt, len(g), names))
    rows.sort(key=lambda x: -x[1])

    lines = ["■ 테마별 거래대금 (유가증권+코스닥 거래대금 상위 200종목 중 키워드 분류, 추정)"]
    for th, tv, rt, n, names in rows:
        lines.append(f"  - {th}: {eok(tv)} / 가중등락 {rt:+.2f}% / {n}종목 ({names})")
    return "\n".join(lines)


def collect_kr():
    key = (os.getenv("KRX_API_KEY") or "").strip()
    if not key:
        return "⚠️ KRX_API_KEY 미설정", [], False
    blocks, dates, stock_frames, ok = [], [], [], False
    for name, url in KRX_ENDPOINTS.items():
        try:
            latest, prev = krx_latest_two_days(url, key)
            if not latest:
                blocks.append(f"■ {name}: 데이터 수신 실패 (로그 확인)")
                continue
            m = {"date": latest[0], "df": make_frame(latest[1]),
                 "prev": make_frame(prev[1]) if prev else None}
            dates.append(latest[0])
            if name in ("유가증권", "코스닥"):
                stock_frames.append(m["df"])
            blocks.append(market_block(name, m))
            ok = True
        except Exception as e:
            print(f"[KRX] {name} 처리 예외:", repr(e))
            blocks.append(f"■ {name}: 처리 중 오류 ({type(e).__name__})")
    if stock_frames:
        blocks.insert(0, theme_block(stock_frames))
    return "\n\n".join(blocks), dates, ok


# ------------------------------------------------------------ 3. 미국 섹터 (보조)
def collect_us():
    try:
        import yfinance as yf
        tickers = list(US_SECTOR_ETFS.values())
        px = yf.download(tickers, period="7d", progress=False, auto_adjust=True)["Close"].dropna(how="all")
        chg = (px.iloc[-1] / px.iloc[-2] - 1) * 100
        rows = sorted(((n, chg[t]) for n, t in US_SECTOR_ETFS.items() if t in chg), key=lambda x: -x[1])
        d = px.index[-1].strftime("%Y-%m-%d")
        lines = [f"■ 미국 섹터 ETF (데이터 기준일 {d} 종가)"]
        lines += [f"  - {n}: {v:+.2f}%" for n, v in rows]
        return "\n".join(lines), d
    except Exception as e:
        print("[US] 수집 실패:", repr(e))
        return "■ 미국 섹터: 수집 실패", None


# ------------------------------------------------------------ 4. Groq 요약
def analyze_with_groq(facts, asof_line):
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None, "GROQ_API_KEY 미설정", None

    models = [m for m in [os.getenv("GROQ_MODEL")] + GROQ_MODELS if m]
    models = list(dict.fromkeys(models))

    system = (
        "당신은 국내 주식 시장 전문 애널리스트입니다. 아래 제공된 데이터에 있는 수치만 근거로 한국어로 답하세요. "
        "데이터에 없는 수치·종목·뉴스는 지어내지 마세요. 테마 분류는 키워드 기반 추정이므로 필요하면 보정해도 됩니다. "
        "형식(총 8줄 이내, 각 줄 짧게):\n"
        "1) 한줄 총평(시장 분위기, 등락/거래대금/인버스 비중)\n"
        "2) 자금이 몰린 섹터·테마와 그 근거(종목·수치)\n"
        "3) 자금이 빠진/약세 섹터·테마\n"
        "4) 눈에 띄는 개별 급등 테마(코스닥 상승률 상위 등)\n"
        "5) 미국 섹터 흐름과 국내 연결\n"
        "6) 다음 거래일 체크포인트 1~2개"
    )
    user = f"{asof_line}\n\n{facts[:5500]}"

    err_last = "알 수 없음"
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_completion_tokens": 1800,
            "reasoning_effort": "low",
        }
        for attempt in range(3):
            try:
                print(f"🔄 Groq 호출 model={model} ({attempt + 1}/3)")
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  json=payload, headers={"Authorization": f"Bearer {api_key}",
                                                         "Content-Type": "application/json"},
                                  timeout=60)
                if r.ok:
                    content = (r.json()["choices"][0]["message"].get("content") or "").strip()
                    if content:
                        return content, None, model
                    err_last = f"{model}: 빈 응답"
                    payload["max_completion_tokens"] = 3000
                    continue

                print(f"⚠️ Groq {r.status_code}: {r.text[:300]}")
                try:
                    err_last = r.json().get("error", {}).get("message", r.text[:120])
                except Exception:
                    err_last = f"HTTP {r.status_code}"

                if r.status_code == 400 and "reasoning" in r.text.lower() and "reasoning_effort" in payload:
                    payload.pop("reasoning_effort")  # 모델이 미지원이면 빼고 재시도
                    continue
                if r.status_code == 429:
                    time.sleep(12)
                    continue
                if r.status_code in (400, 401, 403, 404):
                    break  # 같은 모델 재시도 무의미 → 다음 모델
            except Exception as e:
                err_last = f"{type(e).__name__}: {e}"
                print("💥 Groq 예외:", err_last)
            time.sleep(4)
    return None, err_last[:200], None


# ------------------------------------------------------------ 5. 메인
def run():
    markets = [m.upper() for m in sys.argv[1:]] or ["KR", "US"]
    now = dt.datetime.now(KST)
    now_s = now.strftime("%Y-%m-%d %H:%M KST")
    print(f"🎯 타겟 마켓: {markets} / {now_s}")

    kr_text, kr_dates, kr_ok, us_text, us_date = "", [], False, "", None
    if "KR" in markets:
        kr_text, kr_dates, kr_ok = collect_kr()
    if "US" in markets:
        us_text, us_date = collect_us()

    facts = "\n\n".join(t for t in (kr_text, us_text) if t)

    kr_date = max(kr_dates) if kr_dates else "없음"
    asof_line = (f"[기준] 리포트 생성 {now_s} / 국내 데이터 기준일 {kr_date} (최근 영업일 종가) / "
                 f"미국 데이터 기준일 {us_date or '없음'} 종가")

    summary, err, used = analyze_with_groq(facts, asof_line) if facts else (None, "분석할 데이터 없음", None)

    msg = "📊 [예상 섹터 테마 & 수급 리포트]\n"
    msg += f"⏰ 리포트 생성: {now_s}\n"
    msg += f"📅 국내 데이터 기준일: {kr_date} (최근 영업일 종가) / 미국: {us_date or '없음'} 종가\n\n"
    msg += facts + "\n\n"
    msg += f"🤖 [AI 섹터·테마 요약] ({now:%H:%M} 생성, 데이터 {kr_date} 기준"
    msg += f", {used})\n" if used else ")\n"
    msg += summary if summary else f"⚠️ 요약 실패: {err}"

    print("전송 메시지:\n", msg)

    (DATA_DIR / "latest.json").write_text(
        json.dumps({"generated": now.isoformat(), "kr_date": kr_date, "us_date": us_date,
                    "report": facts, "summary": summary, "model": used},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    sent = send_telegram(msg)
    print("✅ 텔레그램 전송 성공" if sent else "❌ 텔레그램 전송 실패")
    if "KR" in markets and not kr_ok:
        sys.exit(1)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("💥 치명적 에러:", repr(e))
        sys.exit(1)
