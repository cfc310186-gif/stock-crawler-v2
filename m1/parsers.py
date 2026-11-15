import json
import re
from decimal import Decimal, InvalidOperation

from lxml import html


def number(value):
    try:
        parsed = Decimal(str(value).replace(",", "").strip())
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def fubon(raw, mode):
    text = raw.decode("cp950", errors="strict")
    if "</html>" not in text.lower():
        raise ValueError("truncated document")
    doc = html.fromstring(text)
    dates = re.findall(r"資料日期[：:]\s*(\d{8})", doc.text_content())
    if len(set(dates)) != 1:
        raise ValueError("missing/ambiguous response date")
    unit = "仟元" if mode == "B" else "張"
    if f"單位：{unit}" not in doc.text_content():
        raise ValueError("unit mismatch")
    names = (
        ["買進金額", "賣出金額", "差額"]
        if mode == "B"
        else ["買進張數", "賣出張數", "差額"]
    )
    rows, anomalies = [], []
    for table in doc.xpath("//table"):
        headers = None
        for tr in table.xpath("./tr"):
            cells = tr.xpath("./td|./th")
            if any(c.xpath(".//table") for c in cells):
                continue
            labels = [c.text_content().strip() for c in cells]
            if all(n in labels for n in names):
                headers = [labels.index(n) for n in names]
                continue
            scripts = tr.xpath("./td//script/text()")
            match = re.search(
                r"(?:GenLink2stk|Link2Stk)\(['\"](?:AS)?([0-9A-Z]{4,6})['\"]\s*,\s*['\"]([^'\"]+)",
                "".join(scripts),
            )
            if match:
                code, name = match[1], match[2]
            else:
                links = tr.xpath('./td//a[contains(@href, "Link2Stk")]')
                if not links:
                    continue
                linked = re.search(
                    r"Link2Stk\(['\"]([0-9A-Z]{4,6})['\"]", links[0].get("href")
                )
                if not linked:
                    raise ValueError("invalid stock link")
                code = linked[1]
                name = links[0].text_content().removeprefix(code)
            if headers is None or len(cells) <= max(headers):
                raise ValueError("structure/header mismatch")
            raw_values = [cells[i].text_content().strip() for i in headers]
            values = [number(v) for v in raw_values]
            row = {
                "code": code,
                "name": name,
                "buy": values[0],
                "sell": values[1],
                "net": values[2],
                "raw": raw_values,
            }
            if None in values or values[0] - values[1] != values[2]:
                anomalies.append({"code": code, "reason": "missing or buy-sell != net"})
            rows.append(row)
    if not rows:
        raise ValueError("empty/invalid structure")
    return {"date": dates[0], "unit": unit, "rows": rows, "anomalies": anomalies}


def monthly(raw, code, market):
    obj = json.loads(raw)
    table = obj if market == "TWSE" else obj["tables"][0]
    fields = [s.replace(" ", "") for s in table["fields"]]
    ci = fields.index("收盤價" if market == "TWSE" else "收盤")
    vi = fields.index("成交股數" if market == "TWSE" else "成交張數")
    result = {}
    for row in table["data"]:
        y, m, d = map(int, row[0].split("/"))
        date = f"{y + 1911:04}-{m:02}-{d:02}"
        volume = number(row[vi])
        result[date] = {
            "code": code,
            "close": number(row[ci]),
            "volume_shares": volume * (1000 if market == "TPEx" else 1)
            if volume is not None
            else None,
            "price_basis": "unadjusted",
        }
    return result


def daily(raw, market, target):
    """Official OpenAPI daily format: reject latest data used as history."""
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty daily response")
    result = {}
    for row in rows:
        date = row.get("Date", row.get("日期", ""))
        digits = str(date).replace("/", "").replace("-", "")
        if len(digits) == 7:
            digits = str(int(digits[:3]) + 1911) + digits[3:]
        if digits != target.replace("-", ""):
            raise ValueError("daily response date mismatch")
        code = row.get("Code", row.get("SecuritiesCompanyCode", row.get("代號")))
        result[code] = {
            "code": code,
            "close": number(
                row.get("ClosingPrice", row.get("Close", row.get("收盤價")))
            ),
            "volume_shares": number(
                row.get("TradeVolume", row.get("TradingShares", row.get("成交股數")))
            ),
            "price_basis": "unadjusted",
        }
    return result
