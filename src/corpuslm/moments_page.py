"""Moments page: the phone page for approving or declining proposed moments (corpuslm.moments).

One self-contained page at `/moments` on the review server: the counts by state, then one card
per proposed moment, grouped by source (title and date), each with its insight, its span in
minutes and seconds, its concepts, and two buttons, Approve and Decline. A tap posts
`{"method": "moment_verdict", "params": {"id", "verdict"}}` to the server's one API route under
the same write check as the review page. The page carries insights, labels, times and counts:
no transcript text.

Never imports: cli, clikit, db, store, any model transport.

Decisions:
- The page shows insights, concept labels, times and counts only; no transcript text. Enforced
  by: tests/test_moments_page.py::test_the_page_shows_insights_spans_and_concepts_and_escapes.
"""

from __future__ import annotations

import html

from .concepts_page import _CSS as _BASE_CSS

_CSS = (
    _BASE_CSS
    + """
.card{display:grid;gap:8px;padding:10px 0;border-top:1px solid var(--line)}.card:first-of-type{border-top:0}
.insight{font-size:17px}.meta{font-size:12px;color:var(--muted)}.chips span{display:inline-block;background:var(--chip);
border-radius:999px;padding:2px 8px;font-size:12px;margin:2px 4px 0 0}
.buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px}.buttons button{font:inherit;font-weight:600;font-size:15px;
padding:11px 10px;border-radius:9px;border:1.5px solid var(--line);background:transparent;color:var(--ink);min-height:44px}
.buttons button[aria-pressed="true"]{background:var(--good);border-color:var(--good);color:#fff}
.buttons button.decline[aria-pressed="true"]{background:var(--muted);border-color:var(--muted)}
.card.done{opacity:.55}
"""
)

_SCRIPT = """
(function(){
  var saved=document.getElementById('saved');var status=document.getElementById('status');
  function flash(text,warn){saved.textContent=text;saved.classList.add('on');saved.classList.toggle('warn',!!warn);
    setTimeout(function(){saved.classList.remove('on')},1400)}
  function count(){status.textContent=document.querySelectorAll('.card:not(.done)').length+' to rule'}
  function rule(card,verdict){
    fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-Review':'1'},
      body:JSON.stringify({method:'moment_verdict',params:{id:card.getAttribute('data-id'),verdict:verdict}})})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j}})})
    .then(function(out){
      if(!out.ok){flash('Not saved: '+(out.body.error||'error'),true);return}
      card.querySelectorAll('button').forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-v')===verdict))});
      card.classList.add('done');count();flash('Saved: '+verdict)})
    .catch(function(){flash('Not saved: no server',true)})}
  document.querySelectorAll('.card button').forEach(function(b){
    b.addEventListener('click',function(){rule(b.closest('.card'),b.getAttribute('data-v'))})});
  count();
})();
"""


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _card(moment: dict) -> str:
    state = moment["state"]
    done = " done" if state != "proposed" else ""
    chips = "".join(f"<span>{html.escape(key)}</span>" for key in moment.get("concepts") or [])
    pressed_yes = "true" if state == "approved" else "false"
    pressed_no = "true" if state == "declined" else "false"
    return (
        f'<div class="card{done}" data-id="{html.escape(moment["id"])}">'
        f'<div class="insight">{html.escape(moment["insight"])}</div>'
        f'<div class="meta">{_clock(moment.get("start_seconds"))} to '
        f"{_clock(moment.get('end_seconds'))} · {html.escape(moment['origin'])}</div>"
        + (f'<div class="chips">{chips}</div>' if chips else "")
        + f'<div class="buttons"><button data-v="approved" aria-pressed="{pressed_yes}">Approve</button>'
        f'<button class="decline" data-v="declined" aria-pressed="{pressed_no}">Decline</button></div>'
        "</div>"
    )


def render(summary: dict, moments: list[dict]) -> str:
    """`summary` is `corpuslm.moments.summary`; `moments` the rows of `corpuslm.moments.listing`
    to show (the proposed ones first, then recently ruled ones, as the caller chooses)."""
    states = summary["states"]
    groups: dict[str, list[dict]] = {}
    for moment in moments:
        groups.setdefault(moment["source_id"], []).append(moment)
    sections = []
    for source_id, rows in groups.items():
        head = html.escape(rows[0].get("title") or source_id)
        date = html.escape(rows[0].get("date") or "")
        sections.append(
            f"<section><h2>{head}{(' · ' + date) if date else ''}</h2>"
            + "".join(_card(moment) for moment in rows)
            + "</section>"
        )
    body = "".join(sections) or '<section><p class="empty">Nothing proposed.</p></section>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Moments</title><style>{_CSS}</style></head><body>"
        f'<header><div class="row"><h1>Moments</h1><span class="count" id="status"></span></div>'
        f'<p class="note" style="color:inherit;opacity:.9;margin:6px auto 0;max-width:640px">'
        f"{summary['moments']} moments: {states['approved']} approved, "
        f"{states['proposed']} proposed, {states['declined']} declined</p></header>"
        f"<main>{body}</main>"
        f'<div class="saved" id="saved">Saved</div>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


__all__ = ["render"]
