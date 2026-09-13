from __future__ import annotations

import argparse
import html
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


DATE_LINE = re.compile(r"^(\d{1,2})\s*([A-Za-z]{3,9}),?$")
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
        transaction_match = re.match(r"^(Paid\s*to|Received\s*from)\s*(.+)$", lines[index]) if current_date else None
        if transaction_match:
            description = transaction_match.group(2).strip()
            amount = None
            lookahead = index + 1
            while lookahead < len(lines) and lookahead < index + 8:
                amount_match = AMOUNT_LINE.match(lines[lookahead])
                if amount_match:
                    number = amount_match.group(1)
                    # PDF layout can split the final digit of an amount onto its own line.
                    if lookahead + 1 < len(lines) and re.fullmatch(r"\d+", lines[lookahead + 1]):
                        number += lines[lookahead + 1]
                    amount = float(number.replace(",", ""))
                    break
                lookahead += 1
            if amount is not None:
                signed_amount = -amount if any(word in description.casefold() for word in credit_keywords) or transaction_match.group(1).startswith("Received") else amount
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


def upload_page_html(error: str | None = None) -> str:
    error_markup = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return f'''<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Google Pay Analytics</title>
<style>
:root {{ --ink:#17231f; --muted:#6c7973; --paper:#f5f2ea; --panel:#fffdf8; --line:#dedbd1; --green:#0f6b52; --mint:#d6eee2; }}
* {{ box-sizing:border-box; }} body {{ margin:0; color:var(--ink); background:var(--paper); font:16px/1.5 Georgia,serif; }}
main {{ max-width:720px; margin:0 auto; padding:12vh 24px; }} .kicker {{ color:var(--green); font:700 12px/1.2 ui-monospace,monospace; letter-spacing:.14em; text-transform:uppercase; }}
h1 {{ max-width:600px; margin:14px 0; font-size:clamp(2.8rem,8vw,5.8rem); line-height:.95; letter-spacing:-.04em; }} .intro {{ max-width:520px; color:var(--muted); font-size:1.1rem; }}
form {{ margin-top:34px; padding:24px; background:var(--panel); border:1px solid var(--line); border-radius:8px; }} input[type=file] {{ display:block; width:100%; padding:14px; border:1px dashed var(--green); background:#faf8f1; font:14px ui-monospace,monospace; }}
button {{ margin-top:16px; padding:12px 18px; border:0; border-radius:5px; color:white; background:var(--green); font:700 14px ui-monospace,monospace; cursor:pointer; }} button:hover {{ background:#0a503e; }}
.note {{ margin-top:16px; padding:14px 16px; background:var(--mint); color:var(--green); font-size:14px; }} .error {{ margin-top:20px; padding:14px 16px; color:#8d2f25; background:#f8ddd4; border:1px solid #e9b9aa; }}
</style></head><body><main><div class="kicker">Local Google Pay analysis</div><h1>See where the money went.</h1>
<p class="intro">Drop in a Google Pay statement PDF and get a small, readable spending report. Nothing leaves this machine.</p>
<form method="post" enctype="multipart/form-data"><input type="file" name="statement" accept="application/pdf,.pdf" required><button type="submit">Analyze statement</button></form>
<div class="note">Categories come from <strong>config.toml</strong>. Add merchant patterns there before analyzing.</div>{error_markup}</main></body></html>'''


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
.date-filter {{ display:flex; align-items:end; gap:14px; margin-bottom:18px; padding:16px 20px; background:var(--panel); border:1px solid var(--line); border-radius:8px; }} .date-filter label {{ display:grid; gap:5px; color:var(--muted); font:11px ui-monospace,monospace; text-transform:uppercase; letter-spacing:.08em; }} .date-filter input {{ min-height:38px; padding:7px 10px; color:var(--ink); background:#faf8f1; border:1px solid var(--line); border-radius:5px; font:14px ui-monospace,monospace; }} .date-filter button {{ min-height:38px; padding:7px 13px; border:1px solid var(--green); border-radius:5px; color:var(--green); background:transparent; font:700 12px ui-monospace,monospace; cursor:pointer; }} .date-filter button:hover {{ color:white; background:var(--green); }} .range-summary {{ margin-left:auto; align-self:center; color:var(--muted); font:12px ui-monospace,monospace; }}
.card,.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }} .card {{ padding:20px; min-height:118px; }} .card strong {{ display:block; font:700 clamp(1.35rem,2vw,2rem) Georgia,serif; margin-top:16px; }}
.card span,.subtle {{ color:var(--muted); font:12px ui-monospace,monospace; }} .grid {{ display:grid; grid-template-columns:1.1fr .9fr; gap:18px; margin-bottom:18px; }} .panel {{ padding:24px; }}
h2 {{ font-size:1.3rem; margin-bottom:20px; }} .bars {{ display:grid; gap:13px; }} .bar-row {{ display:grid; grid-template-columns:130px 1fr 90px; gap:12px; align-items:center; }} .bar-label {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.track {{ height:11px; background:#e8e5dc; border-radius:20px; overflow:hidden; }} .fill {{ height:100%; background:var(--green); border-radius:inherit; }} .amount {{ text-align:right; font:12px ui-monospace,monospace; }}
.donut-wrap {{ display:flex; align-items:center; gap:28px; }} .donut {{ width:190px; aspect-ratio:1; border-radius:50%; background:conic-gradient(var(--green) 0 25%,var(--orange) 25% 50%,var(--blue) 50% 75%,var(--yellow) 75% 100%); position:relative; flex:none; cursor:crosshair; }} .donut:after {{ content:""; position:absolute; inset:28%; border-radius:50%; background:var(--panel); pointer-events:none; }} .donut-tooltip {{ display:none; position:absolute; left:50%; top:50%; z-index:1; transform:translate(-50%,-50%); width:132px; padding:9px 10px; border:1px solid var(--line); border-radius:5px; background:var(--panel); box-shadow:0 4px 14px #17231f22; text-align:center; font:12px/1.35 ui-monospace,monospace; pointer-events:none; }} .donut-tooltip strong {{ display:block; margin-bottom:3px; font:700 13px Georgia,serif; }} .donut.is-hovering .donut-tooltip {{ display:block; }} .legend {{ display:grid; gap:8px; width:100%; }} .legend-row {{ display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid var(--line); padding-bottom:7px; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ text-align:left; border-bottom:1px solid var(--line); padding:10px 6px; }} th:last-child,td:last-child {{ text-align:right; }} th {{ color:var(--muted); font:11px ui-monospace,monospace; text-transform:uppercase; letter-spacing:.08em; }} td:last-child {{ font-family:ui-monospace,monospace; }} .scroll {{ max-height:430px; overflow:auto; }}
.toolbar {{ display:flex; justify-content:space-between; gap:12px; align-items:center; margin:-8px 0 12px; }} .switch {{ display:flex; align-items:center; gap:9px; color:var(--muted); font:12px ui-monospace,monospace; cursor:pointer; }} .switch input {{ accent-color:var(--green); width:16px; height:16px; }} .stat-list {{ display:grid; grid-template-columns:1fr 1fr; gap:12px 24px; }} .stat {{ border-bottom:1px solid var(--line); padding-bottom:9px; }} .stat b {{ display:block; font:700 1.1rem Georgia,serif; margin-top:3px; }}
.note {{ background:var(--mint); border:0; }} .note strong {{ color:var(--green); }} .empty {{ color:var(--muted); }} @media(max-width:800px) {{ .wrap {{ padding:28px 16px 48px; }} header {{ display:block; }} .source {{ text-align:left; margin-top:16px; }} .cards,.grid {{ grid-template-columns:1fr 1fr; }} .grid {{ display:grid; }} .grid > .panel:first-child {{ grid-column:1/-1; }} .donut-wrap {{ display:block; }} .donut {{ margin:0 auto 22px; }} .date-filter {{ flex-wrap:wrap; }} .range-summary {{ width:100%; margin-left:0; }} }} @media(max-width:520px) {{ .cards {{ grid-template-columns:1fr 1fr; }} .card {{ min-height:100px; padding:15px; }} .bar-row {{ grid-template-columns:100px 1fr 75px; gap:7px; font-size:13px; }} .date-filter label {{ flex:1; }} .date-filter input {{ width:100%; }} }}
</style></head><body><main class="wrap"><header><div><div class="kicker">Personal spending / latest statement</div><h1>Where the money went.</h1></div><div class="source">{pdf_path.name}<br>Generated locally</div></header>
<section class="date-filter" aria-label="Date range"><label>From<input id="date-from" type="date"></label><label>To<input id="date-to" type="date"></label><button id="all-time" type="button">All time</button><span class="range-summary" id="range-summary"></span></section>
<section class="cards" id="cards"></section><section class="grid"><article class="panel"><h2>Spend by category</h2><div class="donut-wrap"><div class="donut" id="donut"><div class="donut-tooltip" id="donut-tooltip"></div></div><div class="legend" id="legend"></div></div></article><article class="panel"><h2>Monthly spend</h2><div class="bars" id="months"></div></article></section>
<section class="grid"><article class="panel"><h2>Category totals</h2><div class="bars" id="categories"></div></article><article class="panel"><h2>Weekday rhythm</h2><div class="bars" id="weekdays"></div></article></section>
<section class="grid"><article class="panel"><h2>Largest expenses</h2><div class="scroll"><table><thead><tr><th>Date</th><th>Merchant</th><th>Category</th><th>Amount</th></tr></thead><tbody id="largest"></tbody></table></div></article><article class="panel"><h2>Spending statistics</h2><div class="stat-list" id="stats"></div></article></section>
<section class="grid"><article class="panel note"><div class="toolbar"><h2>Needs categorizing</h2><label class="switch"><input id="group-toggle" type="checkbox"> Group merchants</label></div><p class="subtle">Review these rows and add patterns to config.toml.</p><div class="scroll"><table><thead><tr><th>Merchant</th><th>Count</th><th>Amount</th></tr></thead><tbody id="uncategorized"></tbody></table></div></article></section></main>
<script>
const data = {data}; const money = value => '₹' + Number(value).toLocaleString('en-IN', {{minimumFractionDigits:2, maximumFractionDigits:2}});
const colors=['#0f6b52','#df704d','#4f7cac','#d6a43c','#7b65a7','#bf5663','#4b8791','#8b7457'];
const donut = document.querySelector('#donut'); const tooltip = document.querySelector('#donut-tooltip'); let segments=[]; let donutTotal=1; let analysis=data.analysis;
const fromInput=document.querySelector('#date-from'); const toInput=document.querySelector('#date-to'); const dates=data.transactions.map(item=>item.date).sort(); const firstDate=dates[0] || ''; const lastDate=dates[dates.length-1] || '';
fromInput.min=toInput.min=firstDate; fromInput.max=toInput.max=lastDate; fromInput.value=firstDate; toInput.value=lastDate;

function calculateAnalysis(transactions) {{
    const expenses=transactions.filter(item=>item.amount>0); const credits=transactions.filter(item=>item.amount<0); const categoryTotals={{}}; const monthlyTotals={{}}; const weekdayTotals={{}}; const dailyTotals={{}};
    expenses.forEach(item=>{{ categoryTotals[item.category]=(categoryTotals[item.category] || 0)+item.amount; monthlyTotals[item.date.slice(0,7)]=(monthlyTotals[item.date.slice(0,7)] || 0)+item.amount; const weekday=new Date(item.date+'T00:00:00').toLocaleDateString('en-US',{{weekday:'long'}}); weekdayTotals[weekday]=(weekdayTotals[weekday] || 0)+item.amount; dailyTotals[item.date]=(dailyTotals[item.date] || 0)+item.amount; }});
    const amounts=expenses.map(item=>item.amount).sort((a,b)=>a-b); const total=amounts.reduce((sum,value)=>sum+value,0); const activeDays=Object.keys(dailyTotals).length; const middle=Math.floor(amounts.length/2); const median=amounts.length ? (amounts.length%2 ? amounts[middle] : (amounts[middle-1]+amounts[middle])/2) : 0; const topTen=amounts.slice(-10).reduce((sum,value)=>sum+value,0);
    const sortTotals=totals=>Object.fromEntries(Object.entries(totals).sort((a,b)=>b[1]-a[1]));
    return {{ transaction_count:transactions.length, total_expenses:total, total_credits:-credits.reduce((sum,item)=>sum+item.amount,0), net_spend:transactions.reduce((sum,item)=>sum+item.amount,0), average_expense:expenses.length ? total/expenses.length : 0, category_totals:sortTotals(categoryTotals), monthly_totals:Object.fromEntries(Object.entries(monthlyTotals).sort()), weekday_totals:sortTotals(weekdayTotals), largest_expenses:[...expenses].sort((a,b)=>b.amount-a.amount), uncategorized:expenses.filter(item=>item.category==='Uncategorized').sort((a,b)=>b.amount-a.amount), statistics:{{ median_expense:median, p90_expense:amounts.length ? amounts[Math.max(0,Math.floor(amounts.length*.9)-1)] : 0, daily_average:activeDays ? total/activeDays : 0, active_days:activeDays, largest_expense:amounts.at(-1) || 0, top_ten_share:total ? topTen/total : 0 }} }};
}}
function bars(target, entries) {{ const max=Math.max(...entries.map(([,value])=>value),1); document.querySelector(target).innerHTML=entries.length ? entries.map(([name,value])=>`<div class="bar-row"><span class="bar-label">${{name}}</span><div class="track"><div class="fill" style="width:${{value/max*100}}%"></div></div><span class="amount">${{money(value)}}</span></div>`).join('') : '<p class="empty">No spending in this date range.</p>'; }}
function uncategorizedRows(grouped) {{
    const rows = grouped ? Object.values(analysis.uncategorized.reduce((groups,item)=>{{ const key=item.description.toLocaleLowerCase(); const existing=groups[key] || {{description:item.description,amount:0,count:0}}; existing.amount += item.amount; existing.count += 1; groups[key]=existing; return groups; }}, {{}})).sort((a,b)=>b.amount-a.amount) : analysis.uncategorized;
    document.querySelector('#uncategorized').innerHTML = rows.length ? rows.map(item=>`<tr><td>${{item.description}}</td><td>${{item.count || 1}}</td><td>${{money(item.amount)}}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">Everything is categorized.</td></tr>';
}}
function render() {{
    const from=fromInput.value; const to=toInput.value; const filtered=data.transactions.filter(item=>(!from || item.date>=from) && (!to || item.date<=to)); analysis=calculateAnalysis(filtered);
    const cards=[['Total spent',analysis.total_expenses],['Net spend',analysis.net_spend],['Credits / refunds',analysis.total_credits],['Average expense',analysis.average_expense]]; document.querySelector('#cards').innerHTML=cards.map(([label,value])=>`<div class="card"><span>${{label}}</span><strong>${{money(value)}}</strong></div>`).join('');
    const categoryEntries=Object.entries(analysis.category_totals); donutTotal=analysis.total_expenses || 1; let cursor=0; donut.style.background=categoryEntries.length ? 'conic-gradient('+categoryEntries.map(([name,value],i)=>{{const start=cursor; cursor+=value/donutTotal*100; return `${{colors[i%colors.length]}} ${{start}}% ${{cursor}}%`;}}).join(',')+')' : '#e8e5dc'; document.querySelector('#legend').innerHTML=categoryEntries.length ? categoryEntries.map(([name,value],i)=>`<div class="legend-row"><span><b style="color:${{colors[i%colors.length]}}">●</b> ${{name}}</span><span>${{money(value)}}</span></div>`).join('') : '<p class="empty">No spending in this date range.</p>';
    let segmentStart=0; segments=categoryEntries.map(([name,value])=>{{const segment={{name,value,start:segmentStart,end:segmentStart+value/donutTotal*360}}; segmentStart=segment.end; return segment;}}); bars('#categories',categoryEntries); bars('#months',Object.entries(analysis.monthly_totals)); bars('#weekdays',Object.entries(analysis.weekday_totals));
    const stats=analysis.statistics; const statEntries=[['Median expense',money(stats.median_expense)],['90th percentile',money(stats.p90_expense)],['Typical daily spend',money(stats.daily_average)],['Active days',stats.active_days],['Largest purchase',money(stats.largest_expense)],['Top 10 share',(stats.top_ten_share*100).toFixed(1)+'%']]; document.querySelector('#stats').innerHTML=statEntries.map(([label,value])=>`<div class="stat"><span class="subtle">${{label}}</span><b>${{value}}</b></div>`).join('');
    document.querySelector('#largest').innerHTML=analysis.largest_expenses.length ? analysis.largest_expenses.map(item=>`<tr><td>${{item.date}}</td><td>${{item.description}}</td><td>${{item.category}}</td><td>${{money(item.amount)}}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">No expenses in this date range.</td></tr>'; uncategorizedRows(document.querySelector('#group-toggle').checked); document.querySelector('#range-summary').textContent=`${{analysis.transaction_count}} transaction${{analysis.transaction_count===1?'':'s'}} · ${{from || 'Beginning'}} to ${{to || 'Today'}}`;
}}
donut.addEventListener('mousemove', event=>{{const bounds=donut.getBoundingClientRect(); const x=event.clientX-(bounds.left+bounds.width/2); const y=event.clientY-(bounds.top+bounds.height/2); const radius=Math.hypot(x,y); if(radius<bounds.width*.28){{donut.classList.remove('is-hovering');return;}} let angle=Math.atan2(y,x)*180/Math.PI+90; if(angle<0)angle+=360; const segment=segments.find(item=>angle>=item.start&&angle<item.end); if(!segment){{donut.classList.remove('is-hovering');return;}} tooltip.innerHTML=`<strong>${{segment.name}}</strong>${{money(segment.value)}}<br>${{(segment.value/donutTotal*100).toFixed(1)}}% of expenses`; donut.classList.add('is-hovering');}}); donut.addEventListener('mouseleave',()=>donut.classList.remove('is-hovering'));
fromInput.addEventListener('change',()=>{{toInput.min=fromInput.value || firstDate; if(toInput.value && fromInput.value>toInput.value)toInput.value=fromInput.value; render();}}); toInput.addEventListener('change',()=>{{fromInput.max=toInput.value || lastDate; if(fromInput.value && toInput.value<fromInput.value)fromInput.value=toInput.value; render();}}); document.querySelector('#all-time').addEventListener('click',()=>{{fromInput.value=firstDate; toInput.value=lastDate; fromInput.max=lastDate; toInput.min=firstDate; render();}}); document.querySelector('#group-toggle').addEventListener('change',event=>uncategorizedRows(event.target.checked)); render();
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
