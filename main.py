import os
import sys
import datetime as dt
import pandas as pd
import requests
import yfinance as yf

KST = dt.timezone(dt.timedelta(hours=9))

# 1. 사용자 지정 핵심 테마 주도주 20선 (저번 주 & 다음 주 대장주/2등주)
THEME_WATCHLIST = {
    # [Part 1] 저번 주 주도 테마 및 종목 (10개)
    "247540.KQ": {"name": "에코프로비엠", "theme": "저번주(월) 2차전지 대장주"},
    "086520.KQ": {"name": "에코프로", "theme": "저번주(월) 2차전지 2등주"},
    "196170.KQ": {"name": "알테오젠", "theme": "저번주(화) 바이오 대장주"},
    "298380.KQ": {"name": "에이비엘바이오", "theme": "저번주(화) 바이오 2등주"},
    "000660.KS": {"name": "SK하이닉스", "theme": "저번주(수) AI반도체/HBM 대장주"},
    "042700.KS": {"name": "한미반도체", "theme": "저번주(수) AI반도체/HBM 2등주"},
    "003230.KS": {"name": "삼양식품", "theme": "저번주(목) K-푸드 대장주"},
    "004370.KS": {"name": "농심", "theme": "저번주(목) K-푸드 2등주"},
    "010120.KS": {"name": "LS일렉트릭", "theme": "저번주(금) 전력인프라 대장주"},
    "298040.KS": {"name": "효성중공업", "theme": "저번주(금) 전력인프라 2등주"},

    # [Part 2] 다음 주 예상 주도 테마 및 종목 (10개)
    "005380.KS": {"name": "현대차", "theme": "다음주(월) 밸류업/자율주행 예상대장"},
    "000270.KS": {"name": "기아", "theme": "다음주(월) 밸류업/자율주행 예상2등"},
    "005930.KS": {"name": "삼성전자", "theme": "다음주(화) 파운드리/딥테크 예상대장"},
    "007660.KS": {"name": "이수페타시스", "theme": "다음주(화) 파운드리/딥테크 예상2등"},
    "277810.KQ": {"name": "레인보우로보틱스", "theme": "다음주(수) 지능형로봇 예상대장"},
    "454910.KS": {"name": "두산로보틱스", "theme": "다음주(수) 지능형로봇 예상2등"},
    "012450.KS": {"name": "한화에어로스페이스", "theme": "다음주(목) 방산/우주항공 예상대장"},
    "079550.KS": {"name": "LIG넥스원", "theme": "다음주(목) 방산/우주항공 예상2등"},
    "058470.KQ": {"name": "리노공업", "theme": "다음주(금) 반도체소부장 예상대장"},
    "039030.KQ": {"name": "이오테크닉스", "theme": "다음주(금) 반도체소부장 예상2등"},
}

# 2. 텔레그램 안전 전송 함수 (특수문자 파싱 오류 원천 차단)[cite: 2]
def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[텔레그램 미설정] 출력 내용:\n" + text)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for part in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        try:
            res = requests.post(url, data={"chat_id": chat_id, "text": part, "disable_web_page_preview": "true"}, timeout=15)
            if not res.ok:
                print("텔레그램 전송 실패:", res.text)
        except Exception as e:
            print("텔레그램 통신 에러:", e)

# 3. Groq AI 시장 및 주도주 요약 함수
def analyze_with_groq(summary_data):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "⚠️ GROQ_API_KEY가 깃허브 Secrets에 설정되지 않아 AI 요약을 건너뜁니다."
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": "당신은 한국 주식 시장(코스피/코스닥) 전문 애널리스트입니다. 전달받은 20개 핵심 테마주 및 등락률/거래대금 상위 종목 데이터를 보고, 오늘 어떤 테마로 순환매가 도는지 핵심만 4~5줄로 명쾌하게 요약해 주세요."},
            {"role": "user", "content": summary_data}
        ],
        "temperature": 0.3
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=20)
        if res.ok:
            return res.json()["choices"][0]["message"]["content"]
        return f"Groq API 오류: {res.text[:150]}"
    except Exception as e:
        return f"Groq 연결 실패: {e}"

# 4. 데이터 수집, Pandas 필터링 및 실행 메인 로직
def run():
    tickers = list(THEME_WATCHLIST.keys())
    print(f"총 {len(tickers)}개 핵심 테마 종목 데이터 수집 중...")
    
    raw_data = yf.download(tickers, period="5d", auto_adjust=True, progress=False, group_by="ticker", threads=True)
    rows = []

    for ticker, info in THEME_WATCHLIST.items():
        try:
            df = raw_data[ticker].dropna(subset=["Close"])
            if len(df) < 2:
                continue
            close = float(df["Close"].iloc[-1])
            prev_close = float(df["Close"].iloc[-2])
            change_pct = ((close - prev_close) / prev_close) * 100
            volume = float(df["Volume"].iloc[-1])
            trading_value = close * volume  # 대략적인 거래대금

            rows.append({
                "ticker": ticker,
                "name": info["name"],
                "theme": info["theme"],
                "close": close,
                "change_pct": round(change_pct, 2),
                "trading_value": trading_value
            })
        except Exception as e:
            print(f"{info['name']}({ticker}) 처리 오류:", e)

    if not rows:
        print("데이터를 불러오지 못했습니다.")
        return

    # Pandas 가공: 등락률 상위 & 거래대금 상위 추출
    df_all = pd.DataFrame(rows)
    df_top_change = df_all.sort_values(by="change_pct", ascending=False).head(5)
    df_top_value = df_all.sort_values(by="trading_value", ascending=False).head(5)

    now_kst = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    
    # 데이터 요약 텍스트 생성
    report_body = f"📊 [핵심 테마 20선 실시간 체크 & 주도주 리포트]\n⏰ 기준: {now_kst}\n\n"
    
    report_body += "🚀 [20선 중 당일 등락률 상위 Top 5]\n"
    for _, r in df_top_change.iterrows():
        report_body += f"- {r['name']} ({r['change_pct']:+.2f}%) | {r['close']:,.0f}원 [{r['theme']}]\n"

    report_body += "\n💰 [20선 중 당일 거래대금 상위 Top 5]\n"
    for _, r in df_top_value.iterrows():
        val_억 = r['trading_value'] / 100_000_000
        report_body += f"- {r['name']} (약 {val_억:,.0f}억) | {r['change_pct']:+.2f}% [{r['theme']}]\n"

    # Groq AI 인사이트 추가
    ai_summary = analyze_with_groq(report_body)
    final_msg = report_body + "\n🤖 [Groq AI 순환매 & 재료 요약]\n" + ai_summary

    print(final_msg)
    send_telegram(final_msg)

if __name__ == "__main__":
    run()
