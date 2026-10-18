"""Usage page: the board of `corpuslm.usage` as one HTML page, and the same board as terminal text.

Numbers and money only. Light and dark colours are tokens on the root, tables scroll inside their
own box so the page never scrolls sideways on a phone, and the page asks the browser to reload
every 60 seconds.

Never imports: cli, clikit, db, store, any model transport.
"""

from __future__ import annotations

from html import escape

from . import brand

REFRESH_SECONDS = 60
OUTCOMES = ("ok", "none", "error", "skipped")

STYLE = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--card:#f5f5f7;--ok:#1a7f37;--bad:#c62828;--warn:#9a6700;
--bar:#d9d9de;--fill:#0a66c2}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#f2f2f2;--mut:#98989d;--card:#1c1c1e;
--ok:#4cc26b;--bad:#ff6b6b;--warn:#e3b341;--bar:#3a3a3c;--fill:#5aa9ff}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.4 -apple-system,system-ui,sans-serif;
margin:0 auto;max-width:820px;padding:16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:17px;margin:26px 0 4px}
h3{font-size:15px;margin:16px 0 4px}
.mut{color:var(--mut)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.card{background:var(--card);border-radius:12px;padding:12px;min-width:0}
.card b{display:block;font-size:26px}.card small{display:block;color:var(--mut)}
.meter{height:6px;border-radius:3px;background:var(--bar);margin-top:8px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--fill)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}th,td{text-align:right;padding:6px 8px;
border-bottom:1px solid var(--card);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
.ok{color:var(--ok)}.error{color:var(--bad)}.none{color:var(--warn)}
"""


def money(value: float) -> str:
    return f"${value:,.2f}" if value >= 0.01 or value == 0 else f"${value:,.4f}"


def _count(table: dict, fetcher: str, outcome: str) -> int:
    return table.get(fetcher, {}).get(outcome, 0)


def _cap_text(cap_usd: float | None) -> str:
    return money(cap_usd) if cap_usd is not None else "none set"


def _tool_card(tool: dict) -> str:
    cap_usd, biggest = tool["per_run_usd"], tool["biggest_day_usd"]
    meter = ""
    if cap_usd:
        share = min(100, round(100 * biggest / cap_usd))
        meter = (
            f'<div class="meter" title="{share}% of the per-run cap">'
            f'<i style="width:{share}%"></i></div>'
        )
    return (
        f'<div class="card"><span class="mut">{escape(tool["tool"])}</span>'
        f"<b>{money(tool['total']['cost_usd'])}</b>"
        f"<small>Per-run cap {_cap_text(cap_usd)}</small>"
        f"<small>Biggest day {money(biggest)} (a day can hold several runs)</small>"
        f"<small>Per-day cap {_cap_text(tool['per_day_usd'])}</small>{meter}</div>"
    )


def _stage_table(tool: dict) -> str:
    rows = "".join(
        f"<tr><td>{escape(stage)}</td><td>{stage_totals['calls']}</td><td>{stage_totals['cached']}</td>"
        f"<td>{stage_totals['tokens_in']:,}</td><td>{stage_totals['tokens_out']:,}</td><td>{money(stage_totals['cost_usd'])}</td></tr>"
        for stage, stage_totals in tool["by_stage"].items()
    )
    if not rows:
        return '<p class="mut">No model calls this month.</p>'
    return (
        '<div class="scroll"><table><tr><th>Stage</th><th>Calls</th><th>Cached</th>'
        f"<th>Tokens in</th><th>Tokens out</th><th>Dollars</th></tr>{rows}</table></div>"
    )


def _day_table(tools: list[dict]) -> str:
    days = sorted({day for tool in tools for day in tool["by_day"]}, reverse=True)
    if not days:
        return ""
    head = "".join(
        f"<th>{escape(tool['tool'])} calls</th><th>{escape(tool['tool'])} $</th>" for tool in tools
    )
    body = ""
    for day in days:
        cells = ""
        for tool in tools:
            day_totals = tool["by_day"].get(day)
            cells += (
                f"<td>{day_totals['calls']}</td><td>{money(day_totals['cost_usd'])}</td>"
                if day_totals
                else "<td></td><td></td>"
            )
        body += f"<tr><td>{escape(day)}</td>{cells}</tr>"
    return f'<div class="scroll"><table><tr><th>Day (UTC)</th>{head}</tr>{body}</table></div>'


def _fetch_section(fetches: dict, month: str) -> str:
    this_month, total = fetches["this_month"], fetches["all_time"]
    cards = [
        (f"Supadata calls answered ({month})", _count(this_month, "supadata", "ok"), "ok"),
        (
            f"Rejected: limit (HTTP 429) or error ({month})",
            _count(this_month, "supadata", "error"),
            "error",
        ),
        ("No captions in that language", _count(this_month, "supadata", "none"), "none"),
        ("Answered, all time", _count(total, "supadata", "ok"), "ok"),
        ("Rejected, all time", _count(total, "supadata", "error"), "error"),
    ]
    tiles = "".join(
        f'<div class="card"><span class="mut">{escape(label)}</span>'
        f'<b class="{cls}">{count}</b></div>'
        for label, count, cls in cards
    )
    columns = [
        (fetcher, outcome)
        for fetcher in sorted({fetcher for day in fetches["by_day"].values() for fetcher in day})
        for outcome in OUTCOMES
        if any(day.get(fetcher, {}).get(outcome) for day in fetches["by_day"].values())
    ]
    head = "".join(f"<th>{escape(fetcher)} {outcome}</th>" for fetcher, outcome in columns)
    body = "".join(
        f"<tr><td>{escape(day)}</td>"
        + "".join(
            f'<td class="{outcome}">{_count(fetches["by_day"][day], fetcher, outcome) or ""}</td>'
            for fetcher, outcome in columns
        )
        + "</tr>"
        for day in sorted(fetches["by_day"], reverse=True)
    )
    error = fetches["last_error"].get("supadata", "")
    note = f"Last Supadata error: {escape(error)}." if error else "No Supadata error on record."
    table = f'<div class="scroll"><table><tr><th>Day (UTC)</th>{head}</tr>{body}</table></div>'
    return (
        "<h2>Caption fetches</h2>"
        '<div class="mut">Counts from the fetch ledger. A Supadata call answered is one billed '
        "call; the plan's own meter is the authority on credits.</div>"
        f'<div class="cards">{tiles}</div><p class="mut">{note}</p>{table}'
    )


def render(board: dict) -> str:
    spend = board["model_spend"]
    tools = list(spend["tools"].values())
    cards = "".join(_tool_card(tool) for tool in tools)
    stages = "".join(
        f"<h3>{escape(tool['tool'])}, by stage</h3>{_stage_table(tool)}" for tool in tools
    )
    problems = "".join(f'<p class="error">{escape(problem)}</p>' for problem in board["problems"])
    return (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1">'
        f"{brand.HEAD}"
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        f"<title>Usage</title><style>{STYLE}</style>"
        f"<h1>{brand.ICON}Usage, {escape(board['month'])}</h1>"
        f'<div class="mut">{escape(board["note"])}</div>{problems}'
        f"<h2>Model spend, {money(spend['cost_usd'])} this month</h2>"
        f'<div class="cards">{cards}</div>{stages}<h3>By day</h3>{_day_table(tools)}'
        f"{_fetch_section(board['caption_fetches'], board['month'])}"
    )


def text(board: dict) -> str:
    """The same board for a terminal: short tables, numbers only."""
    spend = board["model_spend"]
    out = [f"usage {board['month']}", board["note"], ""]
    out.append(f"model spend: {money(spend['cost_usd'])} this month")
    for tool in spend["tools"].values():
        total = tool["total"]
        out.append(
            f"  {tool['tool']}: {money(total['cost_usd'])}, {total['calls']} calls "
            f"({total['cached']} cached), "
            f"{total['tokens_in']:,} tokens in, {total['tokens_out']:,} out; per-run cap "
            f"{_cap_text(tool['per_run_usd'])}, biggest day {money(tool['biggest_day_usd'])}, "
            f"per-day cap {_cap_text(tool['per_day_usd'])}"
        )
        for stage, stage_totals in tool["by_stage"].items():
            out.append(
                f"    {stage}: {stage_totals['calls']} calls, {stage_totals['cached']} cached, "
                f"{stage_totals['tokens_in']:,}/{stage_totals['tokens_out']:,} tokens, "
                f"{money(stage_totals['cost_usd'])}"
            )
        for day, day_totals in sorted(tool["by_day"].items(), reverse=True):
            out.append(f"    {day}: {day_totals['calls']} calls, {money(day_totals['cost_usd'])}")
    fetches = board["caption_fetches"]
    out += ["", "caption fetches:"]
    for label, table in (("this month", fetches["this_month"]), ("all time", fetches["all_time"])):
        for fetcher in sorted(table):
            counts = ", ".join(
                f"{outcome} {count}" for outcome, count in sorted(table[fetcher].items())
            )
            out.append(f"  {label}, {fetcher}: {counts}")
    for day in sorted(fetches["by_day"], reverse=True)[:14]:
        cells = ", ".join(
            f"{fetcher} {outcome} {count}"
            for fetcher, fetcher_counts in sorted(fetches["by_day"][day].items())
            for outcome, count in sorted(fetcher_counts.items())
        )
        out.append(f"  {day}: {cells}")
    for fetcher, error in sorted(fetches["last_error"].items()):
        out.append(f"  last {fetcher} error: {error}")
    out += [f"problem: {problem}" for problem in board["problems"]]
    return "\n".join(out) + "\n"
