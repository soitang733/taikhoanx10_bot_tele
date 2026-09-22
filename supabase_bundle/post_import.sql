create index if not exists price_daily_valid_date_idx
  on public.price_daily (date, company_id) where is_valid;
create index if not exists financial_values_item_period_idx
  on public.financial_values (item_id, period_end desc, company_id);
create index if not exists corporate_actions_company_date_idx
  on public.corporate_actions (company_id, ex_date desc);
create index if not exists price_revisions_company_date_idx
  on public.price_revisions (company_id, date, detected_at desc);

create or replace view public.price_daily_api
with (security_invoker = true) as
select c.ticker, p.date, p.open, p.high, p.low, p.close, p.adjusted_close,
       p.volume, p.close * p.volume as estimated_trading_value,
       s.name as source, p.is_valid, p.fetched_at, p.data_version, encode(p.row_checksum, 'hex') as row_checksum,
       encode(p.dnse_checksum, 'hex') as dnse_checksum
from public.price_daily p
join public.companies c on c.id = p.company_id
left join public.data_sources s on s.id = p.source_id;

create or replace view public.financial_values_api
with (security_invoker = true) as
select c.ticker,
       case f.period_kind when 1 then 'annual' else 'quarterly' end as period_type,
       f.period_end, f.fiscal_year, f.fiscal_quarter,
       case f.statement_id when 1 then 'balance_sheet' when 2 then 'income_statement' else 'cash_flow' end as statement,
       i.code as item_code, i.name as item_name, f.value, i.unit, s.name as source
from public.financial_values f
join public.companies c on c.id = f.company_id
join public.financial_items i on i.id = f.item_id
left join public.data_sources s on s.id = f.source_id;

create or replace view public.corporate_actions_api
with (security_invoker = true) as
select a.id, c.ticker, a.ex_date, t.name as action_type,
       a.cash_dividend, a.stock_ratio, a.split_ratio, a.rights_ratio, a.rights_price,
       s.name as source
from public.corporate_actions a
join public.companies c on c.id = a.company_id
join public.action_types t on t.id = a.action_type_id
left join public.data_sources s on s.id = a.source_id;

alter table public.data_sources enable row level security;
alter table public.companies enable row level security;
alter table public.price_daily enable row level security;
alter table public.benchmark_daily enable row level security;
alter table public.financial_items enable row level security;
alter table public.financial_values enable row level security;
alter table public.action_types enable row level security;
alter table public.corporate_actions enable row level security;
alter table public.financial_snapshot enable row level security;
alter table public.price_revisions enable row level security;
alter table public.sync_runs enable row level security;
alter table public.bundle_imports enable row level security;

do $$
declare table_name text;
begin
  foreach table_name in array array[
    'data_sources','companies','price_daily','benchmark_daily','financial_items',
    'financial_values','action_types','corporate_actions','financial_snapshot'
  ] loop
    if not exists (
      select 1 from pg_policies where schemaname='public' and tablename=table_name and policyname=table_name || '_public_read'
    ) then
      execute format('create policy %I on public.%I for select to anon, authenticated using (true)', table_name || '_public_read', table_name);
    end if;
  end loop;
end $$;

revoke all on public.data_sources, public.companies, public.price_daily,
  public.benchmark_daily, public.financial_items, public.financial_values,
  public.action_types, public.corporate_actions, public.financial_snapshot,
  public.price_revisions, public.sync_runs, public.bundle_imports
  from anon, authenticated;
grant usage on schema public to anon, authenticated;
grant select on public.data_sources, public.companies, public.price_daily,
  public.benchmark_daily, public.financial_items, public.financial_values,
  public.action_types, public.corporate_actions, public.financial_snapshot,
  public.price_daily_api, public.financial_values_api, public.corporate_actions_api
  to anon, authenticated;

analyze public.companies;
analyze public.price_daily;
analyze public.benchmark_daily;
analyze public.financial_values;
analyze public.corporate_actions;
analyze public.financial_snapshot;
analyze public.price_revisions;
analyze public.sync_runs;
