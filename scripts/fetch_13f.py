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


def fetch_filing_xmls(filing: dict) -> tuple[bytes | None, bytes | None]:
    """Returns (information_table_xml, primary_doc_xml) for the filing."""
    cik_int = filing["cik_int"]
    acc_no = filing["accession"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no}"
    r = session.get(f"{base}/index.json", timeout=30)
    r.raise_for_status()
    items = r.json().get("directory", {}).get("item", [])

    xml_files = [it["name"] for it in items if it.get("name", "").lower().endswith(".xml")]
    info_name = None
    primary_name = None
    for n in xml_files:
        nl = n.lower()
        flat = nl.replace("_", "").replace("-", "")
        if "primarydoc" in flat or nl == "primary_doc.xml":
            primary_name = n
        elif "informationtable" in flat:
            info_name = n
    # Fallback: info table = first .xml that isn't primary
    if not info_name:
        for n in xml_files:
            if n != primary_name:
                info_name = n
                break
    info_xml = None
    primary_xml = None
    if info_name:
        time.sleep(SEC_RATE_SLEEP)
        r = session.get(f"{base}/{info_name}", timeout=45)
        r.raise_for_status()
        info_xml = r.content
    if primary_name:
        time.sleep(SEC_RATE_SLEEP)
        r = session.get(f"{base}/{primary_name}", timeout=30)
        if r.ok:
            primary_xml = r.content
    return info_xml, primary_xml


def _strip_namespaces(xml_text: str) -> str:
    """Some filers use a default xmlns, others a prefixed one (ns1:infoTable).
    Strip both so ElementTree XPath doesn't need a namespace map."""
    xml_text = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", xml_text)
    xml_text = re.sub(r"<(/?)\w+:", r"<\1", xml_text)
    return xml_text


def parse_holdings(xml_bytes: bytes) -> list[dict]:
    if not xml_bytes:
        return []
    text = _strip_namespaces(xml_bytes.decode("utf-8", errors="ignore"))
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


def parse_summary(primary_doc_xml: bytes) -> dict:
    """Read tableValueTotal and tableEntryTotal from the cover-page primary_doc.xml.
    SEC instructions require dollars as of Q4 2022, but some filers still report in
    thousands. We use the cover page totals to detect this and scale individual
    values to actual dollars."""
    if not primary_doc_xml:
        return {}
    text = _strip_namespaces(primary_doc_xml.decode("utf-8", errors="ignore"))
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    def f(t):
        n = root.find(f".//{t}")
        return n.text.strip() if n is not None and n.text else ""
    out = {}
    if (v := f("tableValueTotal")):
        try: out["table_value_total"] = float(v)
        except ValueError: pass
    if (v := f("tableEntryTotal")):
        try: out["table_entry_total"] = int(float(v))
        except ValueError: pass
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
            info_xml, primary_xml = fetch_filing_xmls(filing)
            raw = parse_holdings(info_xml)
            summary = parse_summary(primary_xml)

            # Detect filings still reported in thousands (some filers haven't updated to
            # the post-2022 dollars convention). 13F filing threshold is $100M, so any
            # multi-position filing with cover-page total under $50M is almost certainly
            # in thousands.
            tvt = summary.get("table_value_total")
            tet = summary.get("table_entry_total", 0)
            scale = 1
            if tvt is not None and tvt < 50_000_000 and tet >= 10:
                scale = 1000
                print(f"    note: filing reports values in thousands (tvt=${tvt:,.0f}, entries={tet}) — scaling x1000")
                for h in raw:
                    h["value_usd"] *= 1000

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
