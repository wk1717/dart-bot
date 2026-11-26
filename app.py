import ast
import html
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

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
def load_cash():
    """봇이 매수·정산 때 저장한 예수금. 현금은 사고팔 때만 바뀜"""
    rows = bot.sb("GET", "account?select=cash&id=eq.1")
    return rows[0]["cash"] if rows else None


@st.cache_data(max_entries=20)
def load_now(codes, slot):
    """slot이 바뀔 때만 새로 받음"""
    return prices.fetch_now(codes)


UP, DOWN = "#f04452", "#3182f6"  # 이익 빨강, 손실 파랑


def color_of(v):
    return UP if v > 0 else DOWN if v < 0 else "gray"


def pct(v, digits=2):
    # 수익률 칸: 부호 따라 빨강/파랑
    return f'<span style="color:{color_of(v)}">{v:+.{digits}f}%</span>'


def list_html(df, right=()):
    """토스증권 웹 목록처럼 표 테두리 없이. 값은 문자열(HTML 가능), right에 든 칸은 오른쪽 정렬, 첫 칸은 굵게"""
    cell = "border:none;padding:14px 12px;"
    th = (cell + "padding-top:8px;padding-bottom:8px;border-bottom:1px solid rgba(128,128,128,.2);"
          "background:none;font-weight:400;opacity:.55;font-size:.8rem;")
    align = lambda c: f'text-align:{"right" if c in right else "left"};'
    head = f'<th style="{th}width:32px"></th>' + "".join(f'<th style="{th}{align(c)}">{c}</th>' for c in df.columns)
    body = "".join(
        f'<tr style="border:none"><td style="{cell}color:{DOWN}">{i}</td>'
        + "".join(f'<td style="{cell}{align(c)}{"font-weight:600" if j == 0 else ""}">{v}</td>'
                  for j, (c, v) in enumerate(row.items()))
        + "</tr>"
        for i, (_, row) in enumerate(df.iterrows(), 1))
    return (f'<table style="width:100%;border:none;border-collapse:collapse;font-size:.9rem">'
            f'<thead><tr style="border:none">{head}</tr></thead><tbody>{body}</tbody></table>')


def show(df, right=()):
    st.markdown(list_html(df, right), unsafe_allow_html=True)


STAT_NUM = ["표본", "평균", "중앙값", "하락 비율", "p값"]


def stats_view(view, *keep):
    """유형별·그룹별 결과 표 문자열로"""
    return pd.DataFrame({
        **{k: view[k] for k in keep},
        "표본": view["표본"].map("{:,}".format),
        "평균": view["평균"].map(pct),
        "중앙값": view["중앙값"].map(pct),
        "하락 비율": view["하락 비율"].map("{:.0f}%".format),
        "p값": view["p값"].map("{:.3f}".format),
        "판단": view["판단"],
    })


def price_slot():
    # 장중(평일 09:00~15:35)엔 10초마다 새 값, 그 밖엔 하루 한 번
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if now.weekday() < 5 and "0900" <= f"{now:%H%M}" <= "1535":
        return int(time.time() // 10)
    return f"{now:%Y%m%d}"


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
        used = int(view["표본"].sum())
        dropped = sum(sum(ast.literal_eval(d).values()) for d in stats["dropped"])
        st.markdown(f'<div style="opacity:.6;font-size:.9rem">공시 {used:,}건 · {h}거래일 초과수익률</div>',
                    unsafe_allow_html=True)

        # 가로 막대: 유형 이름이 세로로 눕지 않게, 평균 순서대로
        long = view.melt(id_vars="유형", value_vars=["평균", "중앙값"], var_name="지표", value_name="초과수익률(%)")
        st.altair_chart(alt.Chart(long).mark_bar().encode(
            y=alt.Y("유형:N", sort=list(view["유형"]), title=None),
            yOffset="지표:N",
            x=alt.X("초과수익률(%):Q"),
            color=alt.Color("지표:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=["유형", "지표", alt.Tooltip("초과수익률(%):Q", format="+.2f")],
        ), width="stretch")
        table = stats_view(view.sort_values("평균", ascending=False), "유형")
        table["표본"] = [f'{n:,}<span style="opacity:.5;margin-left:6px">{n / used:.0%}</span>'
                       for n in view.sort_values("평균", ascending=False)["표본"]]
        show(table, right=STAT_NUM)
        st.caption(f"공시 다음 날 시가에 샀을 때 같은 기간 지수보다 더 오른 만큼 · 거래정지 등 {dropped:,}건 제외")

with tab4:
    bb = load_stats("buyback_stats.csv")
    if bb is None:
        st.info("data/buyback_stats.csv가 없음 → python3 buyback.py 먼저 실행")
    else:
        stats = load_stats()
        known, unknown = int(bb["n"].sum()), ""
        if stats is not None and (stats["category"] == "자사주취득").any():
            n_all = int(stats.loc[stats["category"] == "자사주취득", "n_3d"].iloc[0])
            unknown = f" · 매입 규모 모르는 {n_all - known:,}건 제외"
        st.markdown(f'<div style="opacity:.6;font-size:.9rem">자사주취득 공시 {known:,}건 · 3거래일 초과수익률</div>',
                    unsafe_allow_html=True)
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
        # 축에 매입 비율 구간까지: "상위 2.34%~"
        axis = {g: f"{g} {a:.1%}~" if g == "상위" else f"{g} ~{b:.1%}" if g == "하위" else f"{g} {a:.1%}~{b:.1%}"
                for g, a, b in zip(bb["group"], bb["ratio_min"], bb["ratio_max"])}
        long = view.assign(그룹=view["그룹"].map(axis)).melt(id_vars="그룹", value_vars=["평균", "중앙값"], var_name="지표", value_name="초과수익률(%)")
        st.altair_chart(alt.Chart(long).mark_bar().encode(
            x=alt.X("그룹:N", sort=list(axis.values()), title=None, axis=alt.Axis(labelAngle=0)),
            xOffset="지표:N",
            y=alt.Y("초과수익률(%):Q"),
            color=alt.Color("지표:N", legend=alt.Legend(orient="top", title=None)),
            tooltip=["그룹", "지표", alt.Tooltip("초과수익률(%):Q", format="+.2f")],
        ), width="stretch")
        show(stats_view(view, "그룹", "매입 비율"), right=STAT_NUM + ["매입 비율"])
        st.caption(f"매입 비율 = 사기로 한 주식 수 ÷ 지금 상장주식수{unknown}")

with tab5:
    st.markdown(f"**대상** 전 거래일 {trader.CATEGORY} 공시 · 매입 비율 {trader.MIN_RATIO:.0%} 이상, 큰 순  \n"
                f"**매수** 09:00 시초가 · 종목당 {trader.BUDGET // 10_000:,}만원 · 최대 {trader.MAX_HOLD}종목  \n"
                f"**매도** 매수일 포함 {trader.HOLD_DAYS}거래일째 종가")

    @st.fragment(run_every="10s")
    def trade_board():
        trades = load_trades()
        held = trades[trades["sell_date"].isna()] if len(trades) else trades
        try:
            now = load_now(tuple(held["stock_code"]), price_slot()) if len(held) else {}
        except Exception:  # 네이버가 막히면 현재가만 비우고 표는 그대로
            now = {}
        ready = len(held) and held["buy_price"].notna().all() and all(c in now for c in held["stock_code"])
        if ready:
            cost = int((held["buy_price"] * held["qty"]).sum())
            value = sum(now[c] * q for c, q in zip(held["stock_code"], held["qty"]))
            pnl = value - cost
            try:
                cash = load_cash()
            except Exception:  # 못 읽으면 총액만 숨김
                cash = None
            total = f"총 {cash + value:,}원" if cash is not None else ""
            st.markdown(f'<div style="opacity:.6">내 투자</div>'
                        f'<div style="font-size:2rem;font-weight:700">{value:,}원'
                        f'<span style="font-size:1rem;font-weight:400;opacity:.5;margin-left:10px">{total}</span></div>'
                        f'<div style="color:{color_of(pnl)}">{pnl:+,}원 ({abs(pnl / cost):.1%})</div>'
                        f'<div style="opacity:.5;font-size:.85rem;margin:4px 0 20px">매수금액 {cost:,}원</div>',
                        unsafe_allow_html=True)
            value_each = held["stock_code"].map(now) * held["qty"]
            cost_each = held["buy_price"] * held["qty"]
            ret = (value_each / cost_each - 1) * 100
            order = ret.sort_values(ascending=False).index  # 수익률 높은 순
            show(pd.DataFrame({
                "종목": held["corp_name"],
                "수량": held["qty"].map("{:,}주".format),
                "매수 평균가": held["buy_price"].map("{:,.0f}원".format),
                "현재가": held["stock_code"].map(now).map("{:,}원".format),
                "평가금액": value_each.map("{:,.0f}원".format),
                "수익률": [f'{pct(r)}<div style="color:{color_of(r)};opacity:.7;font-size:.8rem">{v - c:+,.0f}원</div>'
                        for r, v, c in zip(ret, value_each, cost_each)],
            }).loc[order], right=["수량", "매수 평균가", "현재가", "평가금액", "수익률"])
        past = trades[trades["sell_date"].notna()] if len(trades) else trades
        if trades.empty:
            st.info("아직 거래 없음")
        elif len(past):
            done = past.dropna(subset=["sell_price"])
            ret = done["sell_price"] / done["buy_price"] - 1
            summary = (f'<span style="opacity:.5;font-size:.85rem;margin-left:8px">실현 수익률 '
                       f'{pct(ret.mean() * 100)} · {len(done)}건</span>' if len(done) else "")
            table = list_html(pd.DataFrame({
                "종목": past["corp_name"],
                "매수일": pd.to_datetime(past["buy_date"]).dt.strftime("%m/%d"),
                "상태": ["청산" if pd.notna(p) else "매도 주문 (정산 대기)" for p in past["sell_price"]],
                "수량": past["sell_qty"].fillna(past["qty"]).astype(int).map("{:,}주".format),
                "매수 평균가": past["buy_price"].map(lambda v: f"{v:,.0f}원" if pd.notna(v) else "-"),
                "매도 평균가": past["sell_price"].map(lambda v: f"{v:,.0f}원" if pd.notna(v) else "-"),
                "수익률": [pct(r) if pd.notna(r) else "-" for r in (past["sell_price"] / past["buy_price"] - 1) * 100],
            }), right=["수량", "매수 평균가", "매도 평균가", "수익률"])
            # 토스처럼 박스 없이 글자만 눌러서 펼침
            st.markdown(f'<details style="margin-top:28px"><summary style="cursor:pointer;list-style:none;font-weight:600">'
                        f'거래 내역 <span style="opacity:.4">›</span>{summary}</summary>{table}</details>',
                        unsafe_allow_html=True)

    trade_board()

with tab2:
    days = st.select_slider("기간", [30, 90, 180, 365], value=90, format_func=lambda d: f"최근 {d}일")
    recent = load_recent(days)
    if recent.empty:
        st.info("데이터 없음")
    else:
        recent["날짜"] = pd.to_datetime(recent["rcept_dt"])
        daily = recent.pivot_table(index="날짜", columns="category", aggfunc="size", fill_value=0)
        st.bar_chart(daily)
        counts = recent["category"].value_counts()
        show(pd.DataFrame({"유형": counts.index, "건수": counts.map("{:,}".format).values}), right=["건수"])

with tab3:
    name = st.text_input("회사 이름")
    if name:
        found = search(name)
        st.write(f"{len(found)}건")
        if len(found):
            show(pd.DataFrame({
                "회사": found["corp_name"].map(html.escape),
                "공시일": pd.to_datetime(found["rcept_dt"]).dt.strftime("%Y-%m-%d"),
                "유형": found["category"].fillna("-"),
                "공시": found["report_nm"].map(html.escape),
            }))
