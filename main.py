"""
예상 섹터 테마 & 수급 리포트 봇 (v4)
- KRX Open API(유가증권/코스닥/ETF 일별매매정보) 기반 수급·테마 분석
- 모든 순위 리스트 번호(상위→하위), 제목마다 아이콘
- 오늘 vs 전일 테마 순위 변동 / 거래대금 유입·이탈(순환) 자동 계산, 최근 며칠 이력은 state/ 에 저장
- Groq AI 2회 호출: (A) 총평·테마/섹터 정리  (B) 전일 리뷰·예상 테마·순환 섹터·정리 요약
- AI가 실패해도 데이터 리포트는 전송
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
HISTORY_FILE = STATE_DIR / "theme_history.json"

KST = dt.timezone(dt.timedelta(hours=9))

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"
KRX_ENDPOINTS = {
    "유가증권": f"{KRX_BASE}/sto/stk_bydd_trd",
    "코스닥": f"{KRX_BASE}/sto/ksq_bydd_trd",
    "ETF": f"{KRX_BASE}/etp/etf_bydd_trd",
}
ICONS = {"유가증권": "🏢", "코스닥": "🚀", "ETF": "📦"}

# Groq production 텍스트 모델 (llama3-70b-8192, llama-3.3-70b-versatile 은 사용 불가 확인)
# GROQ_MODEL 환경변수로 덮어쓸 수 있음
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

TOP_N = 10          # 시장별 상위 표시 개수
AI_TOP_N = 20       # AI에게 넘기는 시장별 거래대금 상위 개수
THEME_POOL = 200    # 테마 집계 대상: 거래대금 상위 N종목
HISTORY_DAYS = 10   # 테마 이력 보관 일수

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
        while len(line) > limit:
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


# ------------------------------------------------------------ 3. 시장 블록
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

    icon = ICONS.get(name, "▪️")
    show_theme = name != "ETF"
    lines = [f"{icon} {name} (데이터 기준일 {m['date']})", f"· {head}"]

    if name == "ETF":
        nm = df["ISU_NM"].astype(str)
        inv = df[nm.str.contains("인버스")]["ACC_TRDVAL"].sum()
        lev = df[nm.str.contains("레버리지")]["ACC_TRDVAL"].sum()
        lines.append(f"· 🐻 인버스 {eok(inv)} ({inv / max(tv, 1) * 100:.0f}%) / "
                     f"🐂 레버리지 {eok(lev)} ({lev / max(tv, 1) * 100:.0f}%)")

    def tag(r):
        return f" [{r['theme']}]" if show_theme and r["theme"] else ""

    lines.append(f"💰 거래대금 상위 TOP {TOP_N}")
    tv_top = df.sort_values("ACC_TRDVAL", ascending=False).head(TOP_N)
    for i, (_, r) in enumerate(tv_top.iterrows(), 1):
        lines.append(f"{i}. {r['ISU_NM']}{tag(r)} - {eok(r['ACC_TRDVAL'])} ({fmt_rt(r['FLUC_RT'])})")

    lines.append(f"📈 상승률 상위 TOP {TOP_N} (거래대금 100억+)")
    hot = df[df["ACC_TRDVAL"] >= 1e10].sort_values("FLUC_RT", ascending=False).head(TOP_N)
    for i, (_, r) in enumerate(hot.iterrows(), 1):
        lines.append(f"{i}. {r['ISU_NM']}{tag(r)} - {fmt_rt(r['FLUC_RT'])} / {eok(r['ACC_TRDVAL'])}")

    ai = [f"## {name} (기준일 {m['date']})", head,
          f"[거래대금 상위 {AI_TOP_N}] 순위|종목|거래대금|등락|키워드테마"]
    for i, (_, r) in enumerate(df.sort_values("ACC_TRDVAL", ascending=False).head(AI_TOP_N).iterrows(), 1):
        ai.append(f"{i}|{r['ISU_NM']}|{eok(r['ACC_TRDVAL'])}|{fmt_rt(r['FLUC_RT'])}|{r['theme'] or '-'}")
    ai.append("[상승률 상위 5(거래대금 100억+)]")
    for i, (_, r) in enumerate(hot.head(5).iterrows(), 1):
        ai.append(f"{i}|{r['ISU_NM']}|{fmt_rt(r['FLUC_RT'])}|{eok(r['ACC_TRDVAL'])}")
    return "\n".join(lines), "\n".join(ai)


# ------------------------------------------------------------ 4. 테마 집계 / 이력 / 순환
def theme_rows(frames):
    """거래대금 상위 THEME_POOL 종목을 테마별 집계 → ({테마: 정보}, 풀 총거래대금)."""
    if not frames:
        return {}, 0
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("ACC_TRDVAL", ascending=False).head(THEME_POOL)
    pool_tv = df["ACC_TRDVAL"].sum()
    rows = {}
    for th, g in df[df["theme"] != ""].groupby("theme"):
        tv = g["ACC_TRDVAL"].sum()
        rows[th] = {
            "tv": tv,
            "rt": (g["FLUC_RT"].fillna(0) * g["ACC_TRDVAL"]).sum() / max(tv, 1),
            "n": len(g),
            "mem": g.sort_values("ACC_TRDVAL", ascending=False),
        }
    return rows, pool_tv


def ranked(rows):
    return sorted(rows.items(), key=lambda kv: -kv[1]["tv"])


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(hist, date_rows):
    """date_rows: {날짜: {테마: [거래대금(억), 가중등락]}}. 최근 HISTORY_DAYS 일만 유지."""
    try:
        for d, themes in date_rows.items():
            if themes:
                hist[d] = themes
        for d in sorted(hist)[:-HISTORY_DAYS]:
            hist.pop(d, None)
        HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print("[이력] 저장 실패:", repr(e))


def history_text(hist, exclude=()):
    """최근 이력(최신순)을 날짜별 테마 TOP3로 요약."""
    lines = []
    for d in sorted(hist, reverse=True):
        if d in exclude:
            continue
        top = sorted(hist[d].items(), key=lambda kv: -kv[1][0])[:3]
        lines.append(f"{d}: " + ", ".join(f"{t}({v[0]:,.0f}억, {v[1]:+.1f}%)" for t, v in top))
        if len(lines) >= 5:
            break
    return "\n".join(lines)


def theme_block(today_frames, prev_frames, date, prev_date, hist):
    """(표시 텍스트, AI 전달용 현재 테마, AI 전달용 순환/이력)"""
    cur, pool_tv = theme_rows(today_frames)
    prv, _ = theme_rows(prev_frames)
    if not cur:
        msg = "🔥 테마별 거래대금: 분류된 종목 없음"
        return msg, "## 테마별 거래대금: 분류된 종목 없음", ""

    cur_rank = ranked(cur)
    prv_rank = ranked(prv)
    prv_pos = {th: i for i, (th, _) in enumerate(prv_rank, 1)}
    classified = sum(v["tv"] for v in cur.values())

    lines = [f"🔥 테마별 거래대금 순위 ({date} 기준)",
             f"· 유가증권+코스닥 거래대금 상위 {THEME_POOL}종목을 키워드로 분류한 추정치",
             f"· 분류 {eok(classified)} / 미분류 {eok(pool_tv - classified)}"]
    ai_cur = ["## 오늘 테마별 거래대금(키워드 분류 추정) 순위|테마|거래대금|전일비|순위변동|가중등락|종목수|대표종목(등락)"]

    for i, (th, v) in enumerate(cur_rank, 1):
        p = prv.get(th)
        chg = f"{(v['tv'] / p['tv'] - 1) * 100:+.0f}%" if p and p["tv"] else "신규"
        if th in prv_pos:
            d = prv_pos[th] - i
            mv = f"▲{d}" if d > 0 else (f"▼{-d}" if d < 0 else "–")
        else:
            mv = "NEW"
        names = ", ".join(f"{r['ISU_NM']}({fmt_rt(r['FLUC_RT'])})" for _, r in v["mem"].head(5).iterrows())
        lines.append(f"{i}. {th} - {eok(v['tv'])} (전일비 {chg}, 순위 {mv}) / 가중등락 {v['rt']:+.2f}% / {v['n']}종목")
        lines.append(f"   └ {names}")
        ai_cur.append(f"{i}|{th}|{eok(v['tv'])}|{chg}|{mv}|{v['rt']:+.2f}%|{v['n']}|{names}")

    # 전일 테마
    ai_rot = []
    if prv_rank:
        lines.append("")
        lines.append(f"🕘 전일 테마 TOP 5 ({prev_date})")
        ai_rot.append(f"## 전일({prev_date}) 테마 순위|테마|거래대금|가중등락")
        for i, (th, v) in enumerate(prv_rank[:5], 1):
            lines.append(f"{i}. {th} - {eok(v['tv'])} (가중등락 {v['rt']:+.2f}%)")
        for i, (th, v) in enumerate(prv_rank[:8], 1):
            ai_rot.append(f"{i}|{th}|{eok(v['tv'])}|{v['rt']:+.2f}%")

        # 순환: 거래대금 증감
        delta = {}
        for th in set(cur) | set(prv):
            delta[th] = cur.get(th, {}).get("tv", 0) - prv.get(th, {}).get("tv", 0)
        inflow = [(t, x) for t, x in sorted(delta.items(), key=lambda kv: -kv[1]) if x > 0][:3]
        outflow = [(t, x) for t, x in sorted(delta.items(), key=lambda kv: kv[1]) if x < 0][:3]
        lines.append("")
        lines.append("🔄 테마 순환 (전일 대비 거래대금 증감)")
        lines.append("⬆️ 유입 " + (", ".join(f"{t} {x / 1e8:+,.0f}억" for t, x in inflow) or "없음"))
        lines.append("⬇️ 이탈 " + (", ".join(f"{t} {x / 1e8:+,.0f}억" for t, x in outflow) or "없음"))
        ai_rot.append("## 테마 순환(전일 대비 거래대금 증감)")
        ai_rot.append("유입: " + (", ".join(f"{t} {x / 1e8:+,.0f}억" for t, x in inflow) or "없음"))
        ai_rot.append("이탈: " + (", ".join(f"{t} {x / 1e8:+,.0f}억" for t, x in outflow) or "없음"))

    ht = history_text(hist, exclude=(date, prev_date))
    if ht:
        ai_rot.append("## 그 이전 최근 테마 TOP3 이력(최신순)")
        ai_rot.append(ht)

    return "\n".join(lines), "\n".join(ai_cur), "\n".join(ai_rot)


# ------------------------------------------------------------ 5. KR 수집
def collect_kr():
    """dict(show, ai_core, ai_rot, dates, ok)"""
    key = (os.getenv("KRX_API_KEY") or "").strip()
    if not key:
        return {"show": "⚠️ KRX_API_KEY 미설정", "ai_core": "", "ai_rot": "", "dates": [], "ok": False}

    shows, ais, dates, ok = [], [], [], False
    stock_today, stock_prev, today_date, prev_date = [], [], None, None
    for name, url in KRX_ENDPOINTS.items():
        try:
            latest, prev = krx_latest_two_days(url, key)
            if not latest:
                shows.append(f"{ICONS.get(name, '▪️')} {name}: 데이터 수신 실패 (로그 확인)")
                continue
            m = {"date": latest[0], "df": make_frame(latest[1]),
                 "prev": make_frame(prev[1]) if prev else None}
            dates.append(latest[0])
            if name in ("유가증권", "코스닥"):
                stock_today.append(m["df"])
                today_date = today_date or latest[0]
                if prev:
                    stock_prev.append(m["prev"])
                    prev_date = prev_date or prev[0]
            s, a = market_block(name, m)
            shows.append(s)
            ais.append(a)
            ok = True
        except Exception as e:
            print(f"[KRX] {name} 처리 예외:", repr(e))
            shows.append(f"{ICONS.get(name, '▪️')} {name}: 처리 중 오류 ({type(e).__name__})")

    ai_rot = ""
    if stock_today:
        hist = load_history()
        ts, ta, ai_rot = theme_block(stock_today, stock_prev, today_date, prev_date, hist)
        shows.insert(0, ts)
        ais.insert(0, ta)

        # 이력 저장 (오늘 + 전일)
        def pack(frames):
            rows, _ = theme_rows(frames)
            return {t: [round(v["tv"] / 1e8), round(v["rt"], 2)] for t, v in rows.items()}
        save_history(hist, {today_date: pack(stock_today),
                            **({prev_date: pack(stock_prev)} if prev_date else {})})
    return {"show": "\n\n".join(shows), "ai_core": "\n\n".join(ais), "ai_rot": ai_rot,
            "dates": dates, "ok": ok}


# ------------------------------------------------------------ 6. 미국 섹터 (보조)
def collect_us():
    try:
        import yfinance as yf
        tickers = list(US_SECTOR_ETFS.values())
        px = yf.download(tickers, period="7d", progress=False, auto_adjust=True)["Close"].dropna(how="all")
        chg = (px.iloc[-1] / px.iloc[-2] - 1) * 100
        rows = sorted(((n, chg[t]) for n, t in US_SECTOR_ETFS.items() if t in chg), key=lambda x: -x[1])
        d = px.index[-1].strftime("%Y-%m-%d")
        lines = [f"🇺🇸 미국 섹터 ETF 등락률 순위 (데이터 기준일 {d} 종가)"]
        lines += [f"{i}. {n}: {v:+.2f}%" for i, (n, v) in enumerate(rows, 1)]
        return "\n".join(lines), d
    except Exception as e:
        print("[US] 수집 실패:", repr(e))
        return "🇺🇸 미국 섹터: 수집 실패", None


# ------------------------------------------------------------ 7. Groq 요약
COMMON_RULES = (
    "당신은 국내 주식 시장 전문 애널리스트입니다. 친근하지만 간결한 존댓말로, 읽기 쉽게 쓰세요. "
    "제공된 데이터에 있는 종목·수치만 근거로 하고, 없는 수치·뉴스는 지어내지 마세요. "
    "'키워드테마'는 종목명 기반 추정이므로 틀리면 보정해도 됩니다. "
    "'섹터'는 업종(반도체, 전기전자, 화학, 제약바이오, 금융, 운송, 건설, 음식료 등), "
    "'테마'는 시장이 묶어 보는 이슈(AI반도체, 인버스, 방산, 2차전지 등)입니다. "
    "업종 판단이 불확실하면 '(추정)'이라고 표시하세요. "
    "섹션 제목은 아래 형식 그대로(아이콘 포함) 사용하고, 각 섹션은 짧은 번호 목록으로 쓰세요. 마크다운 기호(#, **)는 쓰지 마세요.\n\n"
)

PROMPT_A = COMMON_RULES + (
    "🧭 [1] 총평 (2~3줄: 시장 분위기, 거래대금·등락·인버스 비중, 미국 연결)\n"
    "🧩 [2] 테마별 거래대금 종목 정리 (거래대금 순으로 테마마다: 테마명 — 핵심 종목 — 섹터 — 한 줄 해석)\n"
    "🏢 [3] 유가증권 테마·섹터 정리 (거래대금 상위 종목을 테마/섹터로 묶어 순위 순으로, 종목 포함)\n"
    "🚀 [4] 코스닥 테마·섹터 정리 (같은 방식, 급등 종목의 테마 포함)"
)

PROMPT_B = COMMON_RULES + (
    "아래는 오늘 데이터와, 앞서 작성한 요약 일부입니다. 이어서 작성하세요.\n"
    "🕘 [5] 전일 테마 리뷰 (전일 상위 테마가 오늘 어떻게 달라졌는지 2~3줄)\n"
    "🔮 [6] 예상 테마 (다음 거래일 관심 가질 만한 테마 2~3개: 테마명 — 근거 수치 — 예상 시나리오). "
    "확정적 표현 대신 '~할 수 있습니다' 같은 시나리오 표현을 쓰세요.\n"
    "🔄 [7] 순환 섹터 예상 (자금 유입→이탈 흐름 정리, 다음 순환 후보 섹터와 근거, 이력이 있으면 며칠 추세 반영)\n"
    "📌 [8] 정리 요약 (정확히 아래 6줄)\n"
    "1) 시장 총평(거래대금·가중등락·인버스 비중)\n"
    "2) 자금이 몰린 테마\n"
    "3) 약세 테마\n"
    "4) 코스닥 급등 종목·테마\n"
    "5) 미국 섹터와 국내 연결\n"
    "6) 체크포인트 (1~2개)"
)


def groq_call(system, user, max_tokens):
    api_key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not api_key:
        return None, "GROQ_API_KEY 미설정", None

    models = list(dict.fromkeys(m for m in [os.getenv("GROQ_MODEL")] + GROQ_MODELS if m))
    err_last = "알 수 없음"
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_completion_tokens": max_tokens,
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
                    payload["max_completion_tokens"] = int(max_tokens * 1.8)
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
                    payload["messages"][1]["content"] = user[:3500]
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


def analyze(kr, us_text, asof_line):
    """(요약 본문, 사용 모델 리스트). 부분 실패 시 해당 부분에 사유 표기."""
    core = "\n\n".join(t for t in (kr["ai_core"], us_text) if t)
    if not core:
        return "⚠️ 요약 실패: 분석할 데이터 없음", []

    used, out = [], []
    a, err_a, ma = groq_call(PROMPT_A, f"{asof_line}\n\n{core[:7000]}", 3000)
    if a:
        out.append(a)
        used.append(ma)
    else:
        out.append(f"⚠️ [1]~[4] 요약 실패: {err_a}")

    time.sleep(3)  # 분당 토큰 한도 완화
    rot = kr["ai_rot"] or "(전일 비교 데이터 없음)"
    head_core = core[:2500]
    prior = f"\n\n[앞서 작성한 요약(일부)]\n{a[:1800]}" if a else ""
    user_b = f"{asof_line}\n\n{rot}\n\n[오늘 데이터 요약]\n{head_core}{prior}"
    b, err_b, mb = groq_call(PROMPT_B, user_b[:7000], 3000)
    if b:
        out.append(b)
        if mb not in used:
            used.append(mb)
    else:
        out.append(f"⚠️ [5]~[8] 요약 실패: {err_b}")

    return "\n\n".join(out), used


# ------------------------------------------------------------ 8. 메인
def run():
    markets = [m.upper() for m in sys.argv[1:]] or ["KR", "US"]
    now = dt.datetime.now(KST)
    now_s = now.strftime("%Y-%m-%d %H:%M KST")
    print(f"🎯 타겟 마켓: {markets} / {now_s}")

    kr = {"show": "", "ai_core": "", "ai_rot": "", "dates": [], "ok": False}
    us_text, us_date = "", None
    if "KR" in markets:
        kr = collect_kr()
    if "US" in markets:
        us_text, us_date = collect_us()

    kr_date = max(kr["dates"]) if kr["dates"] else "없음"
    asof_line = (f"[기준] 리포트 생성 {now_s} / 국내 데이터 기준일 {kr_date} (최근 영업일 종가) / "
                 f"미국 데이터 기준일 {us_date or '없음'} 종가")

    facts_show = "\n\n".join(t for t in (kr["show"], us_text) if t)
    summary, used = analyze(kr, us_text, asof_line)

    msg = "📊 [예상 섹터 테마 & 수급 리포트]\n"
    msg += f"⏰ 리포트 생성: {now_s}\n"
    msg += f"📅 국내 데이터 기준일: {kr_date} (최근 영업일 종가) / 미국: {us_date or '없음'} 종가\n\n"
    msg += facts_show + "\n\n"
    msg += f"🤖 AI 섹터·테마 요약 ({now:%H:%M} 생성, 데이터 {kr_date} 기준"
    msg += f", {'/'.join(used)})\n" if used else ")\n"
    msg += summary + "\n\n"
    msg += "※ 예상·순환 전망은 거래대금·등락 데이터 기반 시나리오이며 투자 권유가 아닙니다."

    print("전송 메시지:\n", msg)

    (DATA_DIR / "latest.json").write_text(
        json.dumps({"generated": now.isoformat(), "kr_date": kr_date, "us_date": us_date,
                    "report": facts_show, "summary": summary, "models": used},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    sent = send_telegram(msg)
    print("✅ 텔레그램 전송 성공" if sent else "❌ 텔레그램 전송 실패")
    if "KR" in markets and not kr["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print("💥 치명적 에러:", repr(e))
        sys.exit(1)
