"""Concepts page: the phone page for ruling on boundary concepts (corpuslm.concepts).

One self-contained page at `/concepts` on the review server: the gate, the counts by state, one
row per boundary concept with two buttons, In and Out, then the concepts in force with their
counts (Out demotes one), the ones out in a collapsed section (In promotes one), and the
section-count distribution as bars. A tap posts `{"method": "concept_verdict", "params": {"key", "verdict"}}` to the server's
one API route under the same write check as the review page, and the row shows the ruling. The
page shows concept labels and counts only: no claim's text and no transcript text.

`render(summary)` turns `corpuslm.concepts.summary` (plus the ruled list) into HTML; the server
builds it per request so it is never older than the database.

Never imports: cli, clikit, db, store, any model transport.

Decisions:
- The page shows labels and counts only; no claim or transcript text. Enforced by:
  tests/test_concepts_page.py::test_the_page_shows_labels_and_counts_and_escapes_them.
"""

from __future__ import annotations

import html
import json

from . import brand

_CSS = """
:root{--ground:#f6f4ef;--panel:#fff;--ink:#1c1b18;--muted:#6b675c;--line:#ddd8cc;--band:#2f4858;
--band-ink:#fff;--good:#0a7a3c;--good-soft:#dff3e7;--chip:#ece8de;--focus:#7b4fb0;--warn:#b7410e}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--ground:#15171a;--panel:#20242a;
--ink:#ece9e1;--muted:#a19c8f;--line:#343a42;--band:#3f6076;--good:#5ad38b;--good-soft:#153a25;
--chip:#2c323a;--focus:#c4a3ee;--warn:#ff9b6a}}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--band);color:var(--band-ink);padding:10px 14px}
header .row{display:flex;justify-content:space-between;align-items:baseline;max-width:640px;margin:0 auto}
header h1{margin:0;font-size:18px}header .count{font-size:13px;opacity:.9;font-variant-numeric:tabular-nums}
main{max-width:640px;margin:0 auto;padding:12px 10px 80px;display:grid;gap:14px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
h2{margin:0 0 8px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.bars{display:grid;gap:4px;font-size:13px}.bar{display:grid;grid-template-columns:3em 1fr 3em;gap:8px;align-items:center}
.bar i{display:block;height:12px;background:var(--band);border-radius:3px}.bar.gate i{background:var(--good)}
article{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
article:first-of-type{border-top:0}article .label{font-weight:600}article .counts{font-size:12px;color:var(--muted)}
article button{font:inherit;font-weight:600;font-size:15px;padding:9px 14px;border-radius:9px;border:1.5px solid var(--line);
background:transparent;color:var(--ink);min-height:44px}
article button[aria-pressed="true"]{background:var(--good);border-color:var(--good);color:#fff}
article button.out[aria-pressed="true"]{background:var(--muted);border-color:var(--muted)}
button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.empty{color:var(--muted);text-align:center;padding:12px}
.saved{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);background:var(--ink);color:var(--ground);
padding:8px 14px;border-radius:999px;font-size:13px;opacity:0;transition:opacity .2s}.saved.on{opacity:1}
.note{font-size:13px;color:var(--muted)}.note.warn{color:var(--warn)}
"""

_SCRIPT = """
(function(){
  var saved=document.getElementById('saved');var status=document.getElementById('status');
  function flash(text,warn){saved.textContent=text;saved.classList.add('on');saved.classList.toggle('warn',!!warn);
    setTimeout(function(){saved.classList.remove('on')},1400)}
  function rule(row,verdict){
    var key=row.getAttribute('data-key');
    fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-Review':'1'},
      body:JSON.stringify({method:'concept_verdict',params:{key:key,verdict:verdict}})})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j}})})
    .then(function(out){
      if(!out.ok){flash('Not saved: '+(out.body.error||'error'),true);return}
      row.querySelectorAll('button').forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-v')===verdict))});
      flash('Saved: '+verdict)})
    .catch(function(){flash('Not saved: no server',true)})}
  document.querySelectorAll('article[data-key] button').forEach(function(b){
    b.addEventListener('click',function(){rule(b.closest('article'),b.getAttribute('data-v'))})});
  status.textContent=document.querySelectorAll('section:first-of-type article[data-key]').length+' on the boundary';
})();
"""


def _bars(histogram: dict, min_sections: int) -> str:
    if not histogram:
        return '<p class="empty">No concepts yet.</p>'
    top = max(histogram.values())
    rows = []
    for sections, count in sorted((int(k), int(v)) for k, v in histogram.items()):
        width = max(2, int(100 * count / top))
        gate = " gate" if sections >= min_sections else ""
        rows.append(
            f'<div class="bar{gate}"><span>{sections}</span><i style="width:{width}%"></i>'
            f"<span>{count}</span></div>"
        )
    return '<div class="bars">' + "".join(rows) + "</div>"


def _row(entry: dict, verdict: str = "") -> str:
    key = html.escape(entry["key"])
    label = html.escape(entry["label"])
    counts = f"{entry['sections']} sections · {entry['claims']} claims · {entry['sources']} sources"
    pressed_in = "true" if verdict == "in" else "false"
    pressed_out = "true" if verdict == "out" else "false"
    return (
        f'<article data-key="{key}"><div><div class="label">{label}</div>'
        f'<div class="counts">{counts}</div></div>'
        f'<button data-v="in" aria-pressed="{pressed_in}">In</button>'
        f'<button class="out" data-v="out" aria-pressed="{pressed_out}">Out</button></article>'
    )


def render(summary: dict, rows: list[dict] | None = None) -> str:
    """`summary` is `corpuslm.concepts.summary`; `rows` is `corpuslm.concepts.listing` (every
    concept with its state, counts and verdict), from which the in-force and out lists are
    drawn; a concept's own verdict shows as the pressed button."""
    states = summary["states"]
    gate = summary["gate"]
    boundary = "".join(_row(entry) for entry in summary["boundary"]) or (
        '<p class="empty">Nothing on the boundary.</p>'
    )
    rows = rows or []
    in_rows = "".join(_row(e, e.get("verdict", "")) for e in rows if e["state"] == "in")
    out_rows = "".join(_row(e, e.get("verdict", "")) for e in rows if e["state"] == "out")
    out_count = sum(1 for e in rows if e["state"] == "out")
    counts = json.dumps(states)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>{brand.HEAD}"
        f"<title>Concepts</title><style>{_CSS}</style></head><body>"
        f'<header><div class="row"><h1>{brand.ICON}Concepts</h1><span class="count" id="status"></span></div>'
        f'<p class="note" style="color:inherit;opacity:.9;margin:6px auto 0;max-width:640px">'
        f"{summary['concepts']} concepts: {states['in']} in, {states['boundary']} boundary, "
        f"{states['out']} out · gate {gate['min_sections']} sections, {gate['min_claims']} claims"
        "</p></header><main>"
        f"<section><h2>Boundary: tap In or Out</h2>{boundary}</section>"
        f"<section><h2>In force ({states['in']})</h2>"
        f"{in_rows or '<p class="empty">None yet.</p>'}</section>"
        f"<section><details><summary>Out ({out_count}): tap In to bring one in</summary>"
        f"{out_rows or '<p class="empty">None.</p>'}</details></section>"
        f"<section><h2>Concepts by section count</h2>"
        f"{_bars(summary['sections_histogram'], gate['min_sections'])}"
        '<p class="note">Green bars are at or above the gate.</p></section>'
        f'</main><div class="saved" id="saved">Saved</div>'
        f'<script type="application/json" id="counts">{counts}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


__all__ = ["render"]
