"""Directives page: the phone page for ruling on directives and on form disagreements.

One self-contained page at `/directives` on the review server: first the form cards (a Bee
source whose speaker count and Jev disagree on its form, corpuslm.form; four buttons, one per
label, and the owner's tap is the ruling), then one card per directive (corpuslm.directives),
grouped by source (title and date), each with the words as heard, the restatement, the
addressee and the time, and two buttons, Confirm and Decline. A tap posts
`{"method": "directive_verdict", "params": {"id", "verdict"}}` or
`{"method": "form_verdict", "params": {"source", "label"}}` to the server's one API route
under the same write check as the review page. Ruled cards stay, dimmed, so the owner can
change a ruling; the newest row is in force.

Never imports: cli, clikit, db, store, any model transport.

Decisions:
- The card shows what the owner needs to rule: the words as heard, the restatement, the
  addressee and the time, nothing else, escaped. Enforced by:
  tests/test_directives_page.py::test_the_page_shows_heard_restatement_addressee_and_time.
"""

from __future__ import annotations

import html

from .concepts_page import _CSS as _BASE_CSS
from .form import LABELS

_CSS = (
    _BASE_CSS
    + """
.card{display:grid;gap:8px;padding:10px 0;border-top:1px solid var(--line)}.card:first-of-type{border-top:0}
.heard{font-size:17px}.heard b{background:var(--chip);padding:0 4px;border-radius:4px}
.restated{font-size:15px;color:var(--ink)}.meta{font-size:12px;color:var(--muted)}
.who{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 8px;font-size:12px}
.who.computer{background:var(--good-soft);color:var(--good)}
.buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px}.buttons.four{grid-template-columns:1fr 1fr 1fr 1fr}
.buttons button{font:inherit;font-weight:600;font-size:15px;padding:11px 6px;border-radius:9px;border:1.5px solid var(--line);
background:transparent;color:var(--ink);min-height:44px}
.buttons button[aria-pressed="true"]{background:var(--good);border-color:var(--good);color:#fff}
.buttons button.decline[aria-pressed="true"]{background:var(--muted);border-color:var(--muted)}
.card.done{opacity:.6}
"""
)

_SCRIPT = """
(function(){
  var saved=document.getElementById('saved');var status=document.getElementById('status');
  function flash(text,warn){saved.textContent=text;saved.classList.add('on');saved.classList.toggle('warn',!!warn);
    setTimeout(function(){saved.classList.remove('on')},1400)}
  function count(){status.textContent=document.querySelectorAll('.card.directive:not(.done)').length+' to rule'}
  function post(params){return fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-Review':'1'},
      body:JSON.stringify(params)}).then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j}})})}
  function mark(card,value){card.querySelectorAll('.buttons button').forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-v')===value))});card.classList.add('done')}
  function rule(card,value){
    var params=card.classList.contains('form')
      ?{method:'form_verdict',params:{source:card.getAttribute('data-source'),label:value}}
      :{method:'directive_verdict',params:{id:card.getAttribute('data-id'),verdict:value}};
    post(params)
    .then(function(out){if(!out.ok){flash('Not saved: '+(out.body.error||'error'),true);return}
      mark(card,value);count();flash('Saved: '+value)})
    .catch(function(){flash('Not saved: no server',true)})}
  document.querySelectorAll('.card .buttons button').forEach(function(b){
    b.addEventListener('click',function(){rule(b.closest('.card'),b.getAttribute('data-v'))})});
  count();
})();
"""


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _directive_card(row: dict) -> str:
    state = row["state"]
    classes = "card directive" + (" done" if state != "open" else "")
    pressed_yes = "true" if state == "confirm" else "false"
    pressed_no = "true" if state == "decline" else "false"
    addressee = html.escape(row.get("addressee") or "self")
    return (
        f'<div class="{classes}" data-id="{html.escape(row["id"])}">'
        f'<div class="heard">Heard: <b>{html.escape(row.get("heard") or "")}</b></div>'
        f'<div class="restated">{html.escape(row.get("text") or "")}</div>'
        f'<div class="meta"><span class="who {addressee}">to {addressee}</span> · '
        f"at {_clock(row.get('start_seconds'))}</div>"
        f'<div class="buttons"><button data-v="confirm" aria-pressed="{pressed_yes}">Confirm'
        f'</button><button class="decline" data-v="decline" aria-pressed="{pressed_no}">'
        "Decline</button></div></div>"
    )


def _form_card(card: dict) -> str:
    buttons = "".join(
        f'<button data-v="{label}" aria-pressed="false">{label}</button>' for label in LABELS
    )
    head = html.escape(card.get("title") or card["source_id"])
    date = html.escape(card.get("date") or "")
    return (
        f'<div class="card form" data-source="{html.escape(card["source_id"])}">'
        f'<div class="restated">{head}{(" · " + date) if date else ""}</div>'
        f'<div class="meta">{int(card.get("count", 0))} named speakers say '
        f"<b>{html.escape(card.get('proposed') or '')}</b>; Jev says "
        f"<b>{html.escape(card.get('jev') or '')}</b>. Which is it?</div>"
        f'<div class="buttons four">{buttons}</div></div>'
    )


def render(summary: dict, rows: list[dict], form_cards: list[dict] | None = None) -> str:
    """`summary` is `corpuslm.directives.summary`; `rows` the rows of
    `corpuslm.directives.listing`; `form_cards` the rows of `corpuslm.form.cards`."""
    states = summary["states"]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["source_id"], []).append(row)
    sections = []
    if form_cards:
        sections.append(
            f"<section><h2>Form · {len(form_cards)} to rule</h2>"
            + "".join(_form_card(card) for card in form_cards)
            + "</section>"
        )
    for source_id, group in groups.items():
        head = html.escape(group[0].get("title") or source_id)
        date = html.escape(group[0].get("date") or "")
        sections.append(
            f"<section><h2>{head}{(' · ' + date) if date else ''}</h2>"
            + "".join(_directive_card(row) for row in group)
            + "</section>"
        )
    body = "".join(sections) or '<section><p class="empty">No directives yet.</p></section>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Directives</title><style>{_CSS}</style></head><body>"
        f'<header><div class="row"><h1>Directives</h1><span class="count" id="status"></span></div>'
        f'<p class="note" style="color:inherit;opacity:.9;margin:6px auto 0;max-width:640px">'
        f"{summary['directives']} directives: {states['open']} open, "
        f"{states['confirm']} confirmed, {states['decline']} declined · "
        f"{summary['addressees'].get('computer', 0)} to Computer</p></header>"
        f"<main>{body}</main>"
        f'<div class="saved" id="saved">Saved</div>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


__all__ = ["render"]
