"""
예상 섹터 테마 & 수급 리포트 봇 (v3)
- KRX Open API(유가증권/코스닥/ETF 일별매매정보)로 시장 수급·테마별 거래대금 계산
- 모든 순위 리스트에 1,2,3… 번호 (상위 → 하위)
- Groq AI가 '테마별 거래대금 종목 / 유가증권 / 코스닥'의 테마·섹터를 각각 정리
- 리포트에 생성 시각(분 단위)과 데이터 기준일 명시, AI가 실패해도 데이터 리포트는 전송
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

# Groq production 텍스트 모델 (llama3-70b-8192, llama-3.3-70b-versatile 은 사용 불가 확인)
# GROQ_MODEL 환경변수로 덮어쓸 수 있음
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

TOP_N = 10        # 시장별 거래대금/상승률 상위 표시 개수
AI_TOP_N = 20     # AI에게 넘기는 시장별 거래대금 상위 개수
THEME_POOL = 200  # 테마 집계 대상: 거래대금 상위 N종목

US_SECTOR_ETFS = {
    "기술(XLK)": "XLK", "금융(XLF)": "XLF", "에너지(XLE)": "XLE",
    "헬스케어(XLV)": "XLV", "산업재(XLI)": "XLI", "경기소비재(XLY)": "XLY",
    "필수소비재(XLP)": "XLP", "유틸리티(XLU)": "XLU", "소재(XLB)": "XLB",
    "부동산(XLRE)": "XLRE", "커뮤니케이션(XLC)": "XLC", "반도체(SOXX)": "SOXX",
}

# 종목명 키워드 기반 테마 분류 (추정치. 자유롭게 추가/수정하세요)
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
def split_message(text, limit=3800):
    """줄 단위로 끊어서 텔레그램 길이 제한 내로 분할."""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # 한 줄이 너무 길면 강제 분할
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def send_telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[텔레그램 경고] 토큰/챗ID 없음. 콘솔 출력으로 대체")
        print(text)
        return False

    for part in split_message(text):
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
        time.sleep(0.5)
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


def theme_of(name):
    """종목명 → 테마 (없으면 '')."""
    name = str(name)
    for th, kws in THEMES.items():
        if any(k in name for k in kws):
            return th
    return ""


def make_frame(rows):
    df = pd.DataFrame(rows)
    for c in ("FLUC_RT", "ACC_TRDVAL"):
        df[c] = to_num(df[c]) if c in df else float("nan")
    if "ISU_NM" not in df:
        df["ISU_NM"] = "?"
    df = df.dropna(subset=["ACC_TRDVAL"]).copy()
    df["theme"] = df["ISU_NM"].map(theme_of)
    return df


def eok(v):
    return f"{v / 1e8:,.0f}억"


def fmt_rt(v):
    return "N/A" if pd.isna(v) else f"{v:+.2f}%"


# ------------------------------------------------------------ 3. 리포트 블록
def market_block(name, m):
    """시장별 (표시 텍스트, AI 전달용 텍스트)."""
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

    show_theme = name != "ETF"
    lines = [f"■ {name} (데이터 기준일 {m['date']})", f"· {head}"]

    if name == "ETF":
        nm = df["ISU_NM"].astype(str)
        inv = df[nm.str.contains("인버스")]["ACC_TRDVAL"].sum()
        lev = df[nm.str.contains("레버리지")]["ACC_TRDVAL"].sum()
        lines.append(f"· 인버스 거래대금 {eok(inv)} ({inv / max(tv, 1) * 100:.0f}%), "
                     f"레버리지 {eok(lev)} ({lev / max(tv, 1) * 100:.0f}%)")

    def tag(r):
        return f" [{r['theme']}]" if show_theme and r["theme"] else ""

    lines.append(f"[거래대금 상위 TOP {TOP_N}]")
    tv_top = df.sort_values("ACC_TRDVAL", ascending=False).head(TOP_N)
    for i, (_, r) in enumerate(tv_top.iterrows(), 1):
        lines.append(f"{i}. {r['ISU_NM']}{tag(r)} - {eok(r['ACC_TRDVAL'])} ({fmt_rt(r['FLUC_RT'])})")

    lines.append(f"[상승률 상위 TOP {TOP_N}] (거래대금 100억+)")
    hot = df[df["ACC_TRDVAL"] >= 1e10].sort_values("FLUC_RT", ascending=False).head(TOP_N)
    for i, (_, r) in enumerate(hot.iterrows(), 1):
        lines.append(f"{i}. {r['ISU_NM']}{tag(r)} - {fmt_rt(r['FLUC_RT'])} / {eok(r['ACC_TRDVAL'])}")

    # AI 전달용 (압축)
    ai = [f"## {name} (기준일 {m['date']})", head, f"[거래대금 상위 {AI_TOP_N}] 순위|종목|거래대금|등락|키워드테마"]
    for i, (_, r) in enumerate(df.sort_values("ACC_TRDVAL", ascending=False).head(AI_TOP_N).iterrows(), 1):
        ai.append(f"{i}|{r['ISU_NM']}|{eok(r['ACC_TRDVAL'])}|{fmt_rt(r['FLUC_RT'])}|{r['theme'] or '-'}")
    ai.append("[상승률 상위 5(거래대금 100억+)]")
    for i, (_, r) in enumerate(hot.head(5).iterrows(), 1):
        ai.append(f"{i}|{r['ISU_NM']}|{fmt_rt(r['FLUC_RT'])}|{eok(r['ACC_TRDVAL'])}")
    return "\n".join(lines), "\n".join(ai)


def theme_block(stock_frames):
    """유가증권+코스닥 거래대금 상위 종목을 키워드로 테마 분류 → 거래대금 순 번호."""
    df = pd.concat(stock_frames, ignore_index=True)
    df = df.sort_values("ACC_TRDVAL", ascending=False).head(THEME_POOL)
    pool_tv = df["ACC_TRDVAL"].sum()
    d = df[df["theme"] != ""]
    if d.empty:
        return "■ 테마별 거래대금: 분류된 종목 없음", "## 테마별 거래대금: 분류된 종목 없음"

    rows = []
    for th, g in d.groupby("theme"):
        tv = g["ACC_TRDVAL"].sum()
        rt = (g["FLUC_RT"].fillna(0) * g["ACC_TRDVAL"]).sum() / max(tv, 1)
        members = g.sort_values("ACC_TRDVAL", ascending=False)
        rows.append((th, tv, rt, len(g), members))
    rows.sort(key=lambda x: -x[1])

    classified = d["ACC_TRDVAL"].sum()
    lines = [f"■ 테마별 거래대금 순위 (유가증권+코스닥 거래대금 상위 {THEME_POOL}종목, 키워드 분류 추정)",
             f"· 분류 종목 거래대금 {eok(classified)} / 미분류 {eok(pool_tv - classified)}"]
    ai = ["## 테마별 거래대금 (키워드 분류 추정) 순위|테마|거래대금|가중등락|종목수|대표종목(등락)"]
    for i, (th, tv, rt, n, mem) in enumerate(rows, 1):
        names = ", ".join(f"{r['ISU_NM']}({fmt_rt(r['FLUC_RT'])})" for _, r in mem.head(5).iterrows())
        lines.append(f"{i}. {th} - {eok(tv)} / 가중등락 {rt:+.2f}% / {n}종목")
        lines.append(f"   └ {names}")
        ai.append(f"{i}|{th}|{eok(tv)}|{rt:+.2f}%|{n}|{names}")
    return "\n".join(lines), "\n".join(ai)


def collect_kr():
    """(표시 텍스트, AI 전달 텍스트, 기준일 리스트, 성공 여부)"""
    key = (os.getenv("KRX_API_KEY") or "").strip()
    if not key:
        return "⚠️ KRX_API_KEY 미설정", "", [], False

    shows, ais, dates, stock_frames, ok = [], [], [], [], False
    for name, url in KRX_ENDPOINTS.items():
        try:
            latest, prev = krx_latest_two_days(url, key)
            if not latest:
                shows.append(f"■ {name}: 데이터 수신 실패 (로그 확인)")
                continue
            m = {"date": latest[0], "df": make_frame(latest[1]),
                 "prev": make_frame(prev[1]) if prev else None}
            dates.append(latest[0])
            if name in ("유가증권", "코스닥"):
                stock_frames.append(m["df"])
            s, a = market_block(name, m)
            shows.append(s)
            ais.append(a)
            ok = True
        except Exception as e:
            print(f"[KRX] {name} 처리 예외:", repr(e))
            shows.append(f"■ {name}: 처리 중 오류 ({type(e).__name__})")

    if stock_frames:
        ts, ta = theme_block(stock_frames)
        shows.insert(0, ts)   # 테마 순위를 맨 앞에
        ais.insert(0, ta)
    return "\n\n".join(shows), "\n\n".join(ais), dates, ok


# ------------------------------------------------------------ 4. 미국 섹터 (보조)
def collect_us():
    try:
        import yfinance as yf
        tickers = list(US_SECTOR_ETFS.values())
        px = yf.download(tickers, period="7d", progress=False, auto_adjust=True)["Close"].dropna(how="all")
        chg = (px.iloc[-1] / px.iloc[-2] - 1) * 100
        rows = sorted(((n, chg[t]) for n, t in US_SECTOR_ETFS.items() if t in chg), key=lambda x: -x[1])
        d = px.index[-1].strftime("%Y-%m-%d")
        lines = [f"■ 미국 섹터 ETF 등락률 순위 (데이터 기준일 {d} 종가)"]
        lines += [f"{i}. {n}: {v:+.2f}%" for i, (n, v) in enumerate(rows, 1)]
        return "\n".join(lines), d
    except Exception as e:
        print("[US] 수집 실패:", repr(e))
        return "■ 미국 섹터: 수집 실패", None


# ------------------------------------------------------------ 5. Groq 요약
SYSTEM_PROMPT = (
    "당신은 국내 주식 시장 전문 애널리스트입니다. 제공된 데이터에 있는 종목·수치만 근거로 한국어로 답하세요. "
    "데이터에 없는 수치·뉴스는 지어내지 마세요. '키워드테마'는 종목명 기반 추정이므로 틀리면 보정하세요. "
    "'섹터'는 업종(예: 반도체, 전기전자, 화학, 제약바이오, 금융, 운송, 건설, 음식료 등)이고 "
    "'테마'는 시장이 묶어 보는 이슈(예: AI반도체, 인버스, 방산, 2차전지 등)입니다. "
    "종목의 업종은 종목명과 일반 상식으로 판단하되 확신이 없으면 '(추정)'이라고 표시하세요.\n\n"
    "반드시 아래 5개 섹션 제목을 그대로 사용하고, 각 섹션은 짧은 번호 목록으로 쓰세요:\n"
    "[1] 총평 (2~3줄: 시장 분위기, 거래대금·등락·인버스 비중, 미국 연결)\n"
    "[2] 테마별 거래대금 종목 정리 (거래대금 순으로 테마마다: 테마명 — 핵심 종목들 — 섹터 — 한 줄 해석)\n"
    "[3] 유가증권 테마·섹터 정리 (거래대금 상위 종목을 테마/섹터로 묶어 순위 순으로, 종목 포함)\n"
    "[4] 코스닥 테마·섹터 정리 (같은 방식, 급등 종목의 테마 포함)\n"
    "[5] 다음 거래일 체크포인트 (1~3개)"
)


def analyze_with_groq(facts, asof_line):
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None, "GROQ_API_KEY 미설정", None

    models = list(dict.fromkeys(m for m in [os.getenv("GROQ_MODEL")] + GROQ_MODELS if m))
    user = f"{asof_line}\n\n{facts[:7000]}"

    err_last = "알 수 없음"
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_completion_tokens": 3500,
            "reasoning_effort": "low",
        }
        for attempt in range(3):
            try:
                print(f"🔄 Groq 호출 model={model} ({attempt + 1}/3)")
                r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                  json=payload,
                                  headers={"Authorization": f"Bearer {api_key}",
                                           "Content-Type": "application/json"},
                                  timeout=90)
                if r.ok:
                    content = (r.json()["choices"][0]["message"].get("content") or "").strip()
                    if content:
                        return content, None, model
                    err_last = f"{model}: 빈 응답"
                    payload["max_completion_tokens"] = 6000
                    continue

                print(f"⚠️ Groq {r.status_code}: {r.text[:300]}")
                try:
                    err_last = r.json().get("error", {}).get("message", r.text[:120])
                except Exception:
                    err_last = f"HTTP {r.status_code}"

                if r.status_code == 400 and "reasoning" in r.text.lower() and "reasoning_effort" in payload:
                    payload.pop("reasoning_effort")
                    continue
                if r.status_code == 413 or (r.status_code == 400 and "too large" in r.text.lower()):
                    payload["messages"][1]["content"] = user[:4000]  # 입력 줄여 재시도
                    continue
                if r.status_code == 429:
                    wait = 15
                    try:
                        wait = min(60, int(float(r.headers.get("retry-after", wait))) + 2)
                    except Exception:
                        pass
                    time.sleep(wait)
                    continue
                if r.status_code in (400, 401, 403, 404):
                    break
            except Exception as e:
                err_last = f"{type(e).__name__}: {e}"
                print("💥 Groq 예외:", err_last)
            time.sleep(4)
    return None, err_last[:200], None


# ------------------------------------------------------------ 6. 메인
def run():
    markets = [m.upper() for m in sys.argv[1:]] or ["KR", "US"]
    now = dt.datetime.now(KST)
    now_s = now.strftime("%Y-%m-%d %H:%M KST")
    print(f"🎯 타겟 마켓: {markets} / {now_s}")

    kr_show, kr_ai, kr_dates, kr_ok, us_text, us_date = "", "", [], False, "", None
    if "KR" in markets:
        kr_show, kr_ai, kr_dates, kr_ok = collect_kr()
    if "US" in markets:
        us_text, us_date = collect_us()

    facts_show = "\n\n".join(t for t in (kr_show, us_text) if t)
    facts_ai = "\n\n".join(t for t in (kr_ai, us_text) if t)

    kr_date = max(kr_dates) if kr_dates else "없음"
    asof_line = (f"[기준] 리포트 생성 {now_s} / 국내 데이터 기준일 {kr_date} (최근 영업일 종가) / "
                 f"미국 데이터 기준일 {us_date or '없음'} 종가")

    if facts_ai:
        summary, err, used = analyze_with_groq(facts_ai, asof_line)
    else:
        summary, err, used = None, "분석할 데이터 없음", None

    msg = "📊 [예상 섹터 테마 & 수급 리포트]\n"
    msg += f"⏰ 리포트 생성: {now_s}\n"
    msg += f"📅 국내 데이터 기준일: {kr_date} (최근 영업일 종가) / 미국: {us_date or '없음'} 종가\n\n"
    msg += facts_show + "\n\n"
    msg += f"🤖 [AI 섹터·테마 요약] ({now:%H:%M} 생성, 데이터 {kr_date} 기준"
    msg += f", {used})\n" if used else ")\n"
    msg += summary if summary else f"⚠️ 요약 실패: {err}"

    print("전송 메시지:\n", msg)

    (DATA_DIR / "latest.json").write_text(
        json.dumps({"generated": now.isoformat(), "kr_date": kr_date, "us_date": us_date,
                    "report": facts_show, "summary": summary, "model": used},
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
