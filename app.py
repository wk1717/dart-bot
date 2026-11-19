import urllib.parse

import altair as alt
import pandas as pd
import streamlit as st

import analyze
import bot

st.set_page_config(page_title="dart-bot", layout="wide")
bot.check_env()


@st.cache_data
def load_stats():
    return pd.read_csv(bot.ROOT / "data" / "event_stats.csv")


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


@st.cache_data(ttl=600)
def search(name):
    q = urllib.parse.quote(f"*{name}*")
    rows = bot.sb("GET", "disclosures?select=rcept_dt,corp_name,stock_code,report_nm,category"
                         f"&corp_name=ilike.{q}&order=rcept_dt.desc&limit=200")
    return pd.DataFrame(rows)


st.title("dart-bot")
st.caption("DART 공시 유형별로 공시 뒤 주가가 어떻게 움직였는지")

tab1, tab2, tab3 = st.tabs(["유형별 결과", "공시 추이", "회사 검색"])

with tab1:
    stats = load_stats()
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
