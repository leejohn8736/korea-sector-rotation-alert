"""
KRX OPEN API 연동 버전 - 주식 섹터 순환매 분석 봇 (v2.4 - 라이브 데이터 버전)
개선 사항:
- 순환매 초기 신호 감지 (바닥 반등 패턴)
- 다중 지표 통합 분석 (거래대금, RS, 모멘텀)
- 섹터별 순환매 점수 시스템
- 전체 35개 섹터 커버리지
- 텔레그램 알림 기능 추가
- 승인된 API 기간에 맞는 실시간 최근 데이터 조회
"""

import requests
import pandas as pd
import datetime
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os
import time

# .env 불러오기
load_dotenv()
KRX_API_KEY = os.getenv('KRX_API_KEY')
if not KRX_API_KEY:
    raise ValueError("KRX_API_KEY가 .env 파일에 없습니다.")

# 텔레그램 설정
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# config.yaml 불러오기
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

market_ticker = config['market_ticker']
sectors = config['sectors']
etf_list = list(sectors.keys())

analysis_config = config['analysis']
period_days = analysis_config['period_days']
rs_window = analysis_config['rs_window']
volume_recent = analysis_config['volume_compare_recent']
volume_prev = analysis_config['volume_compare_prev']
strong_threshold = analysis_config['volume_threshold_strong']
medium_threshold = analysis_config['volume_threshold_medium']

# 순환매 감지 설정
rotation_config = analysis_config.get('rotation_detection', {})
long_term_period = rotation_config.get('long_term_period', 60)
short_term_period = rotation_config.get('short_term_period', 10)
medium_term_period = rotation_config.get('medium_term_period', 20)
undervalued_threshold = rotation_config.get('undervalued_threshold', -10)
bounce_threshold = rotation_config.get('bounce_threshold', 5)
volume_surge_ratio = rotation_config.get('volume_surge_ratio', 1.5)

# 가중치
weight_undervalued = rotation_config.get('weight_undervalued', 2)
weight_bounce = rotation_config.get('weight_bounce', 3)
weight_volume = rotation_config.get('weight_volume', 2)
weight_rs_improve = rotation_config.get('weight_rs_improve', 1)

# 출력 설정
output_config = config.get('output', {})
show_top_n = output_config.get('show_top_n', 15)
min_rotation_score = output_config.get('min_rotation_score', 4)
show_news = output_config.get('show_news', True)
max_news = output_config.get('max_news', 3)

# 알림 설정
alerts_config = config.get('alerts', {})
enable_alerts = alerts_config.get('enable', False)
rotation_score_threshold = alerts_config.get('rotation_score_threshold', 6)

# 날짜 설정: 승인된 API 기간(2026년 현재)에 맞게 어제 날짜 기준으로 설정
end_date_dt = datetime.date.today() - datetime.timedelta(days=1)
start_date_dt = end_date_dt - datetime.timedelta(days=period_days)

print(f"{'='*80}")
print(f"KRX 섹터 순환매 분석 시스템 v2.4")
print(f"{'='*80}")
print(f"데이터 기간: {start_date_dt} ~ {end_date_dt}")
print(f"분석 섹터: {len(sectors)}개")
if enable_alerts and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    print(f"텔레그램 알림: ✓ 활성화 (점수 {rotation_score_threshold}점 이상)")
else:
    print(f"텔레그램 알림: ✗ 비활성화")
print(f"{'='*80}\n")


def send_telegram_message(message):
    """텔레그램 메시지 전송"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print("  ✓ 텔레그램 알림 전송 완료")
            return True
        else:
            print(f"  ✗ 텔레그램 알림 실패: {response.status_code}")
            return False
    except Exception as e:
        print(f"  ✗ 텔레그램 알림 오류: {e}")
        return False


def get_krx_etf_daily_single(base_date):
    """KRX Open API - ETF 일별매매정보 (단일 날짜)"""
    url = "https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd"
    
    headers = {
        'AUTH_KEY': KRX_API_KEY,
        'Content-Type': 'application/json'
    }
    
    payload = {
        'basDd': base_date
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return None
        
        data = response.json()
        
        if 'OutBlock_1' not in data or not data['OutBlock_1']:
            return None
        
        df = pd.DataFrame(data['OutBlock_1'])
        df['날짜'] = pd.to_datetime(base_date, format='%Y%m%d')
        
        return df
        
    except Exception as e:
        return None


def get_krx_etf_daily(tickers, start_date, end_date):
    """KRX Open API - ETF 일별매매정보 (기간 조회)"""
    all_data = []
    current_date = start_date
    
    print(f"데이터 수집 시작...")
    
    while current_date <= end_date:
        if current_date.weekday() < 5:  # 월~금
            base_date_str = current_date.strftime('%Y%m%d')
            print(f"  조회: {base_date_str}", end=' ')
            
            df = get_krx_etf_daily_single(base_date_str)
            
            if df is not None and len(df) > 0:
                all_data.append(df)
                print(f"✓ ({len(df)}개)")
            else:
                print("✗ (휴장일 또는 데이터 없음)")
            
            time.sleep(0.5)  # 부하 방지
        
        current_date += datetime.timedelta(days=1)
    
    if not all_data:
        raise Exception("수집된 데이터가 없습니다. (주말이거나 해당 기간 최신 데이터가 아직 반영되지 않았을 수 있습니다.)")
    
    df_all = pd.concat(all_data, ignore_index=True)
    
    print(f"\n총 수집 레코드: {len(df_all):,}개")
    
    # 숫자 컬럼 변환
    numeric_cols = {
        'TDD_CLSPRC': '종가',
        'ACC_TRDVOL': '거래량',
        'ACC_TRDVAL': '거래대금'
    }
    
    for krx_col, new_col in numeric_cols.items():
        if krx_col in df_all.columns:
            df_all[new_col] = pd.to_numeric(
                df_all[krx_col].astype(str).str.replace(',', '').replace('-', None), 
                errors='coerce'
            )
    
    # 관심 종목만 필터링
    df_filtered = df_all[df_all['ISU_CD'].isin(tickers)].copy()
    
    print(f"필터링 후 레코드: {len(df_filtered):,}개")
    print(f"수집된 종목: {df_filtered['ISU_CD'].nunique()}개 / {len(tickers)}개")
    
    if len(df_filtered) == 0:
        raise Exception("필터링된 종목 데이터가 없습니다.")
    
    # Pivot 테이블 생성
    close_data = df_filtered.pivot(
        index='날짜', 
        columns='ISU_CD', 
        values='종가'
    ).sort_index()
    
    volume_data = df_filtered.pivot(
        index='날짜', 
        columns='ISU_CD', 
        values='거래량'
    ).sort_index()
    
    trade_value = df_filtered.pivot(
        index='날짜', 
        columns='ISU_CD', 
        values='거래대금'
    ).sort_index()
    
    return close_data, volume_data, trade_value


def detect_sector_rotation(close_data, volume_data, trade_value, symbol, market_ticker):
    """개선된 섹터 순환매 감지 로직"""
    result = {
        'rotation_score': 0,
        'is_undervalued': False,
        'is_bouncing': False,
        'has_volume_surge': False,
        'rs_improving': False,
        'long_term_ret': 0,
        'short_term_ret': 0,
        'vol_surge_ratio': 0,
        'current_rs': 0,
        'past_rs': 0
    }
    
    try:
        if symbol not in close_data.columns:
            return result

        # 1. 장기 약세 확인 (바닥권)
        if len(close_data[symbol].dropna()) >= long_term_period:
            long_ret = close_data[symbol].pct_change(long_term_period).iloc[-1] * 100
            result['long_term_ret'] = round(long_ret, 2)
            result['is_undervalued'] = long_ret < undervalued_threshold
        
        # 2. 단기 반등 확인
        if len(close_data[symbol].dropna()) >= short_term_period:
            short_ret = close_data[symbol].pct_change(short_term_period).iloc[-1] * 100
            result['short_term_ret'] = round(short_ret, 2)
            result['is_bouncing'] = short_ret > bounce_threshold
        
        # 3. 거래량 급증 확인
        if symbol in trade_value.columns and len(trade_value[symbol].dropna()) >= 15:
            recent_vol = trade_value[symbol].tail(5).mean()
            base_vol = trade_value[symbol].iloc[:-5].tail(20).mean()
            
            if base_vol > 0 and pd.notna(base_vol):
                vol_ratio = recent_vol / base_vol
                result['vol_surge_ratio'] = round(vol_ratio, 2)
                result['has_volume_surge'] = vol_ratio > volume_surge_ratio
        
        # 4. 상대강도 개선 확인
        if market_ticker in close_data.columns and symbol in close_data.columns:
            if len(close_data) >= medium_term_period + 5:
                past_market = close_data[market_ticker].pct_change(medium_term_period).iloc[-21]
                past_sector = close_data[symbol].pct_change(medium_term_period).iloc[-21]
                past_rs = (past_sector - past_market) * 100 if pd.notna(past_sector) and pd.notna(past_market) else 0
                
                current_market = close_data[market_ticker].pct_change(medium_term_period).iloc[-1]
                current_sector = close_data[symbol].pct_change(medium_term_period).iloc[-1]
                current_rs = (current_sector - current_market) * 100 if pd.notna(current_sector) and pd.notna(current_market) else 0
                
                result['past_rs'] = round(past_rs, 2)
                result['current_rs'] = round(current_rs, 2)
                result['rs_improving'] = current_rs > past_rs
        
        # 5. 순환매 점수 계산
        score = 0
        if result['is_undervalued']:
            score += weight_undervalued
        if result['is_bouncing']:
            score += weight_bounce
        if result['has_volume_surge']:
            score += weight_volume
        if result['rs_improving']:
            score += weight_rs_improve
        
        result['rotation_score'] = score
        
    except Exception as e:
        pass
    
    return result


def get_news_headlines(keyword, max_headlines=3):
    """뉴스 헤드라인 수집"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = f"https://search.naver.com/search.naver?where=news&query={keyword}&sm=tab_opt&sort=1"
    try:
        res = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        titles = soup.select('.news_tit')[:max_headlines]
        return [t.text.strip()[:60] + ('...' if len(t.text) > 60 else '') for t in titles] or ["뉴스 없음"]
    except Exception as e:
        return ["뉴스 조회 실패"]


# 데이터 불러오기 실행
try:
    close_data, volume_data, trade_value = get_krx_etf_daily(
        etf_list + [market_ticker], 
        start_date_dt, 
        end_date_dt
    )
    print(f"\n✓ 데이터 불러오기 성공!")
    print(f"  데이터 shape: {close_data.shape}")
    print(f"  데이터 기간: {close_data.index.min().date()} ~ {close_data.index.max().date()}")
except Exception as e:
    print(f"\n✗ 데이터 불러오기 실패: {e}")
    exit(1)


# 분석 시작
print(f"\n{'='*80}")
print(f"섹터 순환매 분석 시작 (기준일: {end_date_dt.strftime('%Y-%m-%d')})")
print(f"{'='*80}\n")

summary = []

for symbol, name in sectors.items():
    if symbol not in close_data.columns:
        continue
    
    if close_data[symbol].isna().all():
        continue
    
    # 순환매 감지
    rotation_result = detect_sector_rotation(
        close_data, volume_data, trade_value, symbol, market_ticker
    )
    
    # 수급 증가율 계산
    try:
        recent_tv = trade_value[symbol].tail(volume_recent).mean()
        prev_tv = trade_value[symbol].iloc[-(volume_recent + volume_prev):-volume_recent].mean()
        vol_inc = ((recent_tv - prev_tv) / prev_tv * 100) if prev_tv > 0 and pd.notna(prev_tv) else 0
    except:
        vol_inc = 0
    
    # 뉴스 수집
    if show_news:
        headlines = get_news_headlines(name, max_news)
    else:
        headlines = []
    
    summary.append({
        '섹터': name,
        '종목코드': symbol,
        '순환매점수': rotation_result['rotation_score'],
        '수급증가율': round(vol_inc, 1),
        '장기수익률': rotation_result['long_term_ret'],
        '단기수익률': rotation_result['short_term_ret'],
        '거래량배수': rotation_result['vol_surge_ratio'],
        '현재RS': rotation_result['current_rs'],
        '과거RS': rotation_result['past_rs'],
        '바닥권': rotation_result['is_undervalued'],
        '반등중': rotation_result['is_bouncing'],
        '거래량급증': rotation_result['has_volume_surge'],
        'RS개선': rotation_result['rs_improving'],
        '뉴스': headlines
    })
    
    print(f"  [{name}] 순환매 점수: {rotation_result['rotation_score']}/8")

if not summary:
    print("분석된 섹터 데이터가 없습니다.")
    exit(0)

# 결과 정리
df_summary = pd.DataFrame(summary)

# 필터링 및 정렬
df_summary = df_summary[df_summary['순환매점수'] >= min_rotation_score].copy()
df_summary = df_summary.sort_values(by='순환매점수', ascending=False)

if show_top_n:
    df_summary = df_summary.head(show_top_n)

# 텔레그램 알림 전송
if enable_alerts and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    high_score_sectors = df_summary[df_summary['순환매점수'] >= rotation_score_threshold]
    
    if len(high_score_sectors) > 0:
        print(f"\n텔레그램 알림 전송 중...")
        
        telegram_msg = f"🚀 <b>섹터 순환매 신호 감지</b>\n"
        telegram_msg += f"📅 {end_date_dt.strftime('%Y-%m-%d')}\n"
        telegram_msg += f"━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for idx, row in high_score_sectors.iterrows():
            telegram_msg += f"<b>[{row['섹터']}]</b> {row['순환매점수']}/8점 ⭐\n"
            telegram_msg += f"티커: {row['종목코드']}\n"
            telegram_msg += f"📊 장기 {row['장기수익률']:+.1f}% | 단기 {row['단기수익률']:+.1f}%\n"
            telegram_msg += f"💰 거래량 {row['거래량배수']:.1f}배 | 수급 {row['수급증가율']:+.1f}%\n"
            
            if row['뉴스'] and row['뉴스'][0] != "뉴스 없음":
                telegram_msg += f"📰 {row['뉴스'][0]}\n"
            
            telegram_msg += f"\n"
        
        telegram_msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        telegram_msg += f"총 {len(high_score_sectors)}개 섹터 발견"
        
        send_telegram_message(telegram_msg)

# 결과 출력
print(f"\n{'='*80}")
print(f"분석 결과 (상위 {len(df_summary)}개 섹터)")
print(f"{'='*80}\n")

rotation_signals = df_summary[df_summary['순환매점수'] >= 6]
strong_momentum = df_summary[(df_summary['순환매점수'] >= 4) & (df_summary['순환매점수'] < 6)]

if len(rotation_signals) > 0:
    print(f"{'🚀 순환매 초기 신호 (HIGH PRIORITY)':=^80}\n")
    for _, row in rotation_signals.iterrows():
        print(f"[{row['섹터']}] 순환매 점수: {row['순환매점수']}/8 ⭐")
        print(f"  티커: {row['종목코드']}")
        print(f"  📊 수익률: 장기 {row['장기수익률']:+.1f}% {'(바닥권 ✓)' if row['바닥권'] else ''} | 단기 {row['단기수익률']:+.1f}% {'(반등 ✓)' if row['반등중'] else ''}")
        print(f"  💰 거래량: {row['거래량배수']:.2f}배 {'(급증 ✓)' if row['거래량급증'] else ''} | 수급 증가: {row['수급증가율']:+.1f}%")
        print(f"  📈 RS: 현재 {row['현재RS']:+.1f}% | 과거 {row['과거RS']:+.1f}% {'(개선 ✓)' if row['RS개선'] else ''}")
        if row['뉴스']:
            print(f"  📰 최근 뉴스:")
            for i, title in enumerate(row['뉴스'], 1):
                print(f"     {i}. {title}")
        print("-" * 80)

if len(strong_momentum) > 0:
    print(f"\n{'🔥 강한 모멘텀 지속':=^80}\n")
    for _, row in strong_momentum.iterrows():
        print(f"[{row['섹터']}] 순환매 점수: {row['순환매점수']}/8")
        print(f"  티커: {row['종목코드']}")
        print(f"  📊 수익률: 장기 {row['장기수익률']:+.1f}% | 단기 {row['단기수익률']:+.1f}%")
        print(f"  💰 수급 증가: {row['수급증가율']:+.1f}% | RS: {row['현재RS']:+.1f}%")
        if row['뉴스']:
            print(f"  📰 뉴스: {row['뉴스'][0]}")
        print("-" * 80)

print(f"\n{'='*80}")
print(f"분석 완료! 총 {len(df_summary)}개 유망 섹터 발견")
print(f"{'='*80}\n")

# CSV 저장
output_file = f"sector_rotation_analysis_{end_date_dt.strftime('%Y%m%d')}.csv"
df_summary.to_csv(output_file, index=False, encoding='utf-8-sig')
print(f"📁 결과 저장: {output_file}")
