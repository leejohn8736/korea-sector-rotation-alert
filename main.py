"""
예상 섹터 테마 & 수급 리포트 봇 (v5)
- KRX Open API(유가증권/코스닥/ETF 일별매매정보) 기반 수급·테마 분석
- 모든 순위 리스트 번호(상위→하위), 제목마다 아이콘, 문단 간격 정리
- 오늘 vs 전일 테마 순위 변동 / 거래대금 유입·이탈(순환) 자동 계산, 이력은 state/ 에 저장
- Groq AI 2회 호출: (A) 총평·테마/섹터 정리 (B) 전일 리뷰·예상 테마·순환 섹터·정리 요약
- 텔레그램 3개 메시지로 글자 수 균형 분할 전송
- AI가 실패해도 데이터 리포트는 전송
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

# Groq: llama3-70b-8192 는 종료된 모델이라 최신 모델로 교체
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# ───────────────────────── 아이콘 / 포맷 ─────────────────────────
SECTION_ICONS = {
    "총평": "📝", "테마": "🔥", "변동": "🔀", "유입": "📈", "이탈": "📉",
    "상승": "⚡", "정리": "🧭", "리뷰": "🔍", "예상": "🎯", "순환": "🔄", "요약": "✅",
}
RANK_MARKS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
LINE = "━━━━━━━━━━━━━━"
TG_LIMIT = 4000


def rank_mark(i: int) -> str:
    return RANK_MARKS[i] if i < len(RANK_MARKS) else f"{i + 1}."


def make_section(icon: str, title: str, lines, numbered: bool = False) -> str:
    """제목(아이콘) + 구분선 + 본문. 섹션 끝에 빈 줄."""
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


# ───────────────────────── 테마 사전 (필요시 자유롭게 추가) ─────────────────────────
THEME_MAP = {
    "반도체": ["반도체", "하이닉스", "삼성전자", "한미반도체", "DB하이텍", "리노공업", "주성엔지니어링",
              "원익IPS", "이오테크닉스", "테스", "HPSP", "피에스케이", "유진테크", "제주반도체", "에스티아이"],
    "2차전지": ["에코프로", "엘앤에프", "포스코퓨처엠", "LG에너지솔루션", "삼성SDI", "SK이노베이션",
              "천보", "솔브레인", "더블유씨피", "코스모신소재", "대주전자재료", "SKC"],
    "바이오·제약": ["바이오", "제약", "셀트리온", "알테오젠", "삼성바이오", "유한양행", "한미약품",
                 "HLB", "리가켐", "에이비엘", "펩트론", "보로노이", "파마", "헬스케어"],
    "AI·로봇": ["로봇", "레인보우", "두산로보틱스", "에이아이", "AI", "솔트룩스", "마음AI", "클로봇", "뉴로메카"],
    "방산·우주": ["한화에어로", "한국항공우주", "LIG넥스원", "현대로템", "풍산", "빛과전자",
               "쎄트렉", "인텔리안", "한화시스템", "우주", "항공"],
    "조선·해운": ["조선", "한화오션", "HD현대중공업", "삼성중공업", "HD현대미포", "HMM", "팬오션", "해운"],
    "자동차·부품": ["현대차", "기아", "현대모비스", "현대위아", "한온시스템", "만도", "성우하이텍", "자동차"],
    "전력·원전": ["두산에너빌리티", "한전", "LS ELECTRIC", "HD현대일렉트릭", "효성중공업",
               "일진전기", "제룡전기", "원전", "전선", "LS전선", "대한전선", "가온전선"],
    "금융·증권": ["금융", "KB금융", "신한지주", "하나금융", "우리금융", "증권", "미래에셋", "키움", "삼성생명", "보험"],
    "엔터·게임": ["하이브", "JYP", "에스엠", "와이지", "엔터", "크래프톤", "엔씨소프트", "넷마블", "게임", "펄어비스"],
    "화장품·소비": ["아모레", "LG생활건강", "코스메", "화장품", "코스맥스", "한국콜마", "실리콘투", "음식료", "CJ", "농심"],
    "건설·인프라": ["건설", "현대건설", "GS건설", "대우건설", "DL이앤씨", "삼성물산", "시멘트"],
    "철강·소재": ["POSCO홀딩스", "현대제철", "고려아연", "철강", "동국제강", "화학", "롯데케미칼", "금호석유"],
    "플랫폼·SW": ["NAVER", "카카오", "더존", "소프트웨어", "클라우드", "플랫폼", "엔비디아"],
    "신재생·에너지": ["태양광", "풍력", "수소", "OCI", "한화솔루션", "씨에스윈드", "두산퓨얼셀", "에너지"],
}


def classify_theme(name: str):
    for theme, kws in THEME_MAP.items():
        for kw in kws:
            if kw.lower() in name.lower():
                return theme
    return None


# ───────────────────────── KRX 데이터 ─────────────────────────
def to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def fetch_krx(market: str, ymd: str) -> pd.DataFrame:
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


def load_day(ymd: str) -> pd.DataFrame:
    frames = [fetch_krx(m, ymd) for m in KRX_ENDPOINTS]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def find_trading_days(n: int = 2):
    """오늘부터 거슬러 올라가며 데이터가 있는 최근 n개 영업일(최신순) 반환."""
    found, d = [], dt.datetime.now(KST).date()
    for _ in range(20):
        if d.weekday() < 5:
            ymd = d.strftime("%Y%m%d")
            df = fetch_krx("유가증권", ymd)
            if not df.empty:
                found.append(ymd)
                if len(found) == n:
                    break
        d -= dt.timedelta(days=1)
    return found


# ───────────────────────── 테마 계산 ─────────────────────────
def build_theme_table(df: pd.DataFrame) -> pd.DataFrame:
    stocks = df[df["market"].isin(["유가증권", "코스닥"])].copy()
    stocks["theme"] = stocks["name"].map(classify_theme)
    stocks = stocks.dropna(subset=["theme"])
    if stocks.empty:
        return pd.DataFrame(columns=["theme", "value", "rate", "count", "leaders", "rank"])
    rows = []
    for theme, g in stocks.groupby("theme"):
        w = g["value"].clip(lower=0.01)
        rate = float((g["rate"].fillna(0) * w).sum() / w.sum())
        leaders = ", ".join(g.sort_values("value", ascending=False)["name"].head(3))
        rows.append({"theme": theme, "value": float(g["value"].sum()), "rate": rate,
                     "count": len(g), "leaders": leaders})
    t = pd.DataFrame(rows).sort_values("value", ascending=False).reset_index(drop=True)
    t["rank"] = t.index + 1
    return t


def fmt_eok(v: float) -> str:
    return f"{v / 10000:.1f}조" if abs(v) >= 10000 else f"{v:,.0f}억"


def arrow(x: float) -> str:
    return "🔺" if x > 0 else ("🔻" if x < 0 else "➖")


# ───────────────────────── 이력 ─────────────────────────
def load_history() -> dict:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(h: dict):
    keys = sorted(h.keys())[-10:]                       # 최근 10일만 유지
    HISTORY_FILE.write_text(json.dumps({k: h[k] for k in keys}, ensure_ascii=False, indent=1),
                            encoding="utf-8")


# ───────────────────────── Groq AI ─────────────────────────
def call_groq(prompt: str, retries: int = 2) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return ""
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": GROQ_MODEL, "temperature": 0.4, "max_tokens": 1400,
                      "messages": [
                          {"role": "system", "content":
                           "너는 한국 주식시장 수급 분석가다. 한국어로, 간결하고 단정적이지 않게 쓴다. "
                           "마크다운(**, #)은 쓰지 말고 지정된 [태그] 형식을 정확히 지켜라."},
                          {"role": "user", "content": prompt}]},
                timeout=60,
            )
            if r.ok:
                return r.json()["choices"][0]["message"]["content"].strip()
            print(f"[Groq] {r.status_code} {r.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[Groq] 예외: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return ""


def parse_blocks(text: str, keys) -> dict:
    """[태그] 로 시작하는 블록을 {태그: [줄,...]} 로 파싱."""
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
            out[cur].append(re.sub(r"^[\-\•\*\d\.\)\s]+", "", ln).strip())
    return out


# ───────────────────────── 텔레그램 ─────────────────────────
def split_balanced(sections, parts: int = 3):
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


def hard_split(text: str, limit: int = TG_LIMIT):
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


def send_telegram(sections, header: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 시크릿 없음 → 콘솔 출력", file=sys.stderr)
        print(header, *sections, sep="\n")
        return
    msgs = split_balanced(sections, 3)
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


# ───────────────────────── 메인 ─────────────────────────
def main():
    days = find_trading_days(2)
    if not days:
        send_telegram([make_section("⚠️", "데이터 오류", ["KRX 데이터를 불러오지 못했습니다. KRX_API_KEY와 서비스 승인 여부를 확인하세요."])],
                      "📊 <b>예상 섹터 테마 리포트</b>")
        return
    today_ymd = days[0]
    prev_ymd = days[1] if len(days) > 1 else None
    nice = f"{today_ymd[:4]}-{today_ymd[4:6]}-{today_ymd[6:]}"

    df = load_day(today_ymd)
    themes = build_theme_table(df)

    # 전일 테마 (이력 우선, 없으면 재조회)
    hist = load_history()
    prev_themes = None
    if prev_ymd:
        if prev_ymd in hist and hist[prev_ymd].get("themes"):
            prev_themes = pd.DataFrame(hist[prev_ymd]["themes"])
        else:
            prev_themes = build_theme_table(load_day(prev_ymd))

    # ── 데이터 섹션 ──
    sections = []

    top_lines = [f"{r.theme}  {fmt_eok(r.value)} · {r.rate:+.2f}%  ({r.leaders})"
                 for r in themes.head(8).itertuples()]
    sec_top = make_section(SECTION_ICONS["테마"], "오늘의 TOP 테마 (거래대금순)", top_lines, numbered=True)

    change_lines, inflow, outflow = [], [], []
    if prev_themes is not None and not prev_themes.empty and not themes.empty:
        m = themes.merge(prev_themes[["theme", "rank", "value"]], on="theme",
                         how="left", suffixes=("", "_prev"))
        for r in m.head(8).itertuples():
            if pd.isna(r.rank_prev):
                change_lines.append(f"{r.theme}  🆕 신규 진입 ({r.rank}위)")
            else:
                diff = int(r.rank_prev - r.rank)
                tag = f"🔺{diff}" if diff > 0 else (f"🔻{-diff}" if diff < 0 else "➖")
                change_lines.append(f"{r.theme}  {int(r.rank_prev)}위 → {r.rank}위 ({tag})")
        m["diff"] = m["value"] - m["value_prev"].fillna(0)
        inflow = [f"{r.theme}  +{fmt_eok(r.diff)} (전일 {fmt_eok(0 if pd.isna(r.value_prev) else r.value_prev)})"
                  for r in m.sort_values("diff", ascending=False).head(5).itertuples() if r.diff > 0]
        outflow = [f"{r.theme}  -{fmt_eok(abs(r.diff))} (전일 {fmt_eok(r.value_prev)})"
                   for r in m.sort_values("diff").head(5).itertuples()
                   if r.diff < 0 and not pd.isna(r.value_prev)]
    else:
        change_lines = ["전일 비교 데이터가 아직 없습니다 (내일부터 표시)"]

    sec_change = make_section(SECTION_ICONS["변동"], "테마 순위 변동 (전일 대비)", change_lines, numbered=True)
    sec_in = make_section(SECTION_ICONS["유입"], "거래대금 유입 (순환 ↑)", inflow, numbered=True)
    sec_out = make_section(SECTION_ICONS["이탈"], "거래대금 이탈 (순환 ↓)", outflow, numbered=True)

    market_secs = []
    for mk in ["유가증권", "코스닥", "ETF"]:
        sub = df[df["market"] == mk].sort_values("value", ascending=False).head(7)
        lines = [f"{r.name}  {fmt_eok(r.value)} · {r.rate:+.2f}%" for r in sub.itertuples()]
        market_secs.append(make_section(ICONS[mk], f"{mk} 거래대금 TOP", lines, numbered=True))

    liquid = df[(df["market"] != "ETF") & (df["value"] >= 100)].sort_values("rate", ascending=False).head(7)
    sec_up = make_section(SECTION_ICONS["상승"], "상승률 TOP (거래대금 100억↑)",
                          [f"{r.name}  {r.rate:+.2f}% · {fmt_eok(r.value)}" for r in liquid.itertuples()],
                          numbered=True)

    # ── AI A: 총평 + 테마/섹터 정리 ──
    data_brief = (
        f"기준일 {nice}\n[TOP 테마]\n" + "\n".join(top_lines) +
        "\n[순위변동]\n" + "\n".join(change_lines) +
        "\n[유입]\n" + "\n".join(inflow) + "\n[이탈]\n" + "\n".join(outflow) +
        "\n[상승률 TOP]\n" + "\n".join(f"{r.name} {r.rate:+.1f}%" for r in liquid.itertuples())
    )
    ai_a = call_groq(
        data_brief + "\n\n위 수급 데이터를 바탕으로 아래 태그 형식으로만 답하라. "
        "각 태그 아래 줄마다 '- '로 시작하는 짧은 문장 2~4개.\n[총평]\n[테마정리]\n[섹터정리]")
    blocks_a = parse_blocks(ai_a, ["총평", "테마정리", "섹터정리"]) if ai_a else {}
    if blocks_a and any(blocks_a.values()):
        sec_summary = make_section(SECTION_ICONS["총평"], "시장 총평", blocks_a["총평"])
        sec_theme_sum = make_section(SECTION_ICONS["정리"], "테마·섹터 정리",
                                     blocks_a["테마정리"] + blocks_a["섹터정리"])
    else:
        sec_summary = make_section("⏳", "시장 총평", ["AI 요약 연결 지연 또는 모델 오류 (데이터 리포트만 전송)"])
        sec_theme_sum = None

    # ── AI B: 전일 리뷰 / 예상 테마 / 순환 / 요약 ──
    prev_expect = ""
    if prev_ymd and prev_ymd in hist:
        prev_expect = hist[prev_ymd].get("expect", "")
    ai_b = call_groq(
        data_brief + f"\n\n[전일 예상 테마]\n{prev_expect or '기록 없음'}\n\n"
        "전일 예상과 오늘 결과를 비교해 아래 태그 형식으로만 답하라. 줄마다 '- '로 시작하는 짧은 문장 2~4개. "
        "예상 테마는 테마명만 쉼표로 3개 이내 먼저 쓰고 근거를 덧붙여라.\n[전일리뷰]\n[예상테마]\n[순환섹터]\n[정리요약]")
    blocks_b = parse_blocks(ai_b, ["전일리뷰", "예상테마", "순환섹터", "정리요약"]) if ai_b else {}
    ai_secs = []
    if blocks_b and any(blocks_b.values()):
        ai_secs = [
            make_section(SECTION_ICONS["리뷰"], "전일 예상 리뷰", blocks_b["전일리뷰"]),
            make_section(SECTION_ICONS["예상"], "다음 거래일 예상 테마", blocks_b["예상테마"]),
            make_section(SECTION_ICONS["순환"], "순환 예상 섹터", blocks_b["순환섹터"]),
            make_section(SECTION_ICONS["요약"], "한 줄 정리", blocks_b["정리요약"]),
        ]

    # 섹션 조립 (분량 균형은 split_balanced 가 처리)
    sections = [sec_summary, sec_top, sec_change, sec_in, sec_out] + market_secs + [sec_up]
    if sec_theme_sum:
        sections.append(sec_theme_sum)
    sections += ai_secs

    header = f"📊 <b>예상 섹터 테마 리포트</b>\n🗓 기준일 {nice} · ⏰ {dt.datetime.now(KST):%H:%M} 발송"
    send_telegram(sections, header)

    # ── 이력/데이터 저장 ──
    hist[today_ymd] = {
        "themes": themes.to_dict("records"),
        "expect": " / ".join(blocks_b.get("예상테마", [])) if blocks_b else "",
    }
    save_history(hist)
    (DATA_DIR / "latest.json").write_text(
        json.dumps({"date": nice, "themes": themes.to_dict("records")}, ensure_ascii=False, indent=1),
        encoding="utf-8")


if __name__ == "__main__":
    main()
