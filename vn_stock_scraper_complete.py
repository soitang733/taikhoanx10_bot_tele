"""Vietnam equity data-layer scraper for FA and TA research.

The scraper deliberately stores raw observations and provenance.  It does not
calculate an FA score or call a macro/news API.  Financial rows without a real
publication date retain a null ``published_date`` so a backtest cannot silently
assume that the statement was known at period end.

Examples:
    python vn_stock_scraper_complete.py --symbols FPT,VCB,SSI
    python vn_stock_scraper_complete.py --all --years 10 --ta-years 8 --resume
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vn_scraper")


# =============================================================================
# 1. CONFIG, SCHEMAS, AND HELPERS
# =============================================================================
DNSE_API_KEY = os.getenv("DNSE_API_KEY", "").strip()
DNSE_API_SECRET = os.getenv("DNSE_API_SECRET", "").strip()
COMMUNITY_MIN_INTERVAL_SECONDS = float(os.getenv("COMMUNITY_MIN_INTERVAL_SECONDS", "3.2"))
SOURCE_ADJUSTED_PRICE_SOURCES = frozenset({"dnse", "vnstock vci", "vnstock kbs"})
PRICE_FALLBACK_DISCREPANCY = 0.20
PRICE_SUSPICIOUS_RETURN = 0.50
_community_rate_lock = Lock()
_community_last_request = 0.0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "User-Agent": USER_AGENT,
}

FA_COLUMNS = [
    "ticker",
    "statement",
    "report_period",
    "period_type",
    "published_date",
    "item_code",
    "item_name",
    "value",
    "source",
]
TA_COLUMNS = [
    "ticker",
    "date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_open",
    "adjusted_high",
    "adjusted_low",
    "adjusted_close",
    "volume",
    "trading_value",
    "trading_value_is_estimated",
    "adjustment_status",
    "adjustment_source",
    "source",
    "fetched_at",
    "data_version",
    "row_checksum",
    "dnse_checksum",
]
CA_COLUMNS = [
    "ticker",
    "ex_date",
    "action_type",
    "cash_dividend",
    "stock_ratio",
    "split_ratio",
    "rights_ratio",
    "rights_price",
    "source",
]
METADATA_FIELDS = [
    "ticker",
    "company_name",
    "exchange",
    "sector",
    "industry",
    "listing_date",
    "shares_outstanding",
    "market_cap",
    "trading_status",
]
FA_CRITICAL = [
    "revenue",
    "cogs",
    "gross_profit",
    "ebit",
    "interest_expense",
    "profit_before_tax",
    "net_income",
    "eps",
    "total_assets",
    "equity",
    "cash_and_cash_equivalents",
    "current_assets",
    "current_liabilities",
    "short_term_borrowings",
    "long_term_borrowings",
    "total_liabilities",
    "operating_cash_flow",
    "capex",
    "depreciation",
    "amortization",
]

def diagnostic(module: str, source: str, status: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"module": module, "source": source, "status": status}
    result.update({k: json_safe(v) for k, v in details.items()})
    return result


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"nan", "none", "nat"}
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def scalar(value: Any) -> Any:
    if isinstance(value, pd.Series):
        for candidate in value.tolist():
            if not is_missing(candidate):
                return candidate
        return None
    return None if is_missing(value) else value


def safe_float(value: Any) -> float | None:
    value = scalar(value)
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", ""))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def ascii_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def first_present(mapping: dict[str, Any] | pd.Series, *names: str) -> Any:
    for name in names:
        if name in mapping:
            value = scalar(mapping.get(name))
            if value is not None:
                return value
    return None


def empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def ensure_columns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        if column not in result.columns:
            result[column] = pd.NA
    return result[columns]


def normalize_report_period(value: Any, period_type: str) -> str:
    text = str(value or "").strip().upper()
    if period_type == "annual":
        match = re.search(r"(?:19|20)\d{2}", text)
        return match.group(0) if match else text
    for pattern in (
        r"Q([1-4])\s*[/\-]\s*((?:19|20)\d{2})",
        r"((?:19|20)\d{2})\s*[/\-]\s*Q([1-4])",
    ):
        match = re.search(pattern, text)
        if match:
            if text.startswith("Q"):
                quarter, year = match.group(1), match.group(2)
            else:
                year, quarter = match.group(1), match.group(2)
            return f"{year}-Q{quarter}"
    return text


def get_dnse_auth_headers() -> dict[str, str]:
    headers = dict(HTTP_HEADERS)
    if DNSE_API_KEY:
        headers["Authorization"] = f"Bearer {DNSE_API_KEY}"
    if DNSE_API_SECRET:
        headers["X-Api-Secret"] = DNSE_API_SECRET
    return headers


def http_get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict[str, Any] | list[Any] | None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers or HTTP_HEADERS, params=params, timeout=timeout)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    if last_error:
        raise last_error
    return None


def wait_for_community_slot() -> None:
    """Keep vnstock Community calls below the guest 20 requests/minute limit."""
    global _community_last_request
    with _community_rate_lock:
        elapsed = time.monotonic() - _community_last_request
        delay = COMMUNITY_MIN_INTERVAL_SECONDS - elapsed
        if delay > 0:
            time.sleep(delay)
        _community_last_request = time.monotonic()


# =============================================================================
# 2. TICKER LISTING AND METADATA
# =============================================================================
@lru_cache(maxsize=1)
def dnse_ticker_rows() -> tuple[dict[str, Any], ...]:
    raw = http_get_json(
        "https://api.dnse.com.vn/market-api/tickers",
        params={"_start": 0, "_end": 3000},
    )
    if isinstance(raw, dict) and isinstance(raw.get("data"), list):
        return tuple(item for item in raw["data"] if isinstance(item, dict))
    return ()


def get_all_symbols(exchange: str | None = None) -> list[str]:
    exchange = (exchange or "ALL").upper()
    symbols: set[str] = set()
    try:
        for item in dnse_ticker_rows():
            symbol = str(item.get("symbol", "")).strip().upper()
            floor = str(item.get("floor", "")).strip().upper()
            mapped = "HOSE" if floor in {"HSX", "HOSE"} else floor
            item_type = str(item.get("type", "STOCK")).upper()
            # Vietnamese stock symbols can contain digits (for example C4G),
            # so isalpha() would silently omit valid listed equities.
            if len(symbol) == 3 and symbol.isalnum() and mapped in {"HOSE", "HNX"} and item_type == "STOCK":
                if exchange == "ALL" or mapped == exchange:
                    symbols.add(symbol)
    except Exception as exc:
        logger.warning("DNSE ticker listing failed: %s", exc)

    if not symbols:
        try:
            import vnfinancialdata as vnf

            exchanges = ["HSX", "HNX"] if exchange == "ALL" else ["HSX" if exchange == "HOSE" else exchange]
            for exc in exchanges:
                frame = vnf.load(exchange=exc, statement="income_statement")
                if "ticker" in frame:
                    symbols.update(frame["ticker"].dropna().astype(str).str.upper())
        except Exception as exc:
            logger.warning("vnfinancialdata ticker fallback failed: %s", exc)
    return sorted(symbols)


def fetch_metadata(symbol: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    symbol = symbol.strip().upper()
    metadata: dict[str, Any] = {field: None for field in METADATA_FIELDS}
    metadata["ticker"] = symbol
    diagnostics: list[dict[str, Any]] = []

    try:
        match = next(
            (item for item in dnse_ticker_rows() if str(item.get("symbol", "")).upper() == symbol),
            None,
        )
        if match:
            floor = first_present(match, "floor", "exchange")
            metadata.update(
                company_name=first_present(match, "companyName", "name", "organName"),
                exchange="HOSE" if str(floor).upper() in {"HSX", "HOSE"} else floor,
                sector=first_present(match, "sector", "gicsSector"),
                industry=first_present(match, "industry", "gicsIndustry"),
                trading_status=first_present(match, "status", "tradingStatus"),
            )
            diagnostics.append(diagnostic("METADATA", "DNSE", "OK"))
        else:
            diagnostics.append(diagnostic("METADATA", "DNSE", "EMPTY", detail="Ticker not found"))
    except Exception as exc:
        diagnostics.append(diagnostic("METADATA", "DNSE", "FAIL", detail=str(exc)))

    if DNSE_API_KEY and DNSE_API_SECRET:
        try:
            raw_index = http_get_json(
                "https://api-bo.dnse.com.vn/senses-api/v5/financial-info/financial-index",
                headers=get_dnse_auth_headers(),
                params={"symbol": symbol},
            )
            payload = raw_index if isinstance(raw_index, dict) else {}
            indexes = payload.get("indexes", {})
            if not indexes and isinstance(payload.get("data"), dict):
                indexes = payload["data"].get("indexes", payload["data"])
            capitalization = indexes.get("capitalization", {}) if isinstance(indexes, dict) else {}
            market_cap = capitalization.get("value") if isinstance(capitalization, dict) else capitalization
            if is_missing(metadata.get("market_cap")) and not is_missing(market_cap):
                metadata["market_cap"] = json_safe(market_cap)
            diagnostics.append(diagnostic("METADATA", "DNSE financial-index", "OK"))
        except Exception as exc:
            diagnostics.append(diagnostic("METADATA", "DNSE financial-index", "FAIL", detail=str(exc)))
    else:
        diagnostics.append(
            diagnostic("METADATA", "DNSE financial-index", "MISSING_CREDENTIALS")
        )

    missing_after_dnse = [field for field in METADATA_FIELDS[1:] if is_missing(metadata.get(field))]
    dnse_listing_ok = any(
        item.get("source") == "DNSE" and item.get("status") == "OK"
        for item in diagnostics
    )
    if dnse_listing_ok:
        diagnostics.append(
            diagnostic(
                "METADATA",
                "fallback",
                "NOT_USED",
                reason="DNSE returned the ticker; missing optional fields remain null",
                missing_fields=missing_after_dnse,
            )
        )
        if missing_after_dnse:
            diagnostics.append(diagnostic("METADATA", "combined", "PARTIAL", missing_fields=missing_after_dnse))
        return {field: json_safe(metadata[field]) for field in METADATA_FIELDS}, diagnostics

    diagnostics.append(
        diagnostic(
            "METADATA",
            "fallback",
            "TRIGGERED",
            reason="DNSE failed or returned no ticker",
            missing_fields=missing_after_dnse,
        )
    )
    try:
        from vnstock import Reference

        wait_for_community_slot()
        reference = Reference()
        result = reference.search.info(symbol)
        if isinstance(result, pd.DataFrame) and not result.empty:
            row = result.iloc[0]
            metadata["company_name"] = metadata["company_name"] or first_present(
                row, "friendly_name", "local_name", "organ_name", "name"
            )
            metadata["exchange"] = metadata["exchange"] or first_present(row, "exchange_name", "exchange")
            diagnostics.append(diagnostic("METADATA", "vnstock Reference", "OK"))
        else:
            diagnostics.append(diagnostic("METADATA", "vnstock Reference", "EMPTY"))
    except Exception as exc:
        diagnostics.append(diagnostic("METADATA", "vnstock Reference", "FAIL", detail=str(exc)))

    for source in ("KBS", "VCI"):
        if not any(is_missing(metadata.get(field)) for field in METADATA_FIELDS[1:]):
            break
        try:
            from vnstock import Company

            wait_for_community_slot()
            overview = Company(source=source, symbol=symbol).overview()
            if not isinstance(overview, pd.DataFrame) or overview.empty:
                diagnostics.append(diagnostic("METADATA", f"vnstock {source}", "EMPTY"))
                continue
            row = overview.iloc[0]
            candidates = {
                "company_name": first_present(row, "organ_name", "company_name", "name"),
                "exchange": first_present(row, "exchange", "listing"),
                "sector": first_present(row, "sector", "icb_name2"),
                "industry": first_present(row, "industry", "business_model", "icb_name4"),
                "listing_date": first_present(row, "listing_date"),
                "shares_outstanding": first_present(row, "outstanding_shares", "issue_share", "listed_volume"),
                "market_cap": first_present(row, "market_cap"),
                "trading_status": first_present(row, "status", "trading_status"),
            }
            for field, value in candidates.items():
                if is_missing(metadata.get(field)) and not is_missing(value):
                    metadata[field] = json_safe(value)
            diagnostics.append(diagnostic("METADATA", f"vnstock {source}", "OK"))
        except Exception as exc:
            diagnostics.append(diagnostic("METADATA", f"vnstock {source}", "FAIL", detail=str(exc)))

    missing = [field for field in METADATA_FIELDS if field != "ticker" and is_missing(metadata[field])]
    if missing:
        diagnostics.append(
            diagnostic("METADATA", "combined", "PARTIAL", missing_fields=missing)
        )
    return {field: json_safe(metadata[field]) for field in METADATA_FIELDS}, diagnostics


# =============================================================================
# 3. FINANCIAL STATEMENTS
# =============================================================================
VNF_ITEM_MAP = {
    "is_doanh_so_thuan": "revenue",
    "is_gia_von_hang_ban": "cogs",
    "is_lai_gop": "gross_profit",
    "is_ebit": "ebit",
    "is_trong_do_chi_phi_lai_vay": "interest_expense",
    "is_lai_lo_rong_truoc_thue": "profit_before_tax",
    "is_loi_nhuan_cua_co_dong_cua_cong_ty_me": "net_income",
    "is_lai_co_ban_tren_co_phieu": "eps",
    "bs_tong_tai_san": "total_assets",
    "bs_von_chu_so_huu_4d280b22": "equity",
    "bs_tien_va_tuong_duong_tien": "cash_and_cash_equivalents",
    "bs_tai_san_ngan_han": "current_assets",
    "bs_no_ngan_han": "current_liabilities",
    "bs_vay_ngan_han": "short_term_borrowings",
    "bs_vay_dai_han": "long_term_borrowings",
    "bs_no_phai_tra": "total_liabilities",
    "cf_luu_chuyen_tien_thuan_tu_cac_hoat_dong_san_xuat_kinh_doanh": "operating_cash_flow",
    "cf_tien_mua_tai_san_co_dinh_va_cac_tai_san_dai_han_khac": "capex",
    "cf_khau_hao_tscd": "depreciation",
    "cf_phan_bo_loi_the_thuong_mai": "amortization",
}

# vnstock's Community financial tables already expose stable English item_id
# values.  Use exact identifiers only: broad substring matching can turn
# "other current assets" into total current assets, or receivables/loans into
# interest-bearing debt.
VNSTOCK_ITEM_MAP = {
    "revenue": "revenue",
    "net_revenue": "revenue",
    "cost_of_goods_sold": "cogs",
    "gross_profit": "gross_profit",
    "operating_profit": "ebit",
    "of_which_interest_expense": "interest_expense",
    "interest_expense": "interest_expense",
    "profit_before_tax": "profit_before_tax",
    "profit_after_tax_for_shareholders_of_parent_company": "net_income",
    "net_income": "net_income",
    "earnings_per_share": "eps",
    "basic_earnings_per_share": "eps",
    "total_assets": "total_assets",
    "owners_equity": "equity",
    "total_owners_equity": "equity",
    "cash_and_cash_equivalents": "cash_and_cash_equivalents",
    "current_assets": "current_assets",
    "current_liabilities": "current_liabilities",
    "short_term_borrowings_and_financial_leases": "short_term_borrowings",
    "long_term_borrowings_and_financial_leases": "long_term_borrowings",
    "liabilities": "total_liabilities",
    "total_liabilities": "total_liabilities",
    "operating_cash_flow": "operating_cash_flow",
    "payment_for_fixed_assets_constructions_and_other_long_term_assets": "capex",
    "depreciation_of_fixed_assets_and_investment_properties": "depreciation",
    "amortization": "amortization",
}

DNSE_DETAIL_CODES = {
    "REVENUE": ("revenue", "income_statement"),
    "NET_REVENUE": ("revenue", "income_statement"),
    "COST_OF_GOODS_SOLD": ("cogs", "income_statement"),
    "GROSS_PROFIT": ("gross_profit", "income_statement"),
    "EBIT": ("ebit", "income_statement"),
    "INTEREST_EXPENSE": ("interest_expense", "income_statement"),
    "PROFIT_BEFORE_TAX": ("profit_before_tax", "income_statement"),
    "PROFIT_AFTER_TAX_PARENT_COMPANY": ("net_income", "income_statement"),
    "EPS": ("eps", "income_statement"),
    "TOTAL_ASSETS": ("total_assets", "balance_sheet"),
    "OWNERS_EQUITY": ("equity", "balance_sheet"),
    "CASH_AND_CASH_EQUIVALENTS": ("cash_and_cash_equivalents", "balance_sheet"),
    "SHORT_TERM_ASSETS": ("current_assets", "balance_sheet"),
    "SHORT_TERM_LIABILITIES": ("current_liabilities", "balance_sheet"),
    "SHORT_TERM_BORROWINGS": ("short_term_borrowings", "balance_sheet"),
    "LONG_TERM_BORROWINGS": ("long_term_borrowings", "balance_sheet"),
    "TOTAL_LIABILITIES": ("total_liabilities", "balance_sheet"),
    "ACCOUNTS_PAYABLE": ("accounts_payable", "balance_sheet"),
    "CASH_FLOW": ("operating_cash_flow", "cash_flow"),
    "CAPEX": ("capex", "cash_flow"),
    "DEPRECIATION": ("depreciation", "cash_flow"),
    "AMORTIZATION": ("amortization", "cash_flow"),
}


def canonical_financial_code(raw_code: Any, item_name: Any, statement: str) -> str:
    raw = str(raw_code or "").strip().lower()
    # Exact mapping preserves every raw line item and prevents similarly named
    # subtotals from being collapsed into one canonical variable.
    return VNF_ITEM_MAP.get(raw, raw or "unknown_item")


def canonical_vnstock_code(raw_code: Any) -> str:
    raw = str(raw_code or "").strip().lower().replace(" ", "_")
    return VNSTOCK_ITEM_MAP.get(raw, raw or "unknown_item")


def _parse_dnse_detail(raw: Any) -> tuple[list[str], list[float | None]]:
    if not isinstance(raw, dict):
        return [], []
    labels = [str(label) for label in raw.get("x", [])]
    series_list = raw.get("data", [])
    if not isinstance(series_list, list) or not series_list:
        return [], []
    count = len(labels) or max((len(item.get("y", [])) for item in series_list if isinstance(item, dict)), default=0)
    totals = [0.0] * count
    present = [False] * count
    candidates = series_list
    if "markupline" in str(raw.get("type", "")).lower():
        candidates = [item for item in series_list if isinstance(item, dict) and item.get("type") == "line"][:1]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for index, value in enumerate(item.get("y", [])[:count]):
            number = safe_float(value)
            if number is not None:
                totals[index] += number
                present[index] = True
    return labels, [totals[i] if present[i] else None for i in range(count)]


def fetch_dnse_financial(
    symbol: str,
    *,
    cycle_type: str,
    cycle_number: int,
    period_type: str,
    module: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    if not DNSE_API_KEY or not DNSE_API_SECRET:
        diagnostics.append(
            diagnostic(
                module,
                "DNSE",
                "MISSING_CREDENTIALS",
                detail="DNSE_API_KEY/DNSE_API_SECRET missing; fallback remains enabled",
            )
        )
        return empty_frame(FA_COLUMNS), diagnostics
    rows: list[dict[str, Any]] = []
    failed_codes: list[str] = []
    for code, (item_code, statement) in DNSE_DETAIL_CODES.items():
        try:
            raw = http_get_json(
                "https://api-bo.dnse.com.vn/senses-api/financial-report/details",
                headers=get_dnse_auth_headers(),
                params={
                    "symbol": symbol,
                    "code": code,
                    "cycleType": cycle_type,
                    "cycleNumber": cycle_number,
                },
            )
            labels, values = _parse_dnse_detail(raw)
            for label, value in zip(labels, values):
                if value is not None:
                    rows.append(
                        {
                            "ticker": symbol,
                            "statement": statement,
                            "report_period": normalize_report_period(label, period_type),
                            "period_type": period_type,
                            "published_date": pd.NA,
                            "item_code": item_code,
                            "item_name": code,
                            "value": value,
                            "source": "DNSE",
                        }
                    )
        except Exception as exc:
            failed_codes.append(f"{code}: {exc}")
    status = "OK" if rows else "FAIL"
    diagnostics.append(
        diagnostic(module, "DNSE", status, rows=len(rows), failed_codes=failed_codes[:8])
    )
    if rows:
        diagnostics.append(
            diagnostic(
                module,
                "DNSE",
                "WARNING",
                detail="published_date unavailable from endpoint; retained as null",
            )
        )
    return ensure_columns(pd.DataFrame(rows), FA_COLUMNS), diagnostics


def fetch_vnf_annual(symbol: str, years: int) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    try:
        import vnfinancialdata as vnf
    except Exception as exc:
        return empty_frame(FA_COLUMNS), [diagnostic("FA_ANNUAL", "vnfinancialdata", "FAIL", detail=str(exc))]

    end_year = datetime.now().year
    start_year = end_year - years - 1
    selected_exchange: str | None = None
    errors: list[str] = []
    for exchange in ("HSX", "HNX"):
        exchange_rows: list[dict[str, Any]] = []
        for statement in ("income_statement", "balance_sheet", "cash_flow"):
            try:
                frame = vnf.load(
                    exchange=exchange,
                    statement=statement,
                    ticker=symbol,
                    start_year=start_year,
                    end_year=end_year,
                )
                for _, row in frame.iterrows():
                    value = safe_float(row.get("value"))
                    if value is None:
                        continue
                    raw_code = row.get("item_code")
                    exchange_rows.append(
                        {
                            "ticker": symbol,
                            "statement": statement,
                            "report_period": str(row.get("year", "")),
                            "period_type": "annual",
                            "published_date": pd.NA,
                            "item_code": canonical_financial_code(raw_code, row.get("item_name"), statement),
                            "item_name": row.get("item_name") or raw_code,
                            "value": value,
                            "source": f"vnfinancialdata ({exchange})",
                        }
                    )
            except Exception as exc:
                errors.append(f"{exchange}/{statement}: {exc}")
        if exchange_rows:
            rows.extend(exchange_rows)
            selected_exchange = exchange
            break
    diagnostics.append(
        diagnostic(
            "FA_ANNUAL",
            "vnfinancialdata",
            "OK" if rows else "FAIL",
            rows=len(rows),
            exchange=selected_exchange,
            errors=errors[:6],
        )
    )
    if rows:
        diagnostics.append(
            diagnostic(
                "FA_ANNUAL",
                "vnfinancialdata",
                "WARNING",
                detail="Dataset has no publication date; published_date retained as null",
            )
        )
    result = ensure_columns(pd.DataFrame(rows), FA_COLUMNS)
    if not result.empty:
        numeric_periods = sorted(
            {str(period) for period in result["report_period"] if re.fullmatch(r"\d{4}", str(period))}
        )
        keep_periods = set(numeric_periods[-years:])
        result = result[result["report_period"].astype(str).isin(keep_periods)].reset_index(drop=True)
    return result, diagnostics


def fetch_vnstock_quarterly(symbol: str) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    for source in ("KBS", "VCI"):
        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            from vnstock import Finance

            wait_for_community_slot()
            finance = Finance(source=source, symbol=symbol, period="quarter", get_all=True)
            for statement, method_name in (
                ("income_statement", "income_statement"),
                ("balance_sheet", "balance_sheet"),
                ("cash_flow", "cash_flow"),
            ):
                try:
                    frame = getattr(finance, method_name)()
                    if not isinstance(frame, pd.DataFrame) or frame.empty:
                        continue
                    period_columns = [
                        column
                        for column in frame.columns
                        if re.match(r"^\d{4}[-/]?Q[1-4]$", str(column), re.IGNORECASE)
                    ]
                    for _, row in frame.iterrows():
                        raw_code = first_present(row, "item_id", "item_code")
                        item_name = first_present(row, "item", "item_en", "item_name") or raw_code
                        item_code = canonical_vnstock_code(raw_code)
                        for period in period_columns:
                            value = safe_float(row.get(period))
                            if value is not None:
                                rows.append(
                                    {
                                        "ticker": symbol,
                                        "statement": statement,
                                        "report_period": str(period).replace("/", "-"),
                                        "period_type": "quarterly",
                                        "published_date": pd.NA,
                                        "item_code": item_code,
                                        "item_name": item_name,
                                        "value": value,
                                        "source": f"vnstock {source}",
                                    }
                                )
                except Exception as exc:
                    errors.append(f"{method_name}: {exc}")
        except Exception as exc:
            errors.append(str(exc))
        diagnostics.append(
            diagnostic(
                "FA_QUARTERLY",
                f"vnstock {source}",
                "OK" if rows else "FAIL",
                rows=len(rows),
                errors=errors,
            )
        )
        if rows:
            periods = sorted({row["report_period"] for row in rows})
            diagnostics.append(
                diagnostic(
                    "FA_QUARTERLY",
                    f"vnstock {source}",
                    "WARNING",
                    periods=len(periods),
                    detail="Community edition period limit observed; published_date retained as null",
                )
            )
            return ensure_columns(pd.DataFrame(rows), FA_COLUMNS), diagnostics
    return empty_frame(FA_COLUMNS), diagnostics


def merge_financial_sources(
    primary: pd.DataFrame,
    supplement: pd.DataFrame,
    *,
    module: str,
    primary_label: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    combined = pd.concat([primary, supplement], ignore_index=True)
    if combined.empty:
        return empty_frame(FA_COLUMNS), diagnostics
    combined = ensure_columns(combined, FA_COLUMNS)
    combined["_priority"] = combined["source"].astype(str).apply(
        lambda value: 0 if primary_label.lower() in value.lower() else 1
    )
    keys = ["ticker", "statement", "report_period", "period_type", "item_code"]
    duplicate_rows = int(combined.duplicated(keys, keep=False).sum())
    merged_rows: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for key, group in combined.sort_values("_priority").groupby(keys, dropna=False, sort=False):
        chosen = group.iloc[0].copy()
        values = [float(value) for value in group["value"].dropna().tolist()]
        sources = list(dict.fromkeys(group["source"].dropna().astype(str).tolist()))
        chosen["source"] = " | ".join(sources)
        published = group["published_date"].dropna()
        chosen["published_date"] = published.iloc[0] if not published.empty else pd.NA
        if len(values) > 1:
            scale = max(abs(values[0]), 1.0)
            if any(abs(value - values[0]) / scale > 0.02 for value in values[1:]):
                mismatches.append(
                    {
                        "report_period": str(key[2]),
                        "item_code": str(key[4]),
                        "sources": sources,
                        "values": values,
                    }
                )
        merged_rows.append({column: chosen[column] for column in FA_COLUMNS})
    result = pd.DataFrame(merged_rows, columns=FA_COLUMNS)
    result = result.sort_values(["report_period", "statement", "item_code"]).reset_index(drop=True)
    diagnostics.append(
        diagnostic(
            module,
            "merged",
            "OK",
            rows=len(result),
            duplicate_candidates=duplicate_rows,
            cross_check_mismatches=mismatches[:30],
        )
    )
    return result, diagnostics


def select_fallback_gaps(primary: pd.DataFrame, fallback: pd.DataFrame) -> pd.DataFrame:
    """Keep fallback rows only for required item/period cells absent from DNSE."""
    if fallback.empty or primary.empty:
        return fallback
    candidate = fallback[fallback["item_code"].isin(FA_CRITICAL)].copy()
    if candidate.empty:
        return candidate
    keys = ["ticker", "statement", "report_period", "period_type", "item_code"]
    primary_keys = set(map(tuple, primary[keys].astype(str).to_numpy()))
    keep = [tuple(row) not in primary_keys for row in candidate[keys].astype(str).to_numpy()]
    return candidate.loc[keep].reset_index(drop=True)


def fetch_fa_annual(symbol: str, years: int = 10) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    years = min(10, max(5, years))
    dnse_frame, dnse_diag = fetch_dnse_financial(
        symbol,
        cycle_type="nam",
        cycle_number=years,
        period_type="annual",
        module="FA_ANNUAL",
    )
    dnse_missing = sorted(set(FA_CRITICAL) - set(dnse_frame["item_code"].dropna())) if not dnse_frame.empty else FA_CRITICAL.copy()
    dnse_periods = int(dnse_frame["report_period"].nunique()) if not dnse_frame.empty else 0
    if not dnse_frame.empty:
        dnse_diag.append(
            diagnostic(
                "FA_ANNUAL",
                "fallback",
                "NOT_USED",
                reason="DNSE returned data; optional missing variables remain null",
                dnse_periods=dnse_periods,
                dnse_missing_variables=dnse_missing,
            )
        )
        return dnse_frame, dnse_diag

    dnse_diag.append(
        diagnostic(
            "FA_ANNUAL",
            "fallback",
            "TRIGGERED",
            reason="DNSE failed or returned no rows",
            dnse_periods=dnse_periods,
            dnse_missing_variables=dnse_missing,
        )
    )
    vnf_frame, vnf_diag = fetch_vnf_annual(symbol, years)
    vnf_frame = select_fallback_gaps(dnse_frame, vnf_frame)
    merged, merge_diag = merge_financial_sources(
        dnse_frame,
        vnf_frame,
        module="FA_ANNUAL",
        primary_label="DNSE",
    )
    return merged, dnse_diag + vnf_diag + merge_diag


def fetch_fa_quarterly(symbol: str, quarters: int = 40) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    # Verified against the current DNSE endpoint: cycleNumber accepts at most 10
    # quarterly periods (12/20/40 return HTTP 400).
    dnse_quarters = min(10, max(1, quarters))
    dnse_frame, dnse_diag = fetch_dnse_financial(
        symbol,
        cycle_type="quy",
        cycle_number=dnse_quarters,
        period_type="quarterly",
        module="FA_QUARTERLY",
    )
    dnse_missing = sorted(set(FA_CRITICAL) - set(dnse_frame["item_code"].dropna())) if not dnse_frame.empty else FA_CRITICAL.copy()
    dnse_periods = int(dnse_frame["report_period"].nunique()) if not dnse_frame.empty else 0
    if not dnse_frame.empty:
        dnse_diag.append(
            diagnostic(
                "FA_QUARTERLY",
                "fallback",
                "NOT_USED",
                reason="DNSE returned data; optional missing variables remain null",
                dnse_periods=dnse_periods,
                dnse_missing_variables=dnse_missing,
            )
        )
        return dnse_frame, dnse_diag

    dnse_diag.append(
        diagnostic(
            "FA_QUARTERLY",
            "fallback",
            "TRIGGERED",
            reason="DNSE failed or returned no rows",
            dnse_periods=dnse_periods,
            dnse_missing_variables=dnse_missing,
        )
    )
    community_frame, community_diag = fetch_vnstock_quarterly(symbol)
    community_frame = select_fallback_gaps(dnse_frame, community_frame)
    merged, merge_diag = merge_financial_sources(
        dnse_frame,
        community_frame,
        module="FA_QUARTERLY",
        primary_label="DNSE",
    )
    return merged, dnse_diag + community_diag + merge_diag


# =============================================================================
# 4. DAILY EOD AND BENCHMARK
# =============================================================================
def _price_frame(
    symbol: str,
    dates: Iterable[Any],
    opens: Iterable[Any],
    highs: Iterable[Any],
    lows: Iterable[Any],
    closes: Iterable[Any],
    volumes: Iterable[Any],
    source: str,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "ticker": symbol,
            "date": list(dates),
            "open": list(opens),
            "high": list(highs),
            "low": list(lows),
            "close": list(closes),
            "volume": list(volumes),
        }
    )
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce", utc=True).dt.strftime("%Y-%m-%d")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    # DNSE/vnstock equity candles use VND thousands.  Store equities in actual
    # VND so price, dividend, and rights-price units remain consistent.  VNINDEX
    # is an index level and must not be scaled.
    if symbol != "VNINDEX" and frame["close"].dropna().median() < 1000:
        for column in ("open", "high", "low", "close"):
            frame[column] = (frame[column] * 1000.0).round(4)
    # Neither current DNSE chart nor Community Quote exposes actual traded value.
    frame["trading_value"] = (frame["close"] * frame["volume"]).round(2)
    frame["trading_value_is_estimated"] = True
    frame["source"] = source
    if source == "DNSE":
        def checksum(row: pd.Series) -> str:
            values = [symbol, str(row["date"])[:10]]
            for column in ("open", "high", "low", "close", "volume"):
                value = row[column]
                values.append((format(float(value), ".8f").rstrip("0").rstrip(".") or "0") if pd.notna(value) else "")
            return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()
        frame["dnse_checksum"] = frame.apply(checksum, axis=1)
    else:
        frame["dnse_checksum"] = pd.NA
    if source.strip().casefold() in SOURCE_ADJUSTED_PRICE_SOURCES:
        for adjusted, raw in {
            "adjusted_open": "open",
            "adjusted_high": "high",
            "adjusted_low": "low",
            "adjusted_close": "close",
        }.items():
            frame[adjusted] = frame[raw]
        frame["adjustment_status"] = "SOURCE_ADJUSTED"
        frame["adjustment_source"] = source
    else:
        for column in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close"):
            frame[column] = np.nan
        frame["adjustment_status"] = "PENDING"
        frame["adjustment_source"] = pd.NA
    frame = frame.dropna(subset=["date", "close"]).sort_values("date")
    frame = frame.drop_duplicates(["ticker", "date"], keep="last").reset_index(drop=True)
    return ensure_columns(frame, TA_COLUMNS)


def _valid_ohlc_mask(frame: pd.DataFrame) -> pd.Series:
    """Return True only for complete, non-negative and internally valid bars."""
    values = frame[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    complete = values.notna().all(axis=1) & values.ge(0).all(axis=1)
    ordered = (
        values["high"].ge(values[["open", "close", "low"]].max(axis=1))
        & values["low"].le(values[["open", "close", "high"]].min(axis=1))
    )
    return complete & ordered


def _needs_price_fallback(frame: pd.DataFrame) -> bool:
    if frame.empty or not bool(_valid_ohlc_mask(frame).all()):
        return True
    close = pd.to_numeric(frame["close"], errors="coerce")
    return bool(close.pct_change(fill_method=None).abs().gt(PRICE_SUSPICIOUS_RETURN).any())


def _reconcile_price_sources(primary: pd.DataFrame, fallback: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Prefer DNSE, but replace invalid or materially inconsistent rows with KBS/VCI."""
    if primary.empty:
        return ensure_columns(fallback, TA_COLUMNS), len(fallback)
    if fallback.empty:
        return ensure_columns(primary, TA_COLUMNS), 0

    left = ensure_columns(primary, TA_COLUMNS).set_index("date", drop=False)
    right = ensure_columns(fallback, TA_COLUMNS).set_index("date", drop=False)
    overlap = left.index.intersection(right.index)
    primary_valid = _valid_ohlc_mask(left.loc[overlap])
    fallback_valid = _valid_ohlc_mask(right.loc[overlap])
    primary_close = pd.to_numeric(left.loc[overlap, "close"], errors="coerce")
    fallback_close = pd.to_numeric(right.loc[overlap, "close"], errors="coerce")
    discrepancy = (primary_close / fallback_close - 1.0).abs()
    replace_dates = overlap[(fallback_valid & (~primary_valid | discrepancy.gt(PRICE_FALLBACK_DISCREPANCY))).to_numpy()]

    if len(replace_dates):
        original_dnse_checksums = left.loc[replace_dates, "dnse_checksum"].copy()
        left.loc[replace_dates, TA_COLUMNS] = right.loc[replace_dates, TA_COLUMNS]
        left.loc[replace_dates, "dnse_checksum"] = original_dnse_checksums
    missing_dates = right.index.difference(left.index)
    if len(missing_dates):
        left = pd.concat([left, right.loc[missing_dates]], axis=0)
    result = left.reset_index(drop=True).sort_values("date").reset_index(drop=True)
    return ensure_columns(result, TA_COLUMNS), len(replace_dates) + len(missing_dates)


def fetch_ta_daily_range(
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    symbol = symbol.strip().upper()
    diagnostics: list[dict[str, Any]] = []
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise ValueError("TA start must be earlier than end")
    dnse_frame = empty_frame(TA_COLUMNS)
    try:
        dnse_asset_path = "index" if symbol == "VNINDEX" else "stock"
        raw = http_get_json(
            f"https://api.dnse.com.vn/chart-api/v2/ohlcs/{dnse_asset_path}",
            params={
                "resolution": "1D",
                "symbol": symbol,
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        if isinstance(raw, dict) and raw.get("t"):
            timestamps = raw.get("t", [])
            frame = _price_frame(
                symbol,
                [datetime.fromtimestamp(value, tz=timezone.utc) for value in timestamps],
                raw.get("o", []),
                raw.get("h", []),
                raw.get("l", []),
                raw.get("c", []),
                raw.get("v", []),
                "DNSE",
            )
            dnse_frame = frame
            diagnostics.append(diagnostic("TA_DAILY", "DNSE", "OK", rows=len(frame)))
            diagnostics.append(
                diagnostic(
                    "TA_DAILY",
                    "DNSE",
                    "WARNING",
                    detail="Equity OHLC converted from VND thousands to VND; trading_value is estimated as close * volume",
                )
            )
            if not _needs_price_fallback(frame):
                return frame, diagnostics
            diagnostics.append(
                diagnostic(
                    "TA_DAILY",
                    "DNSE",
                    "WARNING",
                    detail="Invalid OHLC or >50% adjacent-session move; checking row-level fallback",
                )
            )
        diagnostics.append(diagnostic("TA_DAILY", "DNSE", "EMPTY"))
    except Exception as exc:
        diagnostics.append(diagnostic("TA_DAILY", "DNSE", "FAIL", detail=str(exc)))

    for source in ("KBS", "VCI"):
        try:
            from vnstock import Quote

            wait_for_community_slot()
            quote = Quote(source=source, symbol=symbol)
            raw_frame = quote.history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1D",
            )
            if isinstance(raw_frame, pd.DataFrame) and not raw_frame.empty:
                frame = _price_frame(
                    symbol,
                    raw_frame["time"],
                    raw_frame["open"],
                    raw_frame["high"],
                    raw_frame["low"],
                    raw_frame["close"],
                    raw_frame["volume"],
                    f"vnstock {source}",
                )
                diagnostics.append(diagnostic("TA_DAILY", f"vnstock {source}", "OK", rows=len(frame)))
                diagnostics.append(
                    diagnostic(
                        "TA_DAILY",
                        f"vnstock {source}",
                        "WARNING",
                        detail="Equity OHLC converted from VND thousands to VND; trading_value is estimated as close * volume",
                    )
                )
                reconciled, replaced = _reconcile_price_sources(dnse_frame, frame)
                diagnostics.append(
                    diagnostic(
                        "TA_DAILY",
                        f"DNSE+{source}",
                        "OK",
                        rows=replaced,
                        detail="Rows replaced/added from fallback after OHLC and adjusted-price consistency checks",
                    )
                )
                return reconciled, diagnostics
            diagnostics.append(diagnostic("TA_DAILY", f"vnstock {source}", "EMPTY"))
        except Exception as exc:
            diagnostics.append(diagnostic("TA_DAILY", f"vnstock {source}", "FAIL", detail=str(exc)))
    return dnse_frame, diagnostics


def fetch_ta_daily(symbol: str, years: int = 8) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    years = min(10, max(5, years))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=years * 366)
    return fetch_ta_daily_range(symbol, start, end)


def fetch_benchmark(years: int = 8) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    frame, diagnostics = fetch_ta_daily("VNINDEX", years=years)
    for item in diagnostics:
        item["module"] = "BENCHMARK"
    return frame, diagnostics


# =============================================================================
# 5. CORPORATE ACTIONS AND PRICE ADJUSTMENT
# =============================================================================
def classify_action(row: pd.Series) -> dict[str, Any] | None:
    code = str(row.get("event_code", "")).upper()
    description = ascii_text(
        " ".join(
            str(row.get(column, ""))
            for column in ("event_name_vi", "event_name_en", "event_title_vi", "event_title_en")
        )
    )
    ex_date = first_present(row, "exright_date", "ex_date", "record_date")
    if ex_date is None:
        return None
    ratio = safe_float(first_present(row, "exercise_ratio", "ratio"))
    cash = safe_float(first_present(row, "value_per_share", "cash_dividend", "value"))
    action_type: str | None = None
    result = {
        "ex_date": str(ex_date)[:10],
        "action_type": None,
        "cash_dividend": None,
        "stock_ratio": None,
        "split_ratio": None,
        "rights_ratio": None,
        "rights_price": None,
    }
    if code == "DIV" or "co tuc bang tien" in description or "cash dividend" in description:
        action_type = "cash_dividend"
        result["cash_dividend"] = cash
    elif "tach co phieu" in description or "stock split" in description:
        action_type = "stock_split"
        result["split_ratio"] = ratio
    elif "quyen mua" in description or "rights issue" in description:
        action_type = "rights_issue"
        result["rights_ratio"] = ratio
        price_match = re.search(r"(?:gia|price)[^0-9]{0,15}([0-9][0-9.,]*)", description)
        result["rights_price"] = safe_float(price_match.group(1)) if price_match else cash
    elif "co tuc bang co phieu" in description or "stock dividend" in description:
        action_type = "stock_dividend"
        result["stock_ratio"] = ratio
    elif "co phieu thuong" in description or "bonus" in description:
        action_type = "bonus_shares"
        result["stock_ratio"] = ratio
    elif code == "ISS" or "phat hanh co phieu" in description or "capital increase" in description:
        action_type = "capital_increase"
        result["stock_ratio"] = ratio
    if action_type is None:
        return None
    result["action_type"] = action_type
    return result


def fetch_corporate_actions(
    symbol: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]], bool]:
    diagnostics: list[dict[str, Any]] = [
        diagnostic(
            "CORPORATE_ACTIONS",
            "DNSE",
            "UNAVAILABLE",
            detail="No verified DNSE corporate-action endpoint is configured; fallback triggered",
        )
    ]
    for source in ("VCI", "KBS"):
        try:
            from vnstock import Company

            wait_for_community_slot()
            company = Company(source=source, symbol=symbol)
            events = company.events()
            if not isinstance(events, pd.DataFrame):
                diagnostics.append(diagnostic("CORPORATE_ACTIONS", f"vnstock {source}", "FAIL", detail="Non-tabular response"))
                continue
            normalized: list[dict[str, Any]] = []
            for _, row in events.iterrows():
                action = classify_action(row)
                if action:
                    action["ticker"] = symbol
                    action["source"] = f"vnstock {source}"
                    normalized.append(action)
            diagnostics.append(
                diagnostic(
                    "CORPORATE_ACTIONS",
                    f"vnstock {source}",
                    "OK" if not events.empty else "EMPTY",
                    raw_rows=len(events),
                    recognized_rows=len(normalized),
                )
            )
            if not events.empty and normalized:
                frame = ensure_columns(pd.DataFrame(normalized), CA_COLUMNS)
                if not frame.empty:
                    frame = frame.drop_duplicates(
                        ["ticker", "ex_date", "action_type", "cash_dividend", "stock_ratio", "rights_ratio"],
                        keep="last",
                    ).sort_values("ex_date")
                return frame.reset_index(drop=True), diagnostics, True
        except Exception as exc:
            diagnostics.append(diagnostic("CORPORATE_ACTIONS", f"vnstock {source}", "FAIL", detail=str(exc)))
    return empty_frame(CA_COLUMNS), diagnostics, False


def _price_unit_value(raw_value: float, reference_price: float) -> float:
    if reference_price < 1000 and raw_value >= 1000:
        return raw_value / 1000.0
    return raw_value


def apply_corporate_actions(
    price_frame: pd.DataFrame,
    action_frame: pd.DataFrame,
    *,
    actions_reliable: bool,
    history_complete: bool,
) -> tuple[pd.DataFrame, str, list[str]]:
    frame = ensure_columns(price_frame, TA_COLUMNS).copy()
    warnings: list[str] = []
    if frame.empty:
        return frame, "MISSING", ["No TA rows to adjust"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame = frame.sort_values("date").reset_index(drop=True)
    adjusted_columns = {
        "adjusted_open": "open",
        "adjusted_high": "high",
        "adjusted_low": "low",
        "adjusted_close": "close",
    }
    # DNSE is preferred, with VCI/KBS as fallbacks. Their historical chart
    # series is already back-adjusted by the provider. Reapplying corporate
    # actions here would double-adjust prices, so preserve the provider series
    # as the canonical adjusted OHLC values.
    source_adjusted = frame["source"].map(
        lambda value: str(value).strip().casefold() in SOURCE_ADJUSTED_PRICE_SOURCES
    )
    if bool(source_adjusted.all()):
        for adjusted, raw in adjusted_columns.items():
            frame[adjusted] = pd.to_numeric(frame[raw], errors="coerce")
        frame["adjustment_status"] = "SOURCE_ADJUSTED"
        frame["adjustment_source"] = frame["source"]
        return ensure_columns(frame, TA_COLUMNS), "SOURCE_ADJUSTED", []

    if not actions_reliable:
        for adjusted in adjusted_columns:
            frame[adjusted] = np.nan
        frame["adjustment_status"] = "MISSING"
        frame["adjustment_source"] = pd.NA
        return frame, "MISSING", ["Corporate-action source unavailable; adjusted prices intentionally null"]

    factors = np.ones(len(frame), dtype=float)
    applied = 0
    incomplete = not history_complete
    min_date, max_date = frame["date"].min(), frame["date"].max()
    for _, event in action_frame.sort_values("ex_date", ascending=False).iterrows():
        ex_date = str(event.get("ex_date", ""))[:10]
        if not ex_date or ex_date > max_date:
            continue
        mask = frame["date"] < ex_date
        if not mask.any() or ex_date <= min_date:
            continue
        pre_index = np.flatnonzero(mask.to_numpy())[-1]
        pre_close = safe_float(frame.loc[pre_index, "close"])
        if not pre_close or pre_close <= 0:
            incomplete = True
            warnings.append(f"{ex_date}: missing pre-event close")
            continue
        action_type = str(event.get("action_type", ""))
        factor: float | None = None
        if action_type == "cash_dividend":
            cash = safe_float(event.get("cash_dividend"))
            if cash:
                cash = _price_unit_value(cash, pre_close)
                if 0 < cash < pre_close:
                    factor = (pre_close - cash) / pre_close
        elif action_type in {"stock_dividend", "bonus_shares"}:
            ratio = safe_float(event.get("stock_ratio"))
            if ratio and ratio > 0:
                ratio = ratio / 100.0 if ratio > 1 else ratio
                factor = 1.0 / (1.0 + ratio)
        elif action_type == "stock_split":
            split_ratio = safe_float(event.get("split_ratio"))
            if split_ratio and split_ratio > 0:
                factor = 1.0 / split_ratio
        elif action_type == "rights_issue":
            rights_ratio = safe_float(event.get("rights_ratio"))
            rights_price = safe_float(event.get("rights_price"))
            if rights_ratio and rights_price is not None and rights_ratio > 0:
                rights_ratio = rights_ratio / 100.0 if rights_ratio > 1 else rights_ratio
                rights_price = _price_unit_value(rights_price, pre_close)
                # TERP formula is intentionally separate from stock dividends.
                factor = (pre_close + rights_ratio * rights_price) / (
                    pre_close * (1.0 + rights_ratio)
                )
        elif action_type == "capital_increase":
            warnings.append(f"{ex_date}: capital increase retained but not adjusted without terms")
            incomplete = True
            continue
        if factor is None or not (0 < factor <= 1.5):
            incomplete = True
            warnings.append(f"{ex_date}: insufficient/invalid terms for {action_type}")
            continue
        factors[mask.to_numpy()] *= factor
        applied += 1

    if action_frame.empty:
        status = "NOT_REQUIRED"
    elif applied == 0:
        status = "PARTIAL" if incomplete else "NOT_REQUIRED"
    else:
        status = "PARTIAL" if incomplete else "APPLIED"
    for adjusted, raw in adjusted_columns.items():
        frame[adjusted] = pd.to_numeric(frame[raw], errors="coerce") * factors
    frame["adjustment_status"] = status
    frame["adjustment_source"] = "corporate_actions" if status in {"APPLIED", "PARTIAL"} else pd.NA
    return ensure_columns(frame, TA_COLUMNS), status, warnings


# =============================================================================
# 6. COVERAGE, QUALITY, AND OUTPUT
# =============================================================================
def financial_stats(frame: pd.DataFrame, period_type: str) -> dict[str, Any]:
    if frame.empty:
        return {
            "periods": 0,
            "missing_percent": 100.0,
            "duplicate_rows": 0,
            "missing_critical_variables": FA_CRITICAL.copy(),
            "published_date_missing_percent": 100.0,
        }
    keys = ["ticker", "statement", "report_period", "period_type", "item_code"]
    return {
        "periods": int(frame["report_period"].nunique()),
        "missing_percent": round(float(frame["value"].isna().mean() * 100), 2),
        "duplicate_rows": int(frame.duplicated(keys).sum()),
        "missing_critical_variables": sorted(set(FA_CRITICAL) - set(frame["item_code"].dropna())),
        "published_date_missing_percent": round(float(frame["published_date"].isna().mean() * 100), 2),
        "period_type": period_type,
    }


def ta_stats(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {
            "sessions": 0,
            "first_date": None,
            "last_date": None,
            "missing_percent": 100.0,
            "duplicate_rows": 0,
        }
    raw_columns = ["open", "high", "low", "close", "volume"]
    return {
        "sessions": len(frame),
        "first_date": str(frame["date"].min()),
        "last_date": str(frame["date"].max()),
        "missing_percent": round(float(frame[raw_columns].isna().mean().mean() * 100), 2),
        "duplicate_rows": int(frame.duplicated(["ticker", "date"]).sum()),
    }


def dataset_status(frame: pd.DataFrame, stats: dict[str, Any], critical_threshold: int = 0) -> str:
    if frame.empty:
        return "MISSING"
    missing = len(stats.get("missing_critical_variables", []))
    return "OK" if missing <= critical_threshold else "PARTIAL"


def build_coverage_report(
    metadata: dict[str, Any],
    annual: pd.DataFrame,
    quarterly: pd.DataFrame,
    ta: pd.DataFrame,
    actions: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for dataset_name, frame in (("fa_annual", annual), ("fa_quarterly", quarterly)):
        for variable in FA_CRITICAL:
            subset = frame[frame["item_code"] == variable] if not frame.empty else frame
            sources = " | ".join(sorted(set(subset["source"].dropna().astype(str)))) if not subset.empty else ""
            periods = int(subset["report_period"].nunique()) if not subset.empty else 0
            published_missing = bool(subset["published_date"].isna().any()) if not subset.empty else True
            rows.append(
                {
                    "variable": f"{dataset_name}.{variable}",
                    "available": not subset.empty,
                    "source": sources,
                    "periods_available": periods,
                    "notes": "published_date missing; exclude from point-in-time backtest until resolved"
                    if not subset.empty and published_missing
                    else ("" if not subset.empty else "not returned by available sources"),
                }
            )
    for field in METADATA_FIELDS[1:]:
        rows.append(
            {
                "variable": f"metadata.{field}",
                "available": not is_missing(metadata.get(field)),
                "source": "combined metadata",
                "periods_available": 1 if not is_missing(metadata.get(field)) else 0,
                "notes": "" if not is_missing(metadata.get(field)) else "field unavailable; retained as null",
            }
        )
    for field in ("open", "high", "low", "close", "adjusted_close", "volume", "trading_value"):
        available = not ta.empty and field in ta and ta[field].notna().any()
        rows.append(
            {
                "variable": f"ta_daily.{field}",
                "available": bool(available),
                "source": " | ".join(sorted(set(ta["source"].dropna().astype(str)))) if not ta.empty else "",
                "periods_available": int(ta[field].notna().sum()) if available else 0,
                "notes": "estimated" if field == "trading_value" and available and ta["trading_value_is_estimated"].any() else "",
            }
        )
    for action_type in ("cash_dividend", "stock_dividend", "bonus_shares", "stock_split", "rights_issue", "capital_increase"):
        subset = actions[actions["action_type"] == action_type] if not actions.empty else actions
        rows.append(
            {
                "variable": f"corporate_actions.{action_type}",
                "available": not subset.empty,
                "source": " | ".join(sorted(set(subset["source"].dropna().astype(str)))) if not subset.empty else "",
                "periods_available": len(subset),
                "notes": "",
            }
        )
    return pd.DataFrame(rows, columns=["variable", "available", "source", "periods_available", "notes"])


def summarize_source_results(symbol: str, diagnostics: list[dict[str, Any]]) -> None:
    for item in diagnostics:
        if item.get("status") in {
            "OK", "FAIL", "MISSING_CREDENTIALS", "EMPTY", "TRIGGERED", "NOT_USED", "UNAVAILABLE"
        }:
            logger.info(
                "[%s] %-18s | %-24s | %s",
                symbol,
                item.get("module"),
                item.get("source"),
                item.get("status"),
            )


def scrape_symbol(
    symbol: str,
    years: int = 10,
    ta_years: int = 8,
    output_dir: str = "scraper_output",
) -> dict[str, Any]:
    symbol = symbol.strip().upper()
    symbol_dir = Path(output_dir) / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    diagnostics: list[dict[str, Any]] = []

    metadata, source_diag = fetch_metadata(symbol)
    diagnostics.extend(source_diag)

    annual, source_diag = fetch_fa_annual(symbol, years=years)
    diagnostics.extend(source_diag)

    quarterly, source_diag = fetch_fa_quarterly(symbol, quarters=40)
    diagnostics.extend(source_diag)

    ta_raw, source_diag = fetch_ta_daily(symbol, years=ta_years)
    diagnostics.extend(source_diag)

    actions, source_diag, actions_reliable = fetch_corporate_actions(symbol)
    diagnostics.extend(source_diag)

    history_complete = False
    if actions_reliable:
        if actions.empty:
            history_complete = True
        elif not ta_raw.empty:
            history_complete = str(actions["ex_date"].min()) <= str(ta_raw["date"].min())
            if not history_complete:
                diagnostics.append(
                    diagnostic(
                        "CORPORATE_ACTIONS",
                        "combined",
                        "WARNING",
                        detail="Event history begins after TA history; older adjustments may be missing",
                        first_action=str(actions["ex_date"].min()),
                        first_ta=str(ta_raw["date"].min()),
                    )
                )
    ta, adjustment_status, adjustment_warnings = apply_corporate_actions(
        ta_raw,
        actions,
        actions_reliable=actions_reliable,
        history_complete=history_complete,
    )
    for warning in adjustment_warnings:
        diagnostics.append(diagnostic("PRICE_ADJUSTMENT", "corporate_actions", "WARNING", detail=warning))

    annual_stats = financial_stats(annual, "annual")
    quarterly_stats = financial_stats(quarterly, "quarterly")
    price_stats = ta_stats(ta)
    metadata_missing = [field for field in METADATA_FIELDS[1:] if is_missing(metadata.get(field))]

    metadata_status = "OK" if not metadata_missing else ("PARTIAL" if len(metadata_missing) < len(METADATA_FIELDS) - 1 else "MISSING")
    annual_status = dataset_status(annual, annual_stats)
    quarterly_status = dataset_status(quarterly, quarterly_stats)
    ta_status = "OK" if not ta.empty else "MISSING"
    if not actions_reliable:
        corporate_action_status = "MISSING"
    elif not history_complete:
        corporate_action_status = "PARTIAL"
    else:
        corporate_action_status = "OK" if not actions.empty else "EMPTY"

    diagnostics_document = {
        "ticker": symbol,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "statuses": {
            "metadata_status": metadata_status,
            "fa_annual_status": annual_status,
            "fa_quarterly_status": quarterly_status,
            "ta_status": ta_status,
            "corporate_action_status": corporate_action_status,
            "adjustment_status": adjustment_status,
        },
        "quality": {
            "metadata_missing_fields": metadata_missing,
            "fa_annual": annual_stats,
            "fa_quarterly": quarterly_stats,
            "ta_daily": price_stats,
            "corporate_action_rows": len(actions),
        },
        "warnings": [
            "Do not use a financial row in a point-in-time backtest when published_date is null.",
            "Current ticker universes can omit delisted securities; survivorship bias remains possible.",
            "Annual and quarterly observations must never be mixed without an explicit transformation.",
            "SOURCE_ADJUSTED prices come directly from DNSE, VCI, or KBS and must not be adjusted a second time.",
        ],
        "source_diagnostics": diagnostics,
    }

    coverage = build_coverage_report(metadata, annual, quarterly, ta, actions)
    with open(symbol_dir / "metadata.json", "w", encoding="utf-8") as handle:
        json.dump(json_safe(metadata), handle, ensure_ascii=False, indent=2, allow_nan=False)
    annual.to_csv(symbol_dir / "fa_annual.csv", index=False, encoding="utf-8-sig")
    quarterly.to_csv(symbol_dir / "fa_quarterly.csv", index=False, encoding="utf-8-sig")
    ta.to_csv(symbol_dir / "ta_daily.csv", index=False, encoding="utf-8-sig")
    actions.to_csv(symbol_dir / "corporate_actions.csv", index=False, encoding="utf-8-sig")
    coverage.to_csv(symbol_dir / "coverage_report.csv", index=False, encoding="utf-8-sig")
    with open(symbol_dir / "diagnostics.json", "w", encoding="utf-8") as handle:
        json.dump(json_safe(diagnostics_document), handle, ensure_ascii=False, indent=2, allow_nan=False)

    summarize_source_results(symbol, diagnostics)
    logger.info(
        "[%s] TA sessions=%d, range=%s..%s, adjustment=%s",
        symbol,
        price_stats["sessions"],
        price_stats["first_date"],
        price_stats["last_date"],
        adjustment_status,
    )
    for variable in FA_CRITICAL:
        annual_periods = int(annual.loc[annual["item_code"] == variable, "report_period"].nunique()) if not annual.empty else 0
        quarter_periods = int(quarterly.loc[quarterly["item_code"] == variable, "report_period"].nunique()) if not quarterly.empty else 0
        logger.info("[%s] FA coverage %-28s annual=%d quarterly=%d", symbol, variable, annual_periods, quarter_periods)

    return {
        "symbol": symbol,
        **diagnostics_document["statuses"],
        "fa_annual_periods": annual_stats["periods"],
        "fa_quarterly_periods": quarterly_stats["periods"],
        "ta_sessions": price_stats["sessions"],
        "ta_first_date": price_stats["first_date"],
        "ta_last_date": price_stats["last_date"],
        "annual_rows": len(annual),
        "quarterly_rows": len(quarterly),
        "ta_rows": len(ta),
        "ca_rows": len(actions),
    }


def load_existing_result(output_path: Path, symbol: str) -> dict[str, Any] | None:
    diagnostics_file = output_path / symbol / "diagnostics.json"
    if not diagnostics_file.exists():
        return None
    try:
        with open(diagnostics_file, encoding="utf-8") as handle:
            document = json.load(handle)
        quality = document.get("quality", {})
        statuses = document.get("statuses", {})
        return {
            "symbol": symbol,
            **statuses,
            "fa_annual_periods": quality.get("fa_annual", {}).get("periods", 0),
            "fa_quarterly_periods": quality.get("fa_quarterly", {}).get("periods", 0),
            "ta_sessions": quality.get("ta_daily", {}).get("sessions", 0),
            "ta_first_date": quality.get("ta_daily", {}).get("first_date"),
            "ta_last_date": quality.get("ta_daily", {}).get("last_date"),
        }
    except Exception:
        return None


def scrape_batch(
    symbols: list[str],
    years: int = 10,
    ta_years: int = 8,
    output_dir: str = "scraper_output",
    workers: int = 3,
    resume: bool = False,
) -> list[dict[str, Any]]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    symbols = sorted(set(symbol.strip().upper() for symbol in symbols if symbol.strip()))

    if not DNSE_API_KEY or not DNSE_API_SECRET:
        logger.warning("DNSE credentials missing; financial fallbacks will be used and the run will continue")
    logger.warning(
        "SURVIVORSHIP BIAS: the current symbol list may omit delisted securities; do not treat it as a historical universe"
    )

    logger.info("Fetching VNINDEX benchmark for %d years", ta_years)
    benchmark, benchmark_diagnostics = fetch_benchmark(years=ta_years)
    benchmark.to_csv(output_path / "vnindex_daily.csv", index=False, encoding="utf-8-sig")
    for item in benchmark_diagnostics:
        logger.info("[VNINDEX] %s | %s | %s", item["source"], item["status"], item.get("detail", ""))

    prior_results: list[dict[str, Any]] = []
    to_scrape = symbols
    if resume:
        to_scrape = []
        for symbol in symbols:
            existing = load_existing_result(output_path, symbol)
            required = [
                output_path / symbol / filename
                for filename in (
                    "metadata.json",
                    "fa_annual.csv",
                    "fa_quarterly.csv",
                    "ta_daily.csv",
                    "corporate_actions.csv",
                    "diagnostics.json",
                    "coverage_report.csv",
                )
            ]
            if existing and all(path.exists() for path in required):
                prior_results.append(existing)
            else:
                to_scrape.append(symbol)
        logger.info("Resume mode: %d/%d tickers remain", len(to_scrape), len(symbols))

    results: list[dict[str, Any]] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(scrape_symbol, symbol, years, ta_years, output_dir): symbol
            for symbol in to_scrape
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result()
                results.append(result)
                logger.info(
                    "[%s] done | annual=%d rows | quarterly=%d rows | TA=%d | CA=%d",
                    symbol,
                    result["annual_rows"],
                    result["quarterly_rows"],
                    result["ta_rows"],
                    result["ca_rows"],
                )
            except Exception as exc:
                logger.exception("[%s] failed: %s", symbol, exc)
    logger.info("Batch completed in %.1f seconds", time.time() - started)

    all_results = prior_results + results
    metadata_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        metadata_file = output_path / symbol / "metadata.json"
        if metadata_file.exists():
            try:
                with open(metadata_file, encoding="utf-8") as handle:
                    metadata_rows.append(json.load(handle))
            except Exception as exc:
                logger.warning("[%s] Cannot read metadata summary: %s", symbol, exc)
    pd.DataFrame(metadata_rows, columns=METADATA_FIELDS).to_csv(
        output_path / "summary_metadata.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(all_results).to_csv(
        output_path / "summary_coverage.csv", index=False, encoding="utf-8-sig"
    )
    return results


# =============================================================================
# 7. CLI
# =============================================================================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vietnam equity data-layer scraper (vnfinancialdata + DNSE + vnstock Community)",
    )
    parser.add_argument("--symbols", "-s", help="Comma-separated tickers, e.g. FPT,VCB,SSI")
    parser.add_argument("--all", "-a", action="store_true", help="Fetch all current HOSE and HNX tickers")
    parser.add_argument("--exchange", "-e", choices=["HOSE", "HNX", "ALL"], help="Exchange filter")
    parser.add_argument("--years", "-y", type=int, default=10, help="Annual FA history, clamped to 5-10 years")
    parser.add_argument("--ta-years", type=int, default=8, help="Daily EOD history, clamped to 5-10 years")
    parser.add_argument("--output", "-o", default="scraper_output", help="Output directory")
    parser.add_argument("--workers", "-w", type=int, default=3, help="Parallel ticker workers")
    parser.add_argument("--resume", action="store_true", help="Skip tickers with all required output files")
    args = parser.parse_args()

    if args.years < 5 or args.years > 10:
        logger.warning("--years=%d is outside 5-10; clamping", args.years)
    if args.ta_years < 5 or args.ta_years > 10:
        logger.warning("--ta-years=%d is outside 5-10; clamping", args.ta_years)
    years = min(10, max(5, args.years))
    ta_years = min(10, max(5, args.ta_years))

    if args.all or args.exchange:
        symbols = get_all_symbols(args.exchange or "ALL")
        logger.info("Found %d current tickers", len(symbols))
    elif args.symbols:
        symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
    else:
        parser.error("Use --symbols FPT,VCB or --all / --exchange HOSE")

    if not symbols:
        parser.error("No tickers were returned by the available listing sources")
    scrape_batch(
        symbols=symbols,
        years=years,
        ta_years=ta_years,
        output_dir=args.output,
        workers=args.workers,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
