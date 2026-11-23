import urllib.parse

import altair as alt
import pandas as pd
import streamlit as st

import analyze
import bot
import prices
import trader

st.set_page_config(page_title="dart-bot", layout="wide")
bot.check_env()


def load_stats(name="event_stats.csv"):  # 작은 파일이라 캐시 안 함 → 분석 다시 돌리면 바로 반영
    p = bot.ROOT / "data" / name
    return pd.read_csv(p) if p.exists() else None


@st.cache_data(ttl=600)
def load_recent(days):
    since = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y%m%d")
    rows, offset = [], 0
    while True:
        page = bot.sb("GET", "disclosures?select=rcept_dt,category&category=not.is.null"
                             f"&rcept_dt=gte.{since}&order=rcept_no&limit=1000&offset={offset}")
        if not page:
            return pd.DataFrame(rows, columns=["rcept_dt", "category"])
        rows += page
        offset += 1000


@st.cache_data(ttl=60)
def load_trades():
    return pd.DataFrame(bot.sb("GET", "trades?select=*&order=buy_date.desc"))


@st.cache_data(ttl=60)
def load_account():
    return bot.sb("GET", "account?select=*&id=eq.1")


@st.cache_data(ttl=60)
def load_now(codes):
    """종목코드 → 현재가. 네이버 일봉의 오늘 종가 = 장중 현재가"""
    return {c: int(prices.fetch_chart(c, 1)[-1][4]) for c in codes}


@st.cache_data(ttl=600)
def search(name):
    q = urllib.parse.quote(f"*{name}*")
    rows = bot.sb("GET", "disclosures?select=rcept_dt,corp_name,stock_code,report_nm,category"
                         f"&corp_name=ilike.{q}&order=rcept_dt.desc&limit=200")
    return pd.DataFrame(rows)


st.title("dart-bot")
st.caption("DART 공시 유형별로 공시 뒤 주가가 어떻게 움직였는지")

tab1, tab4, tab5, tab2, tab3 = st.tabs(["유형별 결과", "자사주 매입 규모", "모의투자", "공시 추이", "회사 검색"])

with tab1:
    stats = load_stats()
    if stats is None:
        st.info("data/event_stats.csv가 없음 → python3 analyze.py 먼저 실행")
    else:
        h = st.radio("기간", [1, 3, 5], index=2, horizontal=True, format_func=lambda d: f"{d}거래일")
        view = pd.DataFrame({
            "유형": stats["category"],
            "표본": stats[f"n_{h}d"],
            "평균": stats[f"mean_{h}d"] * 100,
            "중앙값": stats[f"median_{h}d"] * 100,
            "하락 비율": stats[f"down_{h}d"] * 100,
            "p값": stats[f"p_{h}d"],
        })
        view["판단"] = ["표본 부족" if n < analyze.MIN_SAMPLE else ("유의" if p < 0.05 else "우연일 수 있음")
                      for n, p in zip(view["표본"], view["p값"])]
        view = view.sort_values("평균")

        # 가로 막대: 유형 이름이 세로로 눕지 않게, 평균 순서대로
        long = view.melt(id_vars="유형", value_vars=["평균", "중앙값"], var_name="지표", value_name="초과수익률(%)")
        st.altair_chart(alt.Chart(long).mark_bar().encode(
            y=alt.Y("유형:N", sort=list(view["유형"]), title=None),
            yOffset="지표:N",
            x=alt.X("초과수익률(%):Q"),
            color=alt.Color("지표:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=["유형", "지표", alt.Tooltip("초과수익률(%):Q", format="+.2f")],
        ), width="stretch")
        st.dataframe(view, hide_index=True, column_config={
            "평균": st.column_config.NumberColumn(format="%+.2f%%"),
            "중앙값": st.column_config.NumberColumn(format="%+.2f%%"),
            "하락 비율": st.column_config.NumberColumn(format="%.0f%%"),
            "p값": st.column_config.NumberColumn(format="%.3f"),
        })
        st.caption("기준가: 공시 다음 거래일 시가 · 초과수익률 = 종목 수익률 - 지수 수익률 · "
                   "정정공시, 거래정지, 거래량 0인 날 제외")

with tab4:
    bb = load_stats("buyback_stats.csv")
    if bb is None:
        st.info("data/buyback_stats.csv가 없음 → python3 buyback.py 먼저 실행")
    else:
        st.caption("자사주취득 공시를 매입 비율(취득예정 주식 수 / 상장주식수) 순으로 3등분 · 3거래일 초과수익률")
        view = pd.DataFrame({
            "그룹": bb["group"],
            "매입 비율": [f"{a:.2%} ~ {b:.2%}" for a, b in zip(bb["ratio_min"], bb["ratio_max"])],
            "표본": bb["n"],
            "평균": bb["mean"] * 100,
            "중앙값": bb["median"] * 100,
            "하락 비율": bb["down"] * 100,
            "p값": bb["p"],
        })
        view["판단"] = ["유의" if p < 0.05 else "우연일 수 있음" for p in view["p값"]]
        long = view.melt(id_vars="그룹", value_vars=["평균", "중앙값"], var_name="지표", value_name="초과수익률(%)")
        st.altair_chart(alt.Chart(long).mark_bar().encode(
            x=alt.X("그룹:N", sort=["하위", "중간", "상위"], title=None, axis=alt.Axis(labelAngle=0)),
            xOffset="지표:N",
            y=alt.Y("초과수익률(%):Q"),
            color=alt.Color("지표:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=["그룹", "지표", alt.Tooltip("초과수익률(%):Q", format="+.2f")],
        ), width="stretch")
        st.dataframe(view, hide_index=True, column_config={
            "평균": st.column_config.NumberColumn(format="%+.2f%%"),
            "중앙값": st.column_config.NumberColumn(format="%+.2f%%"),
            "하락 비율": st.column_config.NumberColumn(format="%.0f%%"),
            "p값": st.column_config.NumberColumn(format="%.3f"),
        })
        st.caption(f"모의투자 봇은 매입 비율 {trader.MIN_RATIO:.0%} 미만은 매수 제외, 비율 큰 것부터 매수 · "
                   "상장주식수는 현재 기준이라 오래된 공시는 그사이 증자·감자만큼 어긋남")

with tab5:
    st.markdown(f"""
**규칙** (KIS 모의계좌)
1. 대상: 전 거래일 **{trader.CATEGORY}** 공시 (정정공시 제외)
2. 매입 비율(취득예정 주식 / 상장주식)이 **{trader.MIN_RATIO:.0%} 미만**이거나 규모를 모르면 매수 제외
3. 매입 비율 **큰 것부터**, 최대 **{trader.MAX_HOLD}종목**
4. 08:40 종목당 **{trader.BUDGET:,}원** 시장가 매수 → 09:00 시가 체결 (거절되면 09:05 재시도)
5. 보유 중인 종목, 1주가 {trader.BUDGET:,}원 넘는 종목 제외
6. 매수일 포함 **{trader.HOLD_DAYS}거래일째 15:20** 시장가 매도 → 종가 체결
7. 15:40 체결가로 실현수익률 계산 → 텔레그램
""")
    acc = load_account()
    trades = load_trades()
    held = trades[trades["sell_date"].isna()] if len(trades) else trades
    now = load_now(tuple(held["stock_code"])) if len(held) else {}
    if acc:
        value = sum(now[c] * q for c, q in zip(held["stock_code"], held["qty"]))
        c1, c2 = st.columns(2)
        c1.metric("예수금 (D+2)", f"{acc[0]['cash']:,}원")
        c2.metric("추정자산 (예수금 + 평가금액)", f"{acc[0]['cash'] + value:,}원")
        st.caption(f"예수금은 {acc[0]['updated_at']} 기준 (09:05 매수, 15:40 정산 때 갱신) · 현재가는 네이버 기준, 최대 1분 늦음")
    if trades.empty:
        st.info("아직 거래 없음")
    else:
        cur = trades["stock_code"].map(now).where(trades["sell_date"].isna())
        done = trades.dropna(subset=["sell_price"])
        ret = done["sell_price"] / done["buy_price"] - 1
        if len(done):
            st.write(f"청산 **{len(done)}건**, 평균 실현수익률 **{ret.mean():+.2%}** (수수료·세금 전)")
        st.dataframe(pd.DataFrame({
            "종목": trades["corp_name"],
            "매수일": pd.to_datetime(trades["buy_date"]).dt.strftime("%m/%d"),
            "상태": ["보유" if pd.isna(d) else ("청산" if pd.notna(p) else "매도 주문 (정산 대기)")
                   for d, p in zip(trades["sell_date"], trades["sell_price"])],
            "수량(주)": trades["sell_qty"].fillna(trades["qty"]).astype(int),
            "매수 평균가": trades["buy_price"],
            "현재가": cur,
            "매도 평균가": trades["sell_price"],
            "수익률": (trades["sell_price"].fillna(cur) / trades["buy_price"] - 1) * 100,
        }), hide_index=True, column_config={
            "매수 평균가": st.column_config.NumberColumn(format="%,.0f"),
            "현재가": st.column_config.NumberColumn(format="%,.0f"),
            "매도 평균가": st.column_config.NumberColumn(format="%,.0f"),
            "수익률": st.column_config.NumberColumn(format="%+.2f%%"),
        })

with tab2:
    days = st.select_slider("기간", [30, 90, 180, 365], value=90, format_func=lambda d: f"최근 {d}일")
    recent = load_recent(days)
    if recent.empty:
        st.info("데이터 없음")
    else:
        recent["날짜"] = pd.to_datetime(recent["rcept_dt"])
        daily = recent.pivot_table(index="날짜", columns="category", aggfunc="size", fill_value=0)
        st.bar_chart(daily)
        st.dataframe(recent["category"].value_counts().rename("건수"))

with tab3:
    name = st.text_input("회사 이름")
    if name:
        found = search(name)
        st.write(f"{len(found)}건")
        st.dataframe(found, hide_index=True)
