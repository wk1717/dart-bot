-- Supabase SQL Editor에서 한 번 실행
create table if not exists disclosures (
  rcept_no text primary key, rcept_dt text, corp_code text, corp_name text,
  stock_code text, corp_cls text, report_nm text, category text, saved_at text
);
create index if not exists disclosures_rcept_dt on disclosures (rcept_dt);
create table if not exists runs (
  id bigint generated always as identity primary key,
  started_at text, ok int, saved int, error text
);
create index if not exists runs_started_at on runs (started_at);
create table if not exists alerts (key text primary key, sent_at double precision);
-- 모의투자 매매 기록 (trader.py)
create table if not exists trades (
  rcept_no text primary key, stock_code text, corp_name text, qty int,
  buy_date text, buy_odno text, sell_date text, sell_odno text,
  buy_price double precision, sell_price double precision, sell_qty int
);

-- anon 키로는 못 읽게. 봇은 secret 키라 상관없음
alter table disclosures enable row level security;
alter table runs enable row level security;
alter table alerts enable row level security;
alter table trades enable row level security;
