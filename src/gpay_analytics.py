from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median, pstdev

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

try:
    from pypdf import PdfReader
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal system Python installs
    PdfReader = None


DATE_LINE = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9}),?$")
YEAR_LINE = re.compile(r"^\d{4}$")
AMOUNT_LINE = re.compile(r"^₹\s*([\d,]+(?:\.\d*)?)\s*$")
DATE_FORMATS = ("%d %b %Y", "%d %B %Y")


@dataclass(frozen=True)
class Transaction:
    date: str
    description: str
    amount: float
    category: str


def newest_pdf(folder: Path) -> Path:
    pdfs = [item for item in folder.glob("*.pdf") if item.is_file()]
    if not pdfs:
        raise FileNotFoundError(f"No PDF files found in {folder}")
    return max(pdfs, key=lambda item: item.stat().st_mtime)


def extract_pdf_text(pdf_path: Path) -> str:
    if PdfReader is not None:
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    try:
        result = subprocess.run(["pdftotext", "-raw", str(pdf_path), "-"], check=True, capture_output=True, text=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Install pypdf or pdftotext to read PDF statements") from error
    return result.stdout


def classify(description: str, categories: dict[str, list[str]]) -> str:
    lowered = description.casefold()
    for category, patterns in categories.items():
        if any(pattern.casefold() in lowered for pattern in patterns):
            return category
    return "Uncategorized"


def parse_transactions(text: str, categories: dict[str, list[str]], credit_keywords: list[str] | None = None) -> list[Transaction]:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    transactions: list[Transaction] = []
    current_date: str | None = None
    index = 0
    credit_keywords = [word.casefold() for word in (credit_keywords or [])]
    while index < len(lines):
        date_match = DATE_LINE.match(lines[index])
        if date_match and index + 1 < len(lines) and YEAR_LINE.match(lines[index + 1]):
            current_date = datetime.strptime(f"{date_match.group(1)} {date_match.group(2)} {lines[index + 1]}", "%d %b %Y").date().isoformat()
            index += 2
            continue
        if current_date and (lines[index].startswith("Paid to ") or lines[index].startswith("Received from ")):
            description = re.sub(r"^(?:Paid to|Received from)\s+", "", lines[index]).strip()
            amount = None
            lookahead = index + 1
            while lookahead < len(lines) and lookahead < index + 8:
                amount_match = AMOUNT_LINE.match(lines[lookahead])
                if amount_match:
                    number = amount_match.group(1)
                    # PDF layout can split the final digit of an amount onto its own line.
                    if lookahead + 1 < len(lines) and re.fullmatch(r"\d", lines[lookahead + 1]):
                        number += lines[lookahead + 1]
                    amount = float(number.replace(",", ""))
                    break
                lookahead += 1
            if amount is not None:
                signed_amount = -amount if any(word in description.casefold() for word in credit_keywords) or lines[index].startswith("Received from ") else amount
                transactions.append(Transaction(current_date, description, signed_amount, classify(description, categories)))
                index = lookahead + 1
                continue
        index += 1
    return transactions


def analyze(transactions: list[Transaction]) -> dict:
    expenses = [item for item in transactions if item.amount > 0]
    credits = [item for item in transactions if item.amount < 0]
    categories: dict[str, float] = defaultdict(float)
    merchants: dict[str, float] = defaultdict(float)
    months: dict[str, float] = defaultdict(float)
    weekdays: dict[str, float] = defaultdict(float)
    daily_totals: dict[str, float] = defaultdict(float)
    for item in expenses:
        transaction_date = date.fromisoformat(item.date)
        categories[item.category] += item.amount
        merchants[item.description] += item.amount
        months[item.date[:7]] += item.amount
        weekdays[transaction_date.strftime("%A")] += item.amount
        daily_totals[item.date] += item.amount
    total = sum(item.amount for item in expenses)
    amounts = sorted(item.amount for item in expenses)
    active_days = len(daily_totals)
    top_ten_total = sum(amounts[-10:])
    statistics = {
        "median_expense": round(median(amounts), 2) if amounts else 0,
        "p90_expense": round(amounts[max(0, int(len(amounts) * 0.9) - 1)], 2) if amounts else 0,
        "smallest_expense": round(amounts[0], 2) if amounts else 0,
        "largest_expense": round(amounts[-1], 2) if amounts else 0,
        "expense_stddev": round(pstdev(amounts), 2) if len(amounts) > 1 else 0,
        "active_days": active_days,
        "daily_average": round(total / active_days, 2) if active_days else 0,
        "top_ten_share": round(top_ten_total / total, 4) if total else 0,
    }
    return {
        "transaction_count": len(transactions),
        "expense_count": len(expenses),
        "credit_count": len(credits),
        "total_expenses": round(total, 2),
        "total_credits": round(-sum(item.amount for item in credits), 2),
        "net_spend": round(sum(item.amount for item in transactions), 2),
        "average_expense": round(total / len(expenses), 2) if expenses else 0,
        "statistics": statistics,
        "category_totals": dict(sorted(categories.items(), key=lambda pair: pair[1], reverse=True)),
        "merchant_totals": dict(sorted(merchants.items(), key=lambda pair: pair[1], reverse=True)[:20]),
        "monthly_totals": dict(sorted(months.items())),
        "weekday_totals": dict(sorted(weekdays.items(), key=lambda pair: pair[1], reverse=True)),
        "largest_expenses": [asdict(item) for item in sorted(expenses, key=lambda item: item.amount, reverse=True)],
        "uncategorized": [asdict(item) for item in sorted(expenses, key=lambda item: item.amount, reverse=True) if item.category == "Uncategorized"],
    }


def load_config(path: Path) -> tuple[dict[str, list[str]], list[str]]:
    if not path.exists():
        return {}, []
    with path.open("rb") as file:
        config = tomllib.load(file)
    return config.get("categories", {}), config.get("parser", {}).get("credit_keywords", [])


def markdown_report(pdf_path: Path, report: dict) -> str:
    total = report["total_expenses"] or 1
    stats = report["statistics"]
    lines = [f"# Expense report: {pdf_path.name}", "", "## Overview", "", f"- Transactions: {report['transaction_count']}", f"- Expenses: {report['expense_count']}", f"- Total expenses: ₹{report['total_expenses']:,.2f}", f"- Credits/refunds: ₹{report['total_credits']:,.2f}", f"- Net spend: ₹{report['net_spend']:,.2f}", f"- Average expense: ₹{report['average_expense']:,.2f}", "", "## Statistics", "", f"- Median expense: ₹{stats['median_expense']:,.2f}", f"- 90th percentile expense: ₹{stats['p90_expense']:,.2f}", f"- Daily average on active days: ₹{stats['daily_average']:,.2f}", f"- Active spending days: {stats['active_days']}", f"- Expense standard deviation: ₹{stats['expense_stddev']:,.2f}", f"- Top 10 expense share: {stats['top_ten_share']:.1%}", "", "## By category", "", "| Category | Amount | Share |", "| --- | ---: | ---: |"]
    lines.extend(f"| {category} | ₹{amount:,.2f} | {amount / total:.1%} |" for category, amount in report["category_totals"].items())
    lines.extend(["", "## Monthly totals", "", "| Month | Amount |", "| --- | ---: |"])
    lines.extend(f"| {month} | ₹{amount:,.2f} |" for month, amount in report["monthly_totals"].items())
    lines.extend(["", "## Largest expenses", "", "| Date | Description | Category | Amount |", "| --- | --- | --- | ---: |"])
    lines.extend(f"| {item['date']} | {item['description']} | {item['category']} | ₹{item['amount']:,.2f} |" for item in report["largest_expenses"])
    if report["uncategorized"]:
        lines.extend(["", "## Uncategorized", "", "Add patterns for these descriptions to `config.toml`:", ""])
        lines.extend(f"- {item['description']} (₹{item['amount']:,.2f})" for item in report["uncategorized"])
    return "\n".join(lines) + "\n"


def dashboard_html(pdf_path: Path, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google Pay dashboard</title>
<style>
:root {{ --ink:#17231f; --muted:#6c7973; --paper:#f5f2ea; --panel:#fffdf8; --line:#dedbd1; --green:#0f6b52; --mint:#d6eee2; --orange:#df704d; --blue:#4f7cac; --yellow:#d6a43c; }}
* {{ box-sizing:border-box; }} body {{ margin:0; color:var(--ink); background:var(--paper); font:15px/1.5 Georgia,serif; }}
.wrap {{ max-width:1280px; margin:auto; padding:42px 28px 64px; }} header {{ display:flex; justify-content:space-between; gap:24px; align-items:end; margin-bottom:34px; }}
h1,h2,p {{ margin:0; }} h1 {{ font:700 clamp(2.1rem,5vw,4.8rem)/.95 Georgia,serif; letter-spacing:-.04em; max-width:700px; }}
.kicker {{ color:var(--green); font:700 12px/1.2 ui-monospace,monospace; letter-spacing:.14em; text-transform:uppercase; margin-bottom:12px; }}
.source {{ color:var(--muted); text-align:right; font:12px ui-monospace,monospace; }} .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:18px; }}
.card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }} .card {{ padding:20px; min-height:118px; }} .card strong {{ display:block; font:700 clamp(1.35rem,2vw,2rem) Georgia,serif; margin-top:16px; }}
.card span,.subtle {{ color:var(--muted); font:12px ui-monospace,monospace; }} .grid {{ display:grid; grid-template-columns:1.1fr .9fr; gap:18px; margin-bottom:18px; }} .panel {{ padding:24px; }}
h2 {{ font-size:1.3rem; margin-bottom:20px; }} .bars {{ display:grid; gap:13px; }} .bar-row {{ display:grid; grid-template-columns:130px 1fr 90px; gap:12px; align-items:center; }} .bar-label {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.track {{ height:11px; background:#e8e5dc; border-radius:20px; overflow:hidden; }} .fill {{ height:100%; background:var(--green); border-radius:inherit; }} .amount {{ text-align:right; font:12px ui-monospace,monospace; }}
.donut-wrap {{ display:flex; align-items:center; gap:28px; }} .donut {{ width:190px; aspect-ratio:1; border-radius:50%; background:conic-gradient(var(--green) 0 25%,var(--orange) 25% 50%,var(--blue) 50% 75%,var(--yellow) 75% 100%); position:relative; flex:none; }} .donut:after {{ content:""; position:absolute; inset:28%; border-radius:50%; background:var(--panel); }} .legend {{ display:grid; gap:8px; width:100%; }} .legend-row {{ display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid var(--line); padding-bottom:7px; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:10px 6px; }} th:last-child,td:last-child {{ text-align:right; }} th {{ color:var(--muted); font:11px ui-monospace,monospace; text-transform:uppercase; letter-spacing:.08em; }} td:last-child {{ font-family:ui-monospace,monospace; }} .scroll {{ max-height:430px; overflow:auto; }}
.toolbar {{ display:flex; justify-content:space-between; gap:12px; align-items:center; margin:-8px 0 12px; }} .switch {{ display:flex; align-items:center; gap:9px; color:var(--muted); font:12px ui-monospace,monospace; cursor:pointer; }} .switch input {{ accent-color:var(--green); width:16px; height:16px; }} .stat-list {{ display:grid; grid-template-columns:1fr 1fr; gap:12px 24px; }} .stat {{ border-bottom:1px solid var(--line); padding-bottom:9px; }} .stat b {{ display:block; font:700 1.1rem Georgia,serif; margin-top:3px; }}
.note {{ background:var(--mint); border:0; }} .note strong {{ color:var(--green); }} .empty {{ color:var(--muted); }} @media(max-width:800px) {{ .wrap {{ padding:28px 16px 48px; }} header {{ display:block; }} .source {{ text-align:left; margin-top:16px; }} .cards,.grid {{ grid-template-columns:1fr 1fr; }} .grid {{ display:grid; }} .grid > .panel:first-child {{ grid-column:1/-1; }} .donut-wrap {{ display:block; }} .donut {{ margin:0 auto 22px; }} }} @media(max-width:520px) {{ .cards {{ grid-template-columns:1fr 1fr; }} .card {{ min-height:100px; padding:15px; }} .bar-row {{ grid-template-columns:100px 1fr 75px; gap:7px; font-size:13px; }} }}
</style></head><body><main class="wrap"><header><div><div class="kicker">Personal spending / latest statement</div><h1>Where the money went.</h1></div><div class="source">{pdf_path.name}<br>Generated locally</div></header>
<section class="cards" id="cards"></section><section class="grid"><article class="panel"><h2>Spend by category</h2><div class="donut-wrap"><div class="donut" id="donut"></div><div class="legend" id="legend"></div></div></article><article class="panel"><h2>Monthly spend</h2><div class="bars" id="months"></div></article></section>
<section class="grid"><article class="panel"><h2>Category totals</h2><div class="bars" id="categories"></div></article><article class="panel"><h2>Weekday rhythm</h2><div class="bars" id="weekdays"></div></article></section>
<section class="grid"><article class="panel"><h2>Largest expenses</h2><div class="scroll"><table><thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Amount</th></tr></thead><tbody id="largest"></tbody></table></div></article><article class="panel"><h2>Spending statistics</h2><div class="stat-list" id="stats"></div></article></section>
<section class="grid"><article class="panel note"><div class="toolbar"><h2>Needs categorizing</h2><label class="switch"><input id="group-toggle" type="checkbox"> Group merchants</label></div><p class="subtle">Review these rows and add patterns to config.toml.</p><div class="scroll"><table><thead><tr><th>Merchant</th><th>Count</th><th>Amount</th></tr></thead><tbody id="uncategorized"></tbody></table></div></article></section></main>
<script>
const data = {data}; const analysis = data.analysis; const money = value => '₹' + Number(value).toLocaleString('en-IN', {{minimumFractionDigits:2, maximumFractionDigits:2}});
const cards = [['Total spent',analysis.total_expenses],['Net spend',analysis.net_spend],['Credits / refunds',analysis.total_credits],['Average expense',analysis.average_expense]];
document.querySelector('#cards').innerHTML = cards.map(([label,value]) => `<div class="card"><span>${{label}}</span><strong>${{money(value)}}</strong></div>`).join('');
const colors=['#0f6b52','#df704d','#4f7cac','#d6a43c','#7b65a7','#bf5663','#4b8791','#8b7457']; const categoryEntries=Object.entries(analysis.category_totals); const total=analysis.total_expenses || 1; let cursor=0;
document.querySelector('#donut').style.background='conic-gradient('+categoryEntries.map(([name,value],i)=>{{const start=cursor; cursor += value/total*100; return `${{colors[i%colors.length]}} ${{start}}% ${{cursor}}%`;}}).join(',')+')';
document.querySelector('#legend').innerHTML=categoryEntries.map(([name,value],i)=>`<div class="legend-row"><span><b style="color:${{colors[i%colors.length]}}">●</b> ${{name}}</span><span>${{money(value)}}</span></div>`).join('');
function bars(target, entries) {{ const max=Math.max(...entries.map(([,value])=>value),1); document.querySelector(target).innerHTML=entries.map(([name,value])=>`<div class="bar-row"><span class="bar-label">${{name}}</span><div class="track"><div class="fill" style="width:${{value/max*100}}%"></div></div><span class="amount">${{money(value)}}</span></div>`).join(''); }}
bars('#categories',categoryEntries); bars('#months',Object.entries(analysis.monthly_totals)); bars('#weekdays',Object.entries(analysis.weekday_totals));
const stats = analysis.statistics; const statEntries = [['Median expense',money(stats.median_expense)],['90th percentile',money(stats.p90_expense)],['Typical daily spend',money(stats.daily_average)],['Active days',stats.active_days],['Largest purchase',money(stats.largest_expense)],['Top 10 share',(stats.top_ten_share * 100).toFixed(1) + '%']];
document.querySelector('#stats').innerHTML=statEntries.map(([label,value])=>`<div class="stat"><span class="subtle">${{label}}</span><b>${{value}}</b></div>`).join('');
document.querySelector('#largest').innerHTML=analysis.largest_expenses.map(item=>`<tr><td>${{item.date}}</td><td>${{item.description}}</td><td>${{item.category}}</td><td>${{money(item.amount)}}</td></tr>`).join('');
function uncategorizedRows(grouped) {{
    const rows = grouped ? Object.values(analysis.uncategorized.reduce((groups,item)=>{{ const key=item.description.toLocaleLowerCase(); const existing=groups[key] || {{description:item.description,amount:0,count:0}}; existing.amount += item.amount; existing.count += 1; groups[key]=existing; return groups; }}, {{}})).sort((a,b)=>b.amount-a.amount) : analysis.uncategorized;
    document.querySelector('#uncategorized').innerHTML = rows.length ? rows.map(item=>`<tr><td>${{item.description}}</td><td>${{item.count || 1}}</td><td>${{money(item.amount)}}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">Everything is categorized.</td></tr>';
}}
uncategorizedRows(false); document.querySelector('#group-toggle').addEventListener('change', event=>uncategorizedRows(event.target.checked));
</script></body></html>'''


def run(folder: Path, config_path: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    pdf_path = newest_pdf(folder)
    categories, credit_keywords = load_config(config_path)
    transactions = parse_transactions(extract_pdf_text(pdf_path), categories, credit_keywords)
    if not transactions:
        raise ValueError("No transactions detected in the newest PDF")
    report = analyze(transactions)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{pdf_path.stem}.json"
    markdown_path = output_dir / f"{pdf_path.stem}.md"
    dashboard_path = output_dir / f"{pdf_path.stem}.html"
    payload = {"source": pdf_path.name, "transactions": [asdict(item) for item in transactions], "analysis": report}
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown_path.write_text(markdown_report(pdf_path, report), encoding="utf-8")
    dashboard_path.write_text(dashboard_html(pdf_path, payload), encoding="utf-8")
    return markdown_path, json_path, dashboard_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze the newest Google Pay statement PDF")
    parser.add_argument("--folder", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        markdown_path, json_path, dashboard_path = run(args.folder, args.config or args.folder / "config.toml", args.output or args.folder / "reports")
    except (FileNotFoundError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Report written to {markdown_path}\nData written to {json_path}\nDashboard written to {dashboard_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())