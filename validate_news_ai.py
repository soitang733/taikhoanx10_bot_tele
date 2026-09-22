"""Manual smoke test for the three-part stock-news experience.

Runs real configured search/AI providers, but prints only coverage and structural
checks so API keys and long provider payloads never enter logs.
"""

from __future__ import annotations

import json
import re
import sys
import time

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from stock_research import TRUSTED_POLICY_DOMAINS, _search, grounded_news


API = "http://127.0.0.1:8765"
DEFAULT_TICKERS = ["PET", "MSB", "PVP", "TVS", "PVT", "NTP", "KSV", "DHC", "HHP", "SSB"]


def api_data(path: str, **params):
    response = requests.get(API + path, params=params, timeout=10)
    response.raise_for_status()
    return response.json().get("data")


def validate(ticker: str) -> dict[str, object]:
    signal = api_data("/v1/signal", ticker=ticker) or {}
    started = time.monotonic()
    answer, sources = grounded_news(
        ticker,
        str(signal.get("company_name") or ""),
        str(signal.get("industry") or signal.get("sector") or ""),
    )
    categories = {
        re.sub(r"^(Doanh nghiệp|Ngành|Vĩ mô):\s*", "", str(source.get("title") or ""), flags=re.I)
        for source in sources
    }
    del categories  # Source titles are intentionally not printed by this smoke test.
    return {
        "ticker": ticker,
        "seconds": round(time.monotonic() - started, 2),
        "has_company_block": "DOANH NGHIỆP" in answer.upper() or "Doanh nghiệp" in answer,
        "has_industry_block": "NGÀNH" in answer.upper() or "Ngành" in answer,
        "has_macro_block": "VĨ MÔ" in answer.upper() or "Kinh tế Việt Nam" in answer,
        "source_count": len(sources),
        "answer_chars": len(answer),
        "has_markdown_marker": any(marker in answer for marker in ("**", "```", "###")),
    }


def main() -> int:
    tickers = [value.strip().upper() for value in sys.argv[1:]] or DEFAULT_TICKERS
    try:
        probe = _search("VN-Index Việt Nam phiên gần nhất", domains=TRUSTED_POLICY_DOMAINS,
                        category="macro")
        provider = {"search_probe": "ok", "result_count": len(probe)}
    except Exception as exc:
        provider = {"search_probe": type(exc).__name__,
                    "status": getattr(exc, "status_code", None),
                    "provider_status": getattr(exc, "provider_status", None)}
    results = []
    for ticker in tickers:
        try:
            results.append(validate(ticker))
        except Exception as exc:  # keep testing the remaining symbols
            results.append({"ticker": ticker, "error": type(exc).__name__})
    print(json.dumps({"provider": provider, "results": results}, ensure_ascii=False, indent=2))
    return int(any(item.get("error") or not all(item.get(key) for key in (
        "has_company_block", "has_industry_block", "has_macro_block"
    )) for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
