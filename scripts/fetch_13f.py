"""Fetch the latest SEC 13F-HR filings for each tracked investor and rebuild data/investors.json.

Usage:
    python scripts/fetch_13f.py

Required env:
    EDGAR_USER_AGENT   "Whale Watch <your-email@example.com>"   (SEC requires UA with contact)

Optional env:
    OPENFIGI_API_KEY   raises CUSIP-lookup rate limit (free signup at openfigi.com)

What it does:
  1. Loads data/investors.json (CIK list + metadata)
  2. For each investor: SEC submissions API -> latest 13F-HR -> information_table.xml
  3. Aggregates per-CUSIP, computes weights, sorts by value
  4. Resolves CUSIP -> ticker via OpenFIGI (cached in data/cusip_cache.json)
  5. Rewrites data/investors.json with holdings and last_updated timestamp

Notes:
  - Post-2023 filings report `value` in actual USD (not thousands). Older legacy filings used
    thousands; we stick with current convention since we always pull the latest filing.
  - 13F is US-listed long equity only (excludes shorts, cash, foreign, bonds).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
INV_PATH = ROOT / "data" / "investors.json"
CACHE_PATH = ROOT / "data" / "cusip_cache.json"

UA = os.environ.get("EDGAR_USER_AGENT") or "Whale Watch dev@example.com"
OPENFIGI_KEY = os.environ.get("OPENFIGI_API_KEY", "").strip()

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})

SEC_RATE_SLEEP = 0.15  # SEC asks for <=10 req/sec
OPENFIGI_BATCH = 100
OPENFIGI_SLEEP = 6.5 if not OPENFIGI_KEY else 0.3


# ───────────── EDGAR fetchers ─────────────

def get_latest_13f(cik: str) -> dict | None:
    cik_padded = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    sub = r.json()
    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    pds = recent.get("reportDate", [])
    primary = recent.get("primaryDocument", [])
    # Prefer most recent 13F-HR (or amendment)
    for i, f in enumerate(forms):
        if f in ("13F-HR", "13F-HR/A"):
            return {
                "accession": accs[i],
                "filing_date": dates[i],
                "period_of_report": pds[i],
                "primary_doc": primary[i],
                "cik_int": int(cik_padded),
                "form": f,
            }
    return None


def fetch_information_table_xml(filing: dict) -> bytes | None:
    cik_int = filing["cik_int"]
    acc_no = filing["accession"].replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no}/index.json"
    r = session.get(idx_url, timeout=30)
    r.raise_for_status()
    items = r.json().get("directory", {}).get("item", [])
    info_name = None
    for it in items:
        n = it.get("name", "")
        nl = n.lower()
        if nl.endswith(".xml") and "informationtable" in nl.replace("_", "").replace("-", ""):
            info_name = n
            break
    if not info_name:
        # Fallback: any .xml that isn't the primary doc
        for it in items:
            n = it.get("name", "")
            if n.lower().endswith(".xml") and n != filing["primary_doc"]:
                info_name = n
                break
    if not info_name:
        return None
    time.sleep(SEC_RATE_SLEEP)
    xml_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no}/{info_name}"
    r = session.get(xml_url, timeout=45)
    r.raise_for_status()
    return r.content


def parse_holdings(xml_bytes: bytes) -> list[dict]:
    if not xml_bytes:
        return []
    text = xml_bytes.decode("utf-8", errors="ignore")
    # Strip default xmlns so XPath doesn't need namespace gymnastics
    text = re.sub(r'\sxmlns="[^"]*"', "", text, count=1)
    root = ET.fromstring(text)
    out = []
    for it in root.findall(".//infoTable"):
        out.append({
            "name": (it.findtext("nameOfIssuer") or "").strip(),
            "class": (it.findtext("titleOfClass") or "").strip(),
            "cusip": (it.findtext("cusip") or "").strip().upper(),
            "value_usd": float(it.findtext("value") or 0),
            "shares": float(it.findtext("shrsOrPrnAmt/sshPrnamt") or 0),
            "sh_type": (it.findtext("shrsOrPrnAmt/sshPrnamtType") or "").strip(),
            "put_call": (it.findtext("putCall") or "").strip(),
        })
    return out


def aggregate(holdings: list[dict]) -> list[dict]:
    """Combine multiple line items per CUSIP. Skip option positions (PUT/CALL)."""
    bucket: dict[str, dict] = {}
    for h in holdings:
        if h["put_call"]:
            continue  # 13F also lists option exposures; skip for cleaner equity view
        k = h["cusip"] or h["name"]
        if k not in bucket:
            bucket[k] = {"cusip": h["cusip"], "name": h["name"], "value_usd": 0, "shares": 0}
        bucket[k]["value_usd"] += h["value_usd"]
        if h["sh_type"] == "SH":
            bucket[k]["shares"] += h["shares"]
    out = list(bucket.values())
    total = sum(x["value_usd"] for x in out) or 1
    for x in out:
        x["weight"] = round(x["value_usd"] / total * 100, 4)
        x["value_usd"] = round(x["value_usd"], 2)
        x["shares"] = int(x["shares"])
    out.sort(key=lambda x: -x["value_usd"])
    return out


# ───────────── CUSIP -> ticker (OpenFIGI) ─────────────

def load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(c: dict) -> None:
    CACHE_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def resolve_tickers(holdings: list[dict], cache: dict) -> list[dict]:
    todo = sorted({h["cusip"] for h in holdings if h["cusip"] and h["cusip"] not in cache})
    if todo:
        headers = {"Content-Type": "application/json"}
        if OPENFIGI_KEY:
            headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
        for i in range(0, len(todo), OPENFIGI_BATCH):
            batch = todo[i : i + OPENFIGI_BATCH]
            payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]
            try:
                r = session.post(
                    "https://api.openfigi.com/v3/mapping",
                    headers=headers,
                    data=json.dumps(payload),
                    timeout=45,
                )
                r.raise_for_status()
                rows = r.json()
            except Exception as e:
                print(f"    openfigi batch failed: {e}", file=sys.stderr)
                rows = [{} for _ in batch]
            for cusip, row in zip(batch, rows):
                ticker = ""
                data = row.get("data") if isinstance(row, dict) else None
                if data:
                    # Prefer US-listed common equity
                    for d in data:
                        if d.get("marketSector") == "Equity" and (d.get("exchCode") or "").startswith("U"):
                            ticker = d.get("ticker") or ""
                            if ticker:
                                break
                    if not ticker:
                        ticker = data[0].get("ticker") or ""
                cache[cusip] = ticker
            time.sleep(OPENFIGI_SLEEP)
        save_cache(cache)
    for h in holdings:
        h["ticker"] = cache.get(h["cusip"], "") or ""
    return holdings


# ───────────── main ─────────────

def main() -> int:
    if "@" not in UA:
        print(
            "warning: EDGAR_USER_AGENT should include a contact email. SEC may block requests without one.",
            file=sys.stderr,
        )

    inv_data = json.loads(INV_PATH.read_text(encoding="utf-8"))
    cache = load_cache()
    failed: list[str] = []

    for inv in inv_data["investors"]:
        print(f"→ {inv['name']} ({inv['fund']}) — CIK {inv['cik']}")
        try:
            filing = get_latest_13f(inv["cik"])
            if not filing:
                print("    no 13F-HR found — skipping")
                failed.append(inv["id"])
                time.sleep(SEC_RATE_SLEEP)
                continue
            print(f"    {filing['form']}  acc={filing['accession']}  period={filing['period_of_report']}")
            time.sleep(SEC_RATE_SLEEP)
            xml = fetch_information_table_xml(filing)
            raw = parse_holdings(xml)
            holdings = aggregate(raw)
            holdings = resolve_tickers(holdings, cache)

            inv["filing_date"] = filing["filing_date"]
            inv["period_of_report"] = filing["period_of_report"]
            inv["accession_number"] = filing["accession"]
            inv["holdings_count"] = len(holdings)
            inv["total_value_usd"] = round(sum(h["value_usd"] for h in holdings), 2)
            inv["holdings"] = holdings
            print(f"    OK  {len(holdings)} holdings  ${inv['total_value_usd']:,.0f}")
        except requests.HTTPError as e:
            print(f"    HTTP error: {e}", file=sys.stderr)
            failed.append(inv["id"])
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            failed.append(inv["id"])
        time.sleep(SEC_RATE_SLEEP)

    inv_data["last_updated"] = datetime.now(timezone.utc).isoformat()
    INV_PATH.write_text(
        json.dumps(inv_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nwrote {INV_PATH.relative_to(ROOT)}")
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
