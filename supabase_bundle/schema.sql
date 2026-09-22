begin;

create table if not exists public.data_sources (
  id smallint primary key,
  name text not null unique
);

create table if not exists public.companies (
  id smallint primary key,
  ticker text not null unique check (ticker ~ '^[A-Z0-9]{3}$'),
  company_name text,
  exchange text not null check (exchange in ('HOSE','HNX')),
  sector text,
  industry text,
  listing_date date,
  shares_outstanding bigint check (shares_outstanding is null or shares_outstanding >= 0),
  market_cap double precision,
  trading_status text
);

create table if not exists public.price_daily (
  company_id smallint not null references public.companies(id),
  date date not null,
  open double precision not null,
  high double precision not null,
  low double precision not null,
  close double precision not null,
  adjusted_close double precision not null,
  volume bigint,
  source_id smallint references public.data_sources(id),
  is_valid boolean not null default true,
  fetched_at timestamptz not null,
  data_version integer not null default 1 check (data_version > 0),
  row_checksum bytea not null check (octet_length(row_checksum) = 32),
  dnse_checksum bytea check (dnse_checksum is null or octet_length(dnse_checksum) = 32),
  primary key (company_id, date),
  check (volume is null or volume >= 0)
);

create table if not exists public.benchmark_daily (
  date date primary key,
  open double precision not null,
  high double precision not null,
  low double precision not null,
  close double precision not null,
  adjusted_close double precision not null,
  volume bigint,
  source_id smallint references public.data_sources(id),
  is_valid boolean not null default true,
  fetched_at timestamptz not null,
  data_version integer not null default 1 check (data_version > 0),
  row_checksum bytea not null check (octet_length(row_checksum) = 32),
  dnse_checksum bytea check (dnse_checksum is null or octet_length(dnse_checksum) = 32)
);

create table if not exists public.sync_runs (
  sync_run_id text primary key,
  started_at timestamptz not null,
  finished_at timestamptz,
  status text not null,
  requested_tickers integer not null default 0,
  updated_tickers integer not null default 0,
  failed_tickers integer not null default 0,
  history_audit_tickers integer not null default 0,
  action_audit_tickers integer not null default 0,
  full_refetch_tickers integer not null default 0,
  revision_rows integer not null default 0
);

create table if not exists public.price_revisions (
  sync_run_id text not null references public.sync_runs(sync_run_id),
  company_id smallint not null references public.companies(id),
  date date not null,
  detected_at timestamptz not null,
  reason text not null,
  change_type text not null check (change_type in ('updated','deleted')),
  old_open double precision, old_high double precision, old_low double precision, old_close double precision,
  old_volume bigint, old_source_id smallint references public.data_sources(id), old_checksum bytea, old_dnse_checksum bytea,
  new_open double precision, new_high double precision, new_low double precision, new_close double precision,
  new_volume bigint, new_source_id smallint references public.data_sources(id), new_checksum bytea, new_dnse_checksum bytea,
  primary key (sync_run_id, company_id, date)
);

create table if not exists public.bundle_imports (
  manifest_sha256 text primary key,
  source_sha256 text not null,
  imported_at timestamptz not null default now()
);

create table if not exists public.financial_items (
  id smallint primary key,
  code text not null unique,
  name text,
  unit text
);

create table if not exists public.financial_values (
  company_id smallint not null references public.companies(id),
  period_kind smallint not null check (period_kind in (1,2)),
  period_end date not null,
  fiscal_year smallint not null,
  fiscal_quarter smallint check (fiscal_quarter is null or fiscal_quarter between 1 and 4),
  statement_id smallint not null check (statement_id between 1 and 3),
  item_id smallint not null references public.financial_items(id),
  value double precision not null,
  source_id smallint references public.data_sources(id),
  primary key (company_id, period_kind, period_end, statement_id, item_id)
);

create table if not exists public.action_types (
  id smallint primary key,
  name text not null unique
);

create table if not exists public.corporate_actions (
  id integer primary key,
  company_id smallint not null references public.companies(id),
  ex_date date not null,
  action_type_id smallint not null references public.action_types(id),
  cash_dividend double precision,
  stock_ratio double precision,
  split_ratio double precision,
  rights_ratio double precision,
  rights_price double precision,
  source_id smallint references public.data_sources(id)
);

create table if not exists public.financial_snapshot (
  company_id smallint primary key references public.companies(id),
  as_of_utc timestamptz,
  company_type text,
  status text,
  market_share double precision,
  total_assets double precision,
  eps_ttm double precision,
  pe double precision,
  ps double precision,
  pb double precision,
  beta double precision,
  profit_growth_qoq double precision,
  roe_ttm double precision,
  roa_ttm double precision,
  gross_margin_ttm double precision,
  debt_equity_ratio double precision,
  inventory_growth_qoq double precision,
  free_float_ratio double precision,
  dividend_yield double precision,
  book_value_per_share double precision,
  revenue_ttm double precision,
  net_income_ttm double precision,
  market_cap double precision
);

-- Private application state. These tables are accessed only by the trusted
-- Python backend after Telegram initData verification; they are never exposed
-- to Supabase anon/authenticated Data API roles.
create table if not exists public.telegram_users (
  telegram_user_id bigint primary key check (telegram_user_id > 0),
  username text,
  first_name text not null default '',
  last_name text not null default '',
  created_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);

create table if not exists public.paper_accounts (
  telegram_user_id bigint primary key references public.telegram_users(telegram_user_id) on delete cascade,
  initial_cash double precision not null check (initial_cash > 0),
  cash double precision not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.paper_trades (
  id bigint generated always as identity primary key,
  telegram_user_id bigint not null references public.paper_accounts(telegram_user_id) on delete cascade,
  ticker text not null check (ticker ~ '^[A-Z0-9]{1,12}$'),
  side text not null check (side in ('BUY','SELL')),
  quantity integer not null check (quantity > 0 and quantity <= 10000000),
  price double precision not null check (price > 0),
  gross_amount double precision not null check (gross_amount > 0),
  fee double precision not null check (fee >= 0),
  market_date date not null,
  signal_action text,
  price_source text not null,
  trade_time text,
  executed_at timestamptz not null default now()
);
create index if not exists paper_trades_user_id_idx
  on public.paper_trades (telegram_user_id, id);

create table if not exists public.rate_limit_buckets (
  subject_key text not null,
  scope text not null,
  window_started_at timestamptz not null,
  request_count integer not null check (request_count >= 0),
  primary key (subject_key, scope)
);

alter table public.telegram_users enable row level security;
alter table public.paper_accounts enable row level security;
alter table public.paper_trades enable row level security;
alter table public.rate_limit_buckets enable row level security;
revoke all on public.telegram_users, public.paper_accounts,
  public.paper_trades, public.rate_limit_buckets from anon, authenticated;
revoke all on sequence public.paper_trades_id_seq from anon, authenticated;

commit;
