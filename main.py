"""
예상 섹터 테마 & 수급 리포트 봇 (v7)
- KRX Open API(유가증권/코스닥/ETF 일별매매정보) 기반 수급·테마 분석
- 모든 순위 리스트 번호(상위→하위), 제목마다 아이콘
- 오늘 vs 전일 테마 순위 변동 / 거래대금 유입·이탈(순환) 자동 계산, 이력은 state/ 에 저장
- 구성: AI 섹터·테마 요약 / 테마별 거래대금 종목정리 10 / 유가증권 정리 10 / 코스닥 정리 5 /
        전일 테마 리뷰 3 / 예상 테마 / 순환 섹터 예상 / 정리 요약 + 시장 데이터 섹션
- Groq AI 2회 호출: (A) 요약·유가증권/코스닥 정리 (B) 전일 리뷰·예상 테마·순환 섹터·정리 요약
- 텔레그램 3개 메시지로 글자 수 균형 분할 전송
- AI가 실패해도 데이터 리포트(대체 문구 포함)는 전송
"""
import os
import re
import sys
import json
import time
import html
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

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# ─────────── 분량 설정 ───────────
CNT = dict(stock_theme=10, kospi=10, kosdaq=5, review=3)      # 요청 고정 개수
N = dict(rank=12, market=10, up=10, down=10, surge=10, etf_theme=8, flow=6)  # 자동 축소 대상
TG_LIMIT = 3800

# ─────────── 아이콘 / 포맷 ───────────
SI = {
    "요약": "📝", "한눈": "🌐", "순위": "🔥", "종목": "💰", "유가": "🏢", "코스닥": "🚀",
    "유입": "📈", "이탈": "📉", "상승": "⚡", "하락": "🧊", "급증": "💥", "etf": "🧲",
    "리뷰": "🔍", "예상": "🎯", "순환": "🔄", "정리": "✅",
}
RANK_MARKS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
LINE = "━━━━━━━━━━━━━━"


def rank_mark(i):
    return RANK_MARKS[i] if i < len(RANK_MARKS) else f"{i + 1}."


def make_section(icon, title, lines, numbered=False):
    head = f"{icon} <b>{html.escape(title)}</b>\n{LINE}"
    body = []
    for i, ln in enumerate(lines):
        ln = html.escape(str(ln).strip())
        if not ln:
            continue
        body.append(f"{rank_mark(i)} {ln}" if numbered else f"▫️ {ln}")
    if not body:
        body = ["▫️ 데이터 없음"]
    return head + "\n" + "\n".join(body) + "\n"


def fmt_eok(v):
    return f"{v / 10000:.1f}조" if abs(v) >= 10000 else f"{v:,.0f}억"


# ─────────── 테마 사전 (자유롭게 추가) ───────────
THEME_MAP = {
    "반도체": ["반도체", "하이닉스", "삼성전자", "한미반도체", "DB하이텍", "리노공업", "주성엔지니어링",
              "원익IPS", "이오테크닉스", "테스", "HPSP", "피에스케이", "유진테크", "제주반도체", "에스티아이"],
    "2차전지": ["2차전지", "배터리", "에코프로", "엘앤에프", "포스코퓨처엠", "LG에너지솔루션", "삼성SDI",
              "SK이노베이션", "천보", "솔브레인", "더블유씨피", "코스모신소재", "대주전자재료", "SKC"],
    "바이오·제약": ["바이오", "제약", "셀트리온", "알테오젠", "삼성바이오", "유한양행", "한미약품",
                 "HLB", "리가켐", "에이비엘", "펩트론", "보로노이", "파마", "헬스케어"],
    "AI·로봇": ["로봇", "레인보우", "두산로보틱스", "에이아이", "AI", "솔트룩스", "마음AI", "클로봇", "뉴로메카"],
    "방산·우주": ["방산", "한화에어로", "한국항공우주", "LIG넥스원", "현대로템", "풍산", "빛과전자",
               "쎄트렉", "인텔리안", "한화시스템", "우주", "항공"],
    "조선·해운": ["조선", "한화오션", "HD현대중공업", "삼성중공업", "HD현대미포", "HMM", "팬오션", "해운"],
    "자동차·부품": ["현대차", "기아", "현대모비스", "현대위아", "한온시스템", "만도", "성우하이텍", "자동차"],
    "전력·원전": ["두산에너빌리티", "한전", "LS ELECTRIC", "HD현대일렉트릭", "효성중공업",
               "일진전기", "제룡전기", "원전", "전선", "LS전선", "대한전선", "가온전선", "전력"],
    "금융·증권": ["금융", "KB금융", "신한지주", "하나금융", "우리금융", "증권", "미래에셋", "키움", "삼성생명", "보험", "은행"],
    "엔터·게임": ["하이브", "JYP", "에스엠", "와이지", "엔터", "크래프톤", "엔씨소프트", "넷마블", "게임", "펄어비스"],
    "화장품·소비": ["아모레", "LG생활건강", "코스메", "화장품", "코스맥스", "한국콜마", "실리콘투", "음식료", "CJ", "농심", "소비"],
    "건설·인프라": ["건설", "현대건설", "GS건설", "대우건설", "DL이앤씨", "삼성물산", "시멘트", "인프라"],
    "철강·소재": ["POSCO홀딩스", "현대제철", "고려아연", "철강", "동국제강", "화학", "롯데케미칼", "금호석유", "소재"],
    "플랫폼·SW": ["NAVER", "카카오", "더존", "소프트웨어", "클라우드", "플랫폼", "엔비디아"],
    "신재생·에너지": ["태양광", "풍력", "수소", "OCI", "한화솔루션", "씨에스윈드", "두산퓨얼셀", "에너지"],
}
ETF_THEME_MAP = {
    "레버리지·인버스": ["레버리지", "인버스", "곱버스", "2X", "3X"],
    "미국 지수·빅테크": ["미국", "나스닥", "S&P", "다우", "필라델피아", "테슬라", "엔비디아", "빅테크", "FANG"],
    "국내 지수": ["코스피", "KOSPI", "코스닥150", "KRX300", "TOP10", "200"],
    "채권·금리": ["채권", "국채", "금리", "회사채", "CD", "머니마켓", "단기"],
    "원자재·금·원유": ["골드", "금현물", "은선물", "원유", "구리", "농산물", "원자재"],
    "배당·커버드콜": ["배당", "커버드콜", "고배당", "리츠"],
    "중국·신흥국": ["중국", "차이나", "항셍", "인도", "베트남", "일본", "유럽", "신흥"],
}


def classify_theme(name):
    low = str(name).lower()
    for theme, kws in THEME_MAP.items():
        if any(kw.lower() in low for kw in kws):
            return theme
    return None


def classify_etf(name):
    low = str(name).lower()
    for theme, kws in ETF_THEME_MAP.items():
        if any(kw.lower() in low for kw in kws):
            return theme
    return classify_theme(name) or "기타 ETF"


# ─────────── KRX 데이터 ───────────
def to_num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def fetch_krx(market, ymd):
    key = os.environ.get("KRX_API_KEY", "")
    try:
        r = requests.get(KRX_ENDPOINTS[market], headers={"AUTH_KEY": key},
                         params={"basDd": ymd}, timeout=30)
        r.raise_for_status()
        rows = r.json().get("OutBlock_1", [])
    except Exception as e:
        print(f"[KRX] {market} {ymd} 실패: {e}", file=sys.stderr)
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "market": market,
        "code": df.get("ISU_CD"),
        "name": df.get("ISU_NM"),
        "close": to_num(df.get("TDD_CLSPRC")),
        "rate": to_num(df.get("FLUC_RT")),
        "volume": to_num(df.get("ACC_TRDVOL")),
        "value": to_num(df.get("ACC_TRDVAL")) / 1e8,   # 억원
    })
    return out.dropna(subset=["name", "value"])


def load_day(ymd):
    frames = [fetch_krx(m, ymd) for m in KRX_ENDPOINTS]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def find_trading_days(n=2):
    found, d = [], dt.datetime.now(KST).date()
    for _ in range(20):
        if d.weekday() < 5:
            ymd = d.strftime("%Y%m%d")
            if not fetch_krx("유가증권", ymd).empty:
                found.append(ymd)
                if len(found) == n:
                    break
        d -= dt.timedelta(days=1)
    return found


# ─────────── 계산 ───────────
THEME_COLS = ["theme", "value", "rate", "n_stocks", "leaders", "rank"]


def build_theme_table(df, markets=("유가증권", "코스닥"), n_leaders=5):
    if df.empty:
        return pd.DataFrame(columns=THEME_COLS)
    stocks = df[df["market"].isin(markets)].copy()
    stocks["theme"] = stocks["name"].map(classify_theme)
    stocks = stocks.dropna(subset=["theme"])
    if stocks.empty:
        return pd.DataFrame(columns=THEME_COLS)
    rows = []
    for theme, g in stocks.groupby("theme"):
        w = g["value"].clip(lower=0.01)
        rate = float((g["rate"].fillna(0) * w).sum() / w.sum())
        top = g.sort_values("value", ascending=False).head(n_leaders)
        leaders = [{"name": r.name, "rate": float(r.rate), "value": float(r.value)} for r in top.itertuples()]
        rows.append({"theme": theme, "value": float(g["value"].sum()), "rate": rate,
                     "n_stocks": len(g), "leaders": leaders})
    t = pd.DataFrame(rows).sort_values("value", ascending=False).reset_index(drop=True)
    t["rank"] = t.index + 1
    return t


def build_etf_themes(df):
    etf = df[df["market"] == "ETF"].copy() if not df.empty else pd.DataFrame()
    if etf.empty:
        return pd.DataFrame()
    etf["theme"] = etf["name"].map(classify_etf)
    rows = []
    for theme, g in etf.groupby("theme"):
        w = g["value"].clip(lower=0.01)
        top = g.sort_values("value", ascending=False).iloc[0]
        rows.append({"theme": theme, "value": float(g["value"].sum()),
                     "rate": float((g["rate"].fillna(0) * w).sum() / w.sum()),
                     "n": len(g), "top": top["name"]})
    return pd.DataFrame(rows).sort_values("value", ascending=False).reset_index(drop=True)


def market_overview(df):
    lines = []
    for mk in ["유가증권", "코스닥", "ETF"]:
        g = df[df["market"] == mk]
        if g.empty:
            continue
        up, dn = int((g["rate"] > 0).sum()), int((g["rate"] < 0).sum())
        flat = len(g) - up - dn
        lines.append(f"{ICONS[mk]} {mk}  거래대금 {fmt_eok(g['value'].sum())} · "
                     f"🔺{up} 🔻{dn} ➖{flat} · 평균 {g['rate'].mean():+.2f}%")
    return lines


def volume_surge(df, prev_df):
    if prev_df.empty:
        return pd.DataFrame()
    cur = df[df["market"] != "ETF"][["code", "name", "market", "value", "rate"]]
    pv = prev_df[prev_df["market"] != "ETF"][["code", "value"]].rename(columns={"value": "value_prev"})
    s = cur.merge(pv, on="code")
    s = s[(s["value"] >= 50) & (s["value_prev"] >= 5)].copy()
    s["ratio"] = s["value"] / s["value_prev"]
    return s.sort_values("ratio", ascending=False)


def stock_txt(l):
    return f"{l['name']} {fmt_eok(l['value'])}({l['rate']:+.1f}%)"


def theme_brief(t, n):
    return "\n".join(
        f"{r.theme} {fmt_eok(r.value)} {r.rate:+.2f}% 대장:" + ", ".join(stock_txt(l) for l in r.leaders[:3])
        for r in t.head(n).itertuples())


# ─────────── 이력 ───────────
def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(h):
    keys = sorted(h.keys())[-10:]
    HISTORY_FILE.write_text(json.dumps({k: h[k] for k in keys}, ensure_ascii=False, indent=1),
                            encoding="utf-8")


# ─────────── Groq AI ───────────
SYSTEM_PROMPT = (
    "너는 한국 주식시장 수급 분석 리포트를 작성하는 애널리스트다. "
    "보고서는 충분히 상세하게 제출하라. 각 항목은 2~3문장으로, 제공된 데이터의 종목명·거래대금·등락률 수치를 구체적으로 인용한다. "
    "단정적 투자 권유는 피하고 가능성·관찰 포인트로 서술한다. "
    "마크다운(**, #)은 쓰지 말고, 지정된 [태그] 형식과 항목 개수를 정확히 지켜라. "
    "각 항목은 반드시 '- '로 시작하는 한 줄이며 항목 안에서 줄바꿈하지 않는다."
)


def call_groq(prompt, retries=2):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return ""
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                GROQ_URL, headers={"Authorization": f"Bearer {key}"},
                json={"model": GROQ_MODEL, "temperature": 0.4, "max_tokens": 3500,
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": prompt}]},
                timeout=90)
            if r.ok:
                return r.json()["choices"][0]["message"]["content"].strip()
            print(f"[Groq] {r.status_code} {r.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[Groq] 예외: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return ""


def parse_blocks(text, keys):
    out = {k: [] for k in keys}
    cur = None
    for ln in text.splitlines():
        m = re.match(r"^\s*\[(.+?)\]\s*(.*)$", ln)
        if m and m.group(1) in out:
            cur = m.group(1)
            if m.group(2).strip():
                out[cur].append(m.group(2).strip())
            continue
        if cur and ln.strip():
            # 글머리표/번호만 제거 (2차전지 같은 숫자 시작 단어는 보존)
            out[cur].append(re.sub(r"^\s*(?:[\-\•\*]|\d{1,2}[\.\)])\s*", "", ln).strip())
    return out


# ─────────── 텔레그램 ───────────
def split_balanced(sections, parts=3):
    n = len(sections)
    if n <= parts:
        return ["\n".join(sections[i:i + 1]) for i in range(n)]
    lens = [len(s) for s in sections]
    best, cuts = None, None
    for a in range(1, n - 1):
        for b in range(a + 1, n):
            sizes = [sum(lens[:a]), sum(lens[a:b]), sum(lens[b:])]
            score = max(sizes) - min(sizes)
            if best is None or score < best:
                best, cuts = score, (a, b)
    a, b = cuts
    return ["\n".join(c) for c in (sections[:a], sections[a:b], sections[b:])]


def hard_split(text, limit=4000):
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for ln in text.split("\n"):
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur:
        out.append(cur)
    return out


def send_telegram(sections, header):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    msgs = split_balanced(sections, 3)
    if not token or not chat_id:
        print("텔레그램 시크릿 없음 → 콘솔 출력", file=sys.stderr)
        print(header, *msgs, sep="\n\n")
        return
    total = len(msgs)
    for idx, m in enumerate(msgs, 1):
        top = f"{header}\n\n" if idx == 1 else ""
        text = f"📨 <b>({idx}/{total})</b>\n\n{top}{m}"
        for piece in hard_split(text):
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": piece, "parse_mode": "HTML",
                      "disable_web_page_preview": True}, timeout=20)
            if not r.ok:
                print("텔레그램 실패:", r.text, file=sys.stderr)
            time.sleep(0.8)


# ─────────── 메인 ───────────
def main():
    days = find_trading_days(2)
    if not days:
        send_telegram([make_section("⚠️", "데이터 오류",
                       ["KRX 데이터를 불러오지 못했습니다. KRX_API_KEY와 서비스 승인 여부를 확인하세요."])],
                      "📊 <b>예상 섹터 테마 리포트</b>")
        return
    today_ymd = days[0]
    prev_ymd = days[1] if len(days) > 1 else None
    nice = f"{today_ymd[:4]}-{today_ymd[4:6]}-{today_ymd[6:]}"

    df = load_day(today_ymd)
    prev_df = load_day(prev_ymd) if prev_ymd else pd.DataFrame()

    themes = build_theme_table(df, n_leaders=5)                          # 전체(유가증권+코스닥)
    t_kospi = build_theme_table(df, markets=("유가증권",), n_leaders=3)
    t_kosdaq = build_theme_table(df, markets=("코스닥",), n_leaders=3)
    prev_themes = build_theme_table(prev_df) if not prev_df.empty else pd.DataFrame(columns=THEME_COLS)
    etf_themes = build_etf_themes(df)
    overview = market_overview(df)
    surge = volume_surge(df, prev_df)
    hist = load_history()
    liquid = df[(df["market"] != "ETF") & (df["value"] >= 100)]

    prev_rank = dict(zip(prev_themes["theme"], prev_themes["rank"]))
    prev_val = dict(zip(prev_themes["theme"], prev_themes["value"]))
    cur_val = dict(zip(themes["theme"], themes["value"]))
    have_prev = bool(prev_rank)

    # 유입/이탈 (오늘·전일 어느 쪽에만 있어도 포함)
    flows = []
    if have_prev:
        for th in set(cur_val) | set(prev_val):
            c, p = cur_val.get(th, 0.0), prev_val.get(th, 0.0)
            flows.append((th, c - p, p, c))
        flows.sort(key=lambda x: x[1], reverse=True)
    inflow_all = [f for f in flows if f[1] > 0]
    outflow_all = [f for f in reversed(flows) if f[1] < 0]

    # ── 전일 리뷰 입력/대체 문구 ──
    prev_expect = hist.get(prev_ymd, {}).get("expect", "") if prev_ymd else ""
    today_rank = dict(zip(themes["theme"], themes["rank"]))
    today_rate = dict(zip(themes["theme"], themes["rate"]))
    review_fallback = []
    for r in prev_themes.head(CNT["review"]).itertuples():
        if r.theme in today_rank:
            review_fallback.append(
                f"{r.theme}: 어제 {int(r.rank)}위({fmt_eok(r.value)}) → 오늘 {int(today_rank[r.theme])}위"
                f"({fmt_eok(cur_val[r.theme])}, 평균 {today_rate[r.theme]:+.2f}%)")
        else:
            review_fallback.append(f"{r.theme}: 어제 {int(r.rank)}위({fmt_eok(r.value)}) → 오늘은 순위권 밖")
    if not review_fallback:
        review_fallback = ["전일 비교 데이터가 아직 없습니다 (내일부터 표시)"]

    # ── AI 입력 요약 ──
    data_brief = (
        f"기준일 {nice}\n[시장]\n" + "\n".join(overview) +
        "\n[전체 TOP 테마]\n" + theme_brief(themes, 10) +
        "\n[유가증권 테마 TOP10]\n" + theme_brief(t_kospi, 10) +
        "\n[코스닥 테마 TOP5]\n" + theme_brief(t_kosdaq, 5) +
        "\n[거래대금 유입]\n" + "\n".join(f"{t} +{fmt_eok(d)}" for t, d, _, _ in inflow_all[:5]) +
        "\n[거래대금 이탈]\n" + "\n".join(f"{t} -{fmt_eok(abs(d))}" for t, d, _, _ in outflow_all[:5]) +
        ("\n[ETF 쏠림]\n" + "\n".join(f"{r.theme} {fmt_eok(r.value)} {r.rate:+.2f}%"
                                     for r in etf_themes.head(6).itertuples()) if not etf_themes.empty else "") +
        "\n[상승률 TOP]\n" + "\n".join(f"{r.name} {r.rate:+.1f}%({r.market})"
                                      for r in liquid.sort_values("rate", ascending=False).head(8).itertuples())
    )

    # ── AI A: 요약 / 유가증권 10 / 코스닥 5 ──
    ai_a = call_groq(
        data_brief +
        f"\n\n위 데이터를 바탕으로 아래 형식으로만 답하라.\n"
        f"[AI요약] 오늘 섹터·테마 흐름 종합 요약 6줄\n"
        f"[유가증권정리] 유가증권 시장의 테마·섹터 정리 정확히 {CNT['kospi']}줄 ('- 테마명: 설명' 형식, 거래대금 순)\n"
        f"[코스닥정리] 코스닥 시장의 테마·섹터 정리 정확히 {CNT['kosdaq']}줄 ('- 테마명: 설명' 형식, 거래대금 순)")
    ba = parse_blocks(ai_a, ["AI요약", "유가증권정리", "코스닥정리"]) if ai_a else {}
    a_ok = bool(ba) and any(ba.values())

    # ── AI B: 전일 리뷰 3 / 예상 / 순환 / 요약 ──
    ai_b = call_groq(
        data_brief + f"\n\n[전일 예상 테마]\n{prev_expect or '기록 없음'}\n"
        f"[전일 TOP 테마의 오늘 결과]\n" + "\n".join(review_fallback) +
        f"\n\n전일 예상과 오늘 결과를 비교하여 아래 형식으로만 답하라.\n"
        f"[전일리뷰] 전일 테마 리뷰 정확히 {CNT['review']}줄 (어제 강했던 테마가 오늘 어떻게 이어졌는지)\n"
        f"[예상테마] 첫 줄은 다음 거래일 예상 테마명만 쉼표로 3~5개, 이어서 근거 4~5줄\n"
        f"[순환섹터] 거래대금이 이동할 것으로 보이는 순환 섹터 예상 4~5줄 ('- A → B: 이유' 형식)\n"
        f"[정리요약] 오늘 리포트 전체 정리 요약 5~6줄")
    bb = parse_blocks(ai_b, ["전일리뷰", "예상테마", "순환섹터", "정리요약"]) if ai_b else {}
    b_ok = bool(bb) and any(bb.values())

    # ── 섹션별 최종 문구 (AI 우선, 없으면 데이터 대체) ──
    def fb_market(t, n):
        return [f"{r.theme}: 거래대금 {fmt_eok(r.value)}, 평균 {r.rate:+.2f}%, "
                f"대장 " + ", ".join(stock_txt(l) for l in r.leaders[:3]) for r in t.head(n).itertuples()]

    ai_fail_note = "AI 요약 연결 지연 또는 모델 오류 → 데이터 기반 대체 문구입니다"
    summary_lines = ba.get("AI요약") if a_ok and ba.get("AI요약") else [ai_fail_note] + overview
    kospi_lines = (ba.get("유가증권정리") or fb_market(t_kospi, CNT["kospi"]))[:CNT["kospi"]] if a_ok else fb_market(t_kospi, CNT["kospi"])
    kosdaq_lines = (ba.get("코스닥정리") or fb_market(t_kosdaq, CNT["kosdaq"]))[:CNT["kosdaq"]] if a_ok else fb_market(t_kosdaq, CNT["kosdaq"])
    review_lines = (bb.get("전일리뷰") or review_fallback)[:CNT["review"]] if b_ok else review_fallback

    exp_fb = [", ".join(t for t, _, _, _ in inflow_all[:3]) or "데이터 부족"] + \
             [f"{t}: 거래대금 +{fmt_eok(d)} 유입 ({fmt_eok(p)} → {fmt_eok(c)})" for t, d, p, c in inflow_all[:3]]
    rot_fb = [f"{o[0]} → {i[0]}: {o[0]} 거래대금 {fmt_eok(abs(o[1]))} 감소, {i[0]} {fmt_eok(i[1])} 증가"
              for o, i in zip(outflow_all[:3], inflow_all[:3])] or ["전일 비교 데이터가 아직 없습니다"]
    sum_fb = [f"거래대금 1위 테마는 {themes.iloc[0]['theme']}({fmt_eok(themes.iloc[0]['value'])})입니다."] if not themes.empty else []

    expect_lines = bb.get("예상테마") if b_ok and bb.get("예상테마") else exp_fb
    rotation_lines = bb.get("순환섹터") if b_ok and bb.get("순환섹터") else rot_fb
    final_lines = bb.get("정리요약") if b_ok and bb.get("정리요약") else (sum_fb + overview)

    # ── 섹션 조립 (data 섹션만 f 배율로 자동 축소) ──
    def build(f):
        n = {k: max(3, int(v * f)) for k, v in N.items()}
        leaders_n = max(3, int(5 * f))
        s = [make_section(SI["요약"], "AI 섹터·테마 요약", summary_lines),
             make_section(SI["한눈"], "시장 한눈에", overview)]

        rank_lines = []
        for r in themes.head(n["rank"]).itertuples():
            if have_prev:
                if r.theme in prev_rank:
                    d = int(prev_rank[r.theme] - r.rank)
                    tag = f"🔺{d}" if d > 0 else (f"🔻{-d}" if d < 0 else "➖")
                    chg = f" · {int(prev_rank[r.theme])}위→{r.rank}위 {tag}"
                else:
                    chg = " · 🆕 신규"
            else:
                chg = ""
            rank_lines.append(f"{r.theme}  {fmt_eok(r.value)} · {r.rate:+.2f}% · {r.n_stocks}종목{chg}")
        s.append(make_section(SI["순위"], "테마 순위 (거래대금순·전일 대비)", rank_lines, numbered=True))

        stock_lines = []
        for r in themes.head(CNT["stock_theme"]).itertuples():
            lead = " · ".join(stock_txt(l) for l in r.leaders[:leaders_n])
            stock_lines.append(f"{r.theme}  {fmt_eok(r.value)} · {r.rate:+.2f}%\n   └ {lead}")
        s.append(make_section(SI["종목"], "테마별 거래대금 종목정리 TOP10", stock_lines, numbered=True))

        s.append(make_section(SI["유가"], "유가증권 테마·섹터 정리 (10)", kospi_lines, numbered=True))
        s.append(make_section(SI["코스닥"], "코스닥 테마·섹터 정리 (5)", kosdaq_lines, numbered=True))

        s.append(make_section(SI["유입"], "거래대금 유입 (순환 ↑)",
                              [f"{t}  +{fmt_eok(d)} ({fmt_eok(p)} → {fmt_eok(c)})" for t, d, p, c in inflow_all[:n["flow"]]],
                              numbered=True))
        s.append(make_section(SI["이탈"], "거래대금 이탈 (순환 ↓)",
                              [f"{t}  -{fmt_eok(abs(d))} ({fmt_eok(p)} → {fmt_eok(c)})" for t, d, p, c in outflow_all[:n["flow"]]],
                              numbered=True))

        for mk in ["유가증권", "코스닥", "ETF"]:
            sub = df[df["market"] == mk].sort_values("value", ascending=False).head(n["market"])
            s.append(make_section(ICONS[mk], f"{mk} 거래대금 TOP",
                                  [f"{r.name}  {fmt_eok(r.value)} · {r.rate:+.2f}%" for r in sub.itertuples()],
                                  numbered=True))
        s.append(make_section(SI["상승"], "상승률 TOP (거래대금 100억↑)",
                              [f"{r.name}  {r.rate:+.2f}% · {fmt_eok(r.value)} ({r.market})"
                               for r in liquid.sort_values("rate", ascending=False).head(n["up"]).itertuples()],
                              numbered=True))
        s.append(make_section(SI["하락"], "하락률 TOP (거래대금 100억↑)",
                              [f"{r.name}  {r.rate:+.2f}% · {fmt_eok(r.value)} ({r.market})"
                               for r in liquid.sort_values("rate").head(n["down"]).itertuples()],
                              numbered=True))
        if not surge.empty:
            s.append(make_section(SI["급증"], "거래대금 급증 (전일 대비 배수)",
                                  [f"{r.name}  x{r.ratio:.1f}배 ({fmt_eok(r.value_prev)} → {fmt_eok(r.value)}) · {r.rate:+.2f}%"
                                   for r in surge.head(n["surge"]).itertuples()], numbered=True))
        if not etf_themes.empty:
            s.append(make_section(SI["etf"], "ETF 자금 쏠림 테마",
                                  [f"{r.theme}  {fmt_eok(r.value)} · {r.rate:+.2f}% · {r.n}종\n   └ 대표: {r.top}"
                                   for r in etf_themes.head(n["etf_theme"]).itertuples()], numbered=True))

        s.append(make_section(SI["리뷰"], "전일 테마 리뷰 (3)", review_lines, numbered=True))
        s.append(make_section(SI["예상"], "다음 거래일 예상 테마", expect_lines))
        s.append(make_section(SI["순환"], "순환 섹터 예상", rotation_lines))
        s.append(make_section(SI["정리"], "정리 요약", final_lines))
        return s

    sections = []
    for f in (1.0, 0.8, 0.65, 0.5):          # 3개 메시지 한도 초과 시 데이터 섹션 항목 수 자동 축소
        sections = build(f)
        if sum(len(x) for x in sections) <= 3 * TG_LIMIT:
            break

    header = f"📊 <b>예상 섹터 테마 리포트</b>\n🗓 기준일 {nice} · ⏰ {dt.datetime.now(KST):%H:%M} 발송"
    send_telegram(sections, header)

    # ── 이력/데이터 저장 ──
    hist[today_ymd] = {
        "themes": [{"theme": r.theme, "value": r.value, "rate": r.rate, "rank": int(r.rank)}
                   for r in themes.itertuples()],
        "expect": " / ".join(expect_lines[:1]) if b_ok else "",
    }
    save_history(hist)
    (DATA_DIR / "latest.json").write_text(
        json.dumps({"date": nice, "themes": hist[today_ymd]["themes"]}, ensure_ascii=False, indent=1),
        encoding="utf-8")


if __name__ == "__main__":
    main()
