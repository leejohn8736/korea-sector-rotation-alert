"""
BNF 25일선 이격도 역추세 신호 알림 시스템 + 5대 핵심 테마 우량·성장주 20선 연동
- 단계1 관찰: 종가가 25EMA 대비 기준 이격도 이하
- 단계2 대기: + 최근 RSI(14) 과매도(<=30)
- 매수 신호: 최근 N일 내 이격도·RSI 조건 충족 + 오늘 MACD 히스토그램이 0선 아래→위 전환
"""

import json
import os
import sys
import time
import datetime as dt
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CFG_FILE = ROOT / "config.json"

if CFG_FILE.exists():
    CFG = json.loads(CFG_FILE.read_text(encoding="utf-8"))
else:
    CFG = {
        "rules": {"lookback_days": 5, "rsi_len": 14, "rsi_oversold": 30, "ema_len": 25, "stop_lookback_days": 5, "regime_ma_len": 200},
        "thresholds": {"large": {"bull": -5.0, "bear": -8.0}, "volatile": {"bull": -8.0, "bear": -12.0}},
        "KR": {"regime_index": "^KS200", "kospi_top_n": 30, "kosdaq_top_n": 30},
        "US": {"nasdaq100": ["QQQ"], "large": ["AAPL", "MSFT"], "volatile": ["TSLA"]}
    }

STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "docs" / "data"
STATE_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

R = CFG["rules"]
KST = dt.timezone(dt.timedelta(hours=9))

# [신규 반영] 5대 핵심 테마별 지속 수혜 대표 우량·성장주 10선 (노트북 소스 기반)
THEME_WATCHLIST = {
    # 1. 커스텀 HBM4 & 유리기판·어드밴스드 패키징
    "000660.KS": {"name": "SK하이닉스", "group": "large", "theme": "HBM4/패키징 대장주"},
    "005930.KS": {"name": "삼성전자", "group": "large", "theme": "2nm파운드리/패키징 대장주"},

    # 2. AI 데이터센터 전력망 & SMR (원전)
    "329180.KS": {"name": "HD현대중공업", "group": "large", "theme": "데이터센터 전력/엔진 대장주"},
    "009540.KS": {"name": "HD한국조선해양", "group": "large", "theme": "친환경 선박/에너지 대장주"},

    # 3. K-방산 & 우주항공
    "012450.KS": {"name": "한화에어로스페이스", "group": "large", "theme": "글로벌 방산/우주항공 대장주"},
    "082920.KQ": {"name": "비츠로셀", "group": "volatile", "theme": "방산 전지/스마트그리드 우량주"},

    # 4. AI 반도체 테스트 소부장 및 로봇 (추가 연동)
    "042700.KS": {"name": "한미반도체", "group": "large", "theme": "AI 반도체 HBM 장비"},
    "277810.KQ": {"name": "레인보우로보틱스", "group": "volatile", "theme": "지능형 로봇 대장주"},

    # 5. 바이오시밀러 및 플랫폼
    "196170.KQ": {"name": "알테오젠", "group": "volatile", "theme": "바이오 기술수출 대장주"},
    "068270.KQ": {"name": "셀트리온제약", "group": "volatile", "theme": "바이오시밀러 우량주"}
}

### ---------------------------------------------------------------- 유니버스
def kr_universe():
    """코스피 시총 상위 N + 코스닥 시총 상위 N + 5대 테마 우량주 10선 병합"""
    cache = STATE_DIR / "kr_universe.json"
    try:
        import FinanceDataReader as fdr
        out = {}
        for mkt, n, grp, sfx in (("KOSPI", CFG["KR"]["kospi_top_n"], "large", ".KS"),
                                 ("KOSDAQ", CFG["KR"]["kosdaq_top_n"], "volatile", ".KQ")):
            df = fdr.StockListing(mkt)
            df = df[df["Code"].astype(str).str.fullmatch(r"\d{6}")]
            df = df.sort_values("Marcap", ascending=False).head(n)
            for _, r in df.iterrows():
                out[r["Code"] + sfx] = {"name": r["Name"], "group": grp, "theme": "시총 상위"}
        
        # 5대 테마 우량주 병합
        for t, v in THEME_WATCHLIST.items():
            out[t] = v

        if len(out) > 30:
            cache.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
            return out
    except Exception as e:
        print("KR 종목목록 조회 실패, 캐시 사용:", e)
    
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    return THEME_WATCHLIST

def us_universe():
    u = CFG["US"]
    out = {}
    for t in u["nasdaq100"] + u["large"]:
        out[t] = {"name": t, "group": "large", "theme": "미국 대형주"}
    for t in u["volatile"]:
        out[t] = {"name": t, "group": "volatile", "theme": "미국 변동성주"}
    return out

### ---------------------------------------------------------------- 지표 계산 함수
def rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

def indicators(df):
    c = df["Close"]
    ema = c.ewm(span=R["ema_len"], adjust=False).mean()
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    out = pd.DataFrame({
        "close": c, "high": df["High"], "low": df["Low"], "volume": df["Volume"],
        "ema": ema, "dev": (c / ema - 1) * 100, "rsi": rsi(c, R["rsi_len"]), "hist": hist,
    })
    out["vol_ratio"] = out["volume"] / out["volume"].rolling(20).mean()
    return out.dropna(subset=["close", "ema"])

def download(tickers, period="2y"):
    frames = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        for attempt in range(3):
            try:
                d = yf.download(chunk, period=period, auto_adjust=True, progress=False,
                                group_by="ticker", threads=True)
                break
            except Exception as e:
                print("다운로드 재시도", attempt, e)
                time.sleep(5)
        else:
            continue
        for t in chunk:
            try:
                sub = d[t] if len(chunk) > 1 else d
                sub = sub.dropna(subset=["Close"])
                if len(sub) > 60:
                    frames[t] = sub
            except Exception:
                pass
    return frames

def telegram(text):
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[텔레그램 미설정] 메시지:\n" + text)
        return False
    for part in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                            data={"chat_id": chat, "text": part, "disable_web_page_preview": "true"}, timeout=20)
            if not r.ok:
                print("텔레그램 전송 실패:", r.status_code, r.text[:200])
                return False
        except Exception as e:
            print("텔레그램 전송 실패:", e)
            return False
    return True

### ---------------------------------------------------------------- 메인 실행
def fmt_px(x, market):
    return f"{x:,.0f}" if market == "KR" else f"{x:,.2f}"

def run(market):
    universe = kr_universe() if market == "KR" else us_universe()
    idx_t = CFG[market]["regime_index"]
    print(f"[{market}] 종목 {len(universe)}개 다운로드")
    data = download(list(universe) + [idx_t])

    regime = "bull"
    idx_info = {}
    if idx_t in data:
        ic = data[idx_t]["Close"]
        ma = ic.rolling(R["regime_ma_len"]).mean()
        regime = "bull" if ic.iloc[-1] >= ma.iloc[-1] else "bear"
        idx_info = {"ticker": idx_t, "close": round(float(ic.iloc[-1]), 2),
                    "ma": round(float(ma.iloc[-1]), 2)}

    pos_file = STATE_DIR / f"positions_{market.lower()}.json"
    hist_file = STATE_DIR / f"history_{market.lower()}.json"
    meta_file = STATE_DIR / f"meta_{market.lower()}.json"
    
    positions = json.loads(pos_file.read_text("utf-8")) if pos_file.exists() else []
    history = json.loads(hist_file.read_text("utf-8")) if hist_file.exists() else []
    meta = json.loads(meta_file.read_text("utf-8")) if meta_file.exists() else {}

    rows, buys = [], []
    asof = None
    lb = R["lookback_days"]
    
    for t, info in universe.items():
        if t not in data:
            continue
        ind = indicators(data[t])
        if len(ind) < 40:
            continue
        last = ind.iloc[-1]
        d_last = ind.index[-1].date()
        asof = max(asof, d_last) if asof else d_last
        
        group_key = info.get("group", "large")
        th = CFG["thresholds"][group_key][regime]
        win = ind.iloc[-lb:]
        dev_ok_now = last["dev"] <= th
        dev_ok_recent = win["dev"].min() <= th
        rsi_ok_recent = win["rsi"].min() <= R["rsi_oversold"]
        cross = ind["hist"].iloc[-2] <= 0 < ind["hist"].iloc[-1]

        stage = 0
        if dev_ok_now:
            stage = 1
        if dev_ok_recent and rsi_ok_recent:
            stage = max(stage, 2)
        if dev_ok_recent and rsi_ok_recent and cross:
            stage = 3

        close_val = float(last["close"])
        row = {
            "ticker": t, "name": info["name"], "group": group_key, "th": th,
            "close": round(close_val, 4), "ema": round(float(last["ema"]), 4),
            "dev": round(float(last["dev"]), 2), "dev_min": round(float(win["dev"].min()), 2),
            "rsi": round(float(last["rsi"]), 1), "rsi_min": round(float(win["rsi"].min()), 1),
            "hist": round(float(last["hist"]), 4), "hist_prev": round(float(ind["hist"].iloc[-2]), 4),
            "vol_ratio": round(float(last["vol_ratio"]), 2) if pd.notna(last["vol_ratio"]) else None,
            "stage": stage, "date": str(d_last),
            "theme": info.get("theme", "일반")
        }
        if stage == 3:
            stop = float(ind["low"].iloc[-R["stop_lookback_days"]:].min())
            entry = close_val
            risk = max(entry - stop, entry * 0.01)
            row.update({"stop": round(stop, 4), "target_ema": round(float(last["ema"]), 4),
                        "target_2r": round(entry + 2 * risk, 4)})
            buys.append(row)
        rows.append(row)

    if asof is None:
        print("데이터 없음")
        return

    asof_s = str(asof)
    now_kst = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    
    msg = f"🎯 [5대 핵심 테마 & BNF 역추세 신호 알림]\n⏰ 기준일: {now_kst}\n\n"
    if buys:
        msg += f"🔥 [매수 신호 발생 종목 ({len(buys)}개)]\n"
        for b in buys:
            msg += f"- {b['name']} ({b['ticker']}) | 종가: {fmt_px(b['close'], market)}원 | 테마: {b['theme']}\n"
    else:
        msg += "☕ 조건에 부합하는 매수 신호 종목이 없습니다."

    print(msg)
    telegram(msg)

if __name__ == "__main__":
    for m in (sys.argv[1:] or ["KR"]):
        run(m.upper())
