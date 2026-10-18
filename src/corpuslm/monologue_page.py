"""Monologue pages: start a monologue and choose what it draws on, from a phone
(corpuslm.monologues, nagarkar/corpuslm#51).

Two self-contained pages on the review server. `/monologues` lists the monologues with a form to
start one (a name, an optional brief). `/monologues/<name>` is the choosing page: the brief (saved
on tap), the moments and bodies attached so far (tap to take off), the candidate moments ranked by
the concepts they share with the attached ones and by the brief's words (every moment with Edit,
which rewords its insight, and Delete, which declines it), the bodies of work from
history, and the YouTube sources that share those concepts or match the brief (tap to attach as an
occasional body). Estimate shows what a run would cost from the cache alone; Run appears after an
estimate and is the second tap, the owner's yes. Every tap posts to the server's one API route
under the same write check as the review page. The page shows insights, titles, labels and counts;
no transcript text.

Never imports: cli, clikit, db, store, any model transport.
"""

from __future__ import annotations

import html
import json
import re

from . import brand
from .concepts_page import _CSS as _BASE_CSS

_CSS = (
    _BASE_CSS
    + """
.row2{display:grid;gap:8px}textarea,input[type=text]{width:100%;font:inherit;font-size:15px;padding:9px 10px;border-radius:8px;
border:1.5px solid var(--line);background:var(--ground);color:var(--ink);min-height:44px}
.card{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
.card:first-of-type{border-top:0}.card .text{font-size:15px}.card .meta{font-size:12px;color:var(--muted)}
.chips span{display:inline-block;background:var(--chip);border-radius:999px;padding:2px 8px;font-size:12px;margin:2px 4px 0 0}
.chips span.hot{background:var(--good-soft);color:var(--good)}
button.act{font:inherit;font-weight:600;font-size:15px;padding:9px 14px;border-radius:9px;border:1.5px solid var(--line);
background:transparent;color:var(--ink);min-height:44px}button.act.primary{background:var(--good);border-color:var(--good);color:#fff}
button.act.warn{border-color:var(--warn);color:var(--warn)}
.big{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
.result{font-size:14px;margin-top:8px}.result a{color:inherit}
input.search{width:100%;font:inherit;font-size:15px;padding:9px 10px;border-radius:8px;border:1.5px solid var(--line);
background:var(--ground);color:var(--ink);min-height:44px;margin:0 0 6px}
.tools{display:flex;gap:14px;margin-top:2px}button.link{font:inherit;font-size:13px;background:transparent;border:0;padding:0;
color:var(--muted);text-decoration:underline;min-height:32px}button.link.danger{color:var(--warn)}
.editbox{display:grid;gap:6px;margin-top:4px}.editbox textarea{min-height:5em}.editbox div{display:flex;gap:8px}
textarea{resize:vertical;line-height:1.45}
#brief{min-height:40vh}#newbrief{min-height:20vh}
@media (min-width:900px){header .row,main{max-width:min(1400px,96vw)}#brief{min-height:55vh}}
"""
)

_SCRIPT = """
(function(){
  var name=document.body.getAttribute('data-name');var saved=document.getElementById('saved');
  function flash(text,warn){saved.textContent=text;saved.classList.add('on');saved.classList.toggle('warn',!!warn);
    setTimeout(function(){saved.classList.remove('on')},1600)}
  function call(method,params){return fetch('/api/review',{method:'POST',headers:{'Content-Type':'application/json','X-Review':'1'},
    body:JSON.stringify({method:method,params:params})}).then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j}})})}
  function reload(){setTimeout(function(){location.reload()},500)}
  document.querySelectorAll('textarea').forEach(function(box){
    function fit(){box.style.height='auto';box.style.height=Math.max(box.scrollHeight+4,box.offsetHeight)+'px'}
    box.addEventListener('input',fit);fit()});
  var shortlists={};document.querySelectorAll('[data-list]').forEach(function(l){shortlists[l.getAttribute('data-list')]=l.innerHTML});
  document.querySelectorAll('input.search').forEach(function(box){var timer=null;box.addEventListener('input',function(){
    clearTimeout(timer);timer=setTimeout(function(){var kind=box.getAttribute('data-search');var list=document.querySelector('[data-list="'+kind+'"]');
      var q=box.value.trim();if(q.length<2){list.innerHTML=shortlists[kind];return}
      call('monologue_search',{name:name,kind:kind,query:q}).then(function(o){
        if(!o.ok){flash('Search failed: '+(o.body.error||''),true);return}if(box.value.trim()===q)list.innerHTML=o.body.result.html})},250)})});
  document.addEventListener('click',function(ev){var b=ev.target.closest('[data-act]');if(!b)return;
    var act=b.getAttribute('data-act');var params={name:name};
    if(act==='approve-attach'){var mid=b.getAttribute('data-id');call('moment_verdict',{id:mid,verdict:'approved'}).then(function(o){
      if(!o.ok){flash('Not approved: '+(o.body.error||''),true);return null}return call('monologue_attach',{name:name,moment:mid})})
      .then(function(o){if(!o)return;if(o.ok){flash('Approved and attached');reload()}else{flash('Not attached: '+(o.body.error||''),true)}});return}
    if(act==='edit'){var card=b.closest('.card');var shown=card.querySelector('.insight');if(card.querySelector('.editbox'))return;
      var box=document.createElement('div');box.className='editbox';var area=document.createElement('textarea');area.value=shown.textContent;
      var row=document.createElement('div');var save=document.createElement('button');save.className='act primary';save.textContent='Save';
      var cancel=document.createElement('button');cancel.className='act';cancel.textContent='Cancel';row.appendChild(save);row.appendChild(cancel);
      box.appendChild(area);box.appendChild(row);shown.after(box);shown.hidden=true;area.focus();
      cancel.addEventListener('click',function(){box.remove();shown.hidden=false});
      save.addEventListener('click',function(){call('moment_edit',{id:card.getAttribute('data-moment'),insight:area.value}).then(function(o){
        if(!o.ok){flash('Not saved: '+(o.body.error||''),true);return}shown.textContent=o.body.result.insight;box.remove();shown.hidden=false;flash('Saved')})});
      return}
    if(act==='delete'){var dcard=b.closest('.card');if(!confirm('Delete this moment? It leaves every monologue and is not proposed again.'))return;
      var id=dcard.getAttribute('data-moment');
      var off=dcard.getAttribute('data-attached')?call('monologue_detach',{name:name,moment:id}):Promise.resolve({ok:true});
      off.then(function(o){if(!o.ok){flash('Not taken off: '+(o.body.error||''),true);return null}
        return call('moment_verdict',{id:id,verdict:'declined'})}).then(function(o){if(!o)return;
        if(o.ok){flash('Deleted');reload()}else{flash('Not deleted: '+(o.body.error||''),true)}});return}
    if(act==='save'){params.brief=document.getElementById('brief').value;params.title=document.getElementById('title').value;
      call('monologue_brief',params).then(function(o){flash(o.ok?'Saved':'Not saved: '+(o.body.error||''),!o.ok)});return}
    if(act==='new'){call('monologue_new',{name:document.getElementById('newname').value,brief:document.getElementById('newbrief').value})
      .then(function(o){if(o.ok){location.href='/monologues/'+o.body.result.name}else{flash('Not made: '+(o.body.error||''),true)}});return}
    if(act==='attach'||act==='detach'){params[b.getAttribute('data-kind')]=b.getAttribute('data-id');
      call('monologue_'+act,params).then(function(o){if(o.ok){flash(act==='attach'?'Attached':'Taken off');reload()}else{flash('Not saved: '+(o.body.error||''),true)}});return}
    if(act==='estimate'){b.disabled=true;call('monologue_estimate',params).then(function(o){b.disabled=false;var out=document.getElementById('estimate');
      if(!o.ok){out.textContent='Estimate failed: '+(o.body.error||'');return}var r=o.body.result;
      out.textContent=r.stored_run?'A run for these ingredients exists: running would send nothing.':
        'About $'+(r.estimated_usd==null?'?':r.estimated_usd.toFixed(3))+' ('+r.tokens_in+' tokens in, '+r.moments+' moments, '+r.bodies+' bodies).';
      document.getElementById('run').hidden=false});return}
    if(act==='run'){b.disabled=true;b.textContent='Running…';call('monologue_run',{name:name,fresh:document.getElementById('fresh').checked})
      .then(function(o){b.disabled=false;b.textContent='Run';var out=document.getElementById('runout');
      if(!o.ok){out.textContent='Run failed: '+(o.body.error||'');return}var r=o.body.result;
      out.innerHTML=r.unchanged?'Unchanged ingredients: the stored run v'+r.run+' stands; nothing was sent.':
        'Run v'+r.run+': '+r.paragraphs+' paragraphs, '+r.words+' words, $'+r.cost_usd.toFixed(3)+'. <a href="/teleprompter/'+name+'">Teleprompter</a>'+(r.url?' · <a href="'+r.url+'">Doc</a>':'')});return}
  });
})();
"""


def _chips(keys: list[str], hot: set[str] | None = None) -> str:
    hot = hot or set()
    return "".join(
        f'<span class="{"hot" if key in hot else ""}">{html.escape(key)}</span>' for key in keys
    )


def _card(text: str, meta: str, button: str) -> str:
    return (
        f'<div class="card"><div><div class="text">{text}</div>'
        f'<div class="meta">{meta}</div></div>{button}</div>'
    )


def _moment_card(moment: dict, meta: str, button: str, *, attached: bool) -> str:
    """A moment with Edit (reword its insight) and Delete (decline it; an attached one is taken
    off first) beside its Add or Off."""
    moment_id = html.escape(moment["id"])
    return (
        f'<div class="card" data-moment="{moment_id}" data-attached="{"1" if attached else ""}">'
        f'<div><div class="text insight">{html.escape(moment["insight"])}</div>'
        f'<div class="meta">{meta}</div>'
        '<div class="tools"><button class="link" data-act="edit" type="button">Edit</button>'
        '<button class="link danger" data-act="delete" type="button">Delete</button></div></div>'
        f"{button}</div>"
    )


def offered_moment_card(m: dict) -> str:
    """A moment that could be added: Add, or Approve & add for one still proposed."""
    proposed = m.get("state", "approved") == "proposed"
    meta = (
        f"{html.escape(m['title'] or m['source_id'])} · {html.escape(m['date'])}"
        f' <span class="chips">{_chips(m["concepts"], set(m.get("shared_concepts") or []))}</span>'
        + (f" · brief: {html.escape(', '.join(m['brief_words']))}" if m.get("brief_words") else "")
        + (" · proposed" if proposed else "")
    )
    act = "approve-attach" if proposed else "attach"
    label = "Approve &amp; add" if proposed else "Add"
    return _moment_card(
        m,
        meta,
        f'<button class="act" data-act="{act}" data-kind="moment" '
        f'data-id="{html.escape(m["id"])}">{label}</button>',
        attached=False,
    )


_SPEAKER_LABEL = re.compile(r"^(HOST|GUEST_\d+|UNKNOWN)$")  # a diarization label, not a name


def video_card(s: dict, hot: set[str] | None = None) -> str:
    """A YouTube source that could be added: its title linked to the video, then channel, date,
    the people in it and the concepts it shares."""
    link = f"https://www.youtube.com/watch?v={html.escape(s['source_id'])}"
    people = ", ".join(
        name for name in s.get("people") or [] if name and not _SPEAKER_LABEL.match(name)
    )
    meta = " · ".join(
        part
        for part in (
            html.escape(s.get("channel") or ""),
            html.escape(s.get("date") or ""),
            html.escape(people),
        )
        if part
    )
    chips = _chips(s.get("shared_concepts") or [], hot)
    return _card(
        f'<a href="{link}" target="_blank" rel="noopener" style="color:inherit">'
        f"{html.escape(s['title'] or s['source_id'])}</a>",
        meta
        + (f' <span class="chips">{chips}</span>' if chips else "")
        + (" · matches the brief" if s.get("full_text") else ""),
        f'<button class="act" data-act="attach" data-kind="source" '
        f'data-id="{html.escape(s["source_id"])}">Add</button>',
    )


def render_results(kind: str, items: list[dict], found: int) -> str:
    """The cards a search returns, for the page to put in place of its shortlist."""
    cards = "".join(
        offered_moment_card(item) if kind == "moments" else video_card(item) for item in items
    )
    more = (
        f'<p class="empty">{found - len(items)} more: narrow the search.</p>'
        if found > len(items)
        else ""
    )
    return (cards or '<p class="empty">Nothing matches.</p>') + more


def _search_box(kind: str, placeholder: str) -> str:
    return (
        f'<input type="search" class="search" data-search="{kind}" placeholder="{placeholder}" '
        f'aria-label="{placeholder}">'
    )


def render_list(monologues: list[dict]) -> str:
    rows = (
        "".join(
            _card(
                f'<a href="/monologues/{html.escape(m["name"])}">{html.escape(m["title"] or m["name"])}</a>',
                f"{m['moments']} moments · {len(m['bodies'])} bodies · {m['runs']} runs · {html.escape(m['state'])}"
                + (
                    f" · ingredients {html.escape(m['ingredients'])}"
                    if m.get("ingredients")
                    else ""
                ),
                "",
            )
            for m in monologues
        )
        or '<p class="empty">No monologues yet.</p>'
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>{brand.HEAD}"
        f"<title>Monologues</title><style>{_CSS}</style></head><body data-name=''>"
        f'<header><div class="row"><h1>{brand.ICON}Monologues</h1></div></header><main>'
        f'<section><h2>Start one</h2><div class="row2">'
        '<input type="text" id="newname" placeholder="A short name" aria-label="Name">'
        '<textarea id="newbrief" rows="6" placeholder="What it is about (optional)" aria-label="Brief"></textarea>'
        '<button class="act primary" data-act="new">Start</button></div></section>'
        f"<section><h2>Monologues</h2>{rows}</section></main>"
        '<div class="saved" id="saved">Saved</div>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


def render(data: dict) -> str:
    """`data` is `corpuslm.monologues.candidates`."""
    name = html.escape(data["name"])
    hot = set(data["concepts"])
    attached_moments = (
        "".join(
            _moment_card(
                m,
                f'{html.escape(m["source_id"])} <span class="chips">{_chips(m["concepts"], hot)}</span>',
                f'<button class="act warn" data-act="detach" data-kind="moment" data-id="{html.escape(m["id"])}">Off</button>',
                attached=True,
            )
            for m in data["attached_moments"]
        )
        or '<p class="empty">No moments yet.</p>'
    )
    attached_bodies = (
        "".join(
            _card(
                html.escape(b["name"]),
                html.escape(b["kind"]),
                f'<button class="act warn" data-act="detach" data-kind="body" data-id="{html.escape(b["name"])}">Off</button>',
            )
            for b in data["attached_bodies"]
        )
        or '<p class="empty">No bodies of work yet.</p>'
    )
    moments = (
        "".join(offered_moment_card(m) for m in data["moments"])
        or '<p class="empty">Nothing to offer: approve some moments first.</p>'
    )
    bodies = (
        "".join(
            _card(
                html.escape(b["name"]),
                f"{html.escape(b['kind'])} · {html.escape(b['reason'])}"
                + (" · has a sheet" if b["sheet"] else ""),
                f'<button class="act" data-act="attach" data-kind="body" data-id="{html.escape(b["name"])}">Add</button>',
            )
            for b in data["bodies"]
        )
        or '<p class="empty">No bodies of work yet.</p>'
    )
    sources = (
        "".join(video_card(s, hot) for s in data["sources"])
        or '<p class="empty">No matching videos yet.</p>'
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>{brand.HEAD}"
        f"<title>{html.escape(data['title'] or data['name'])}</title><style>{_CSS}</style></head>"
        f'<body data-name="{name}">'
        f'<header><div class="row"><h1><a href="/monologues" style="color:inherit;text-decoration:none">‹</a> {brand.ICON}'
        f"{html.escape(data['title'] or data['name'])}</h1></div></header><main>"
        '<section><h2>Brief</h2><div class="row2">'
        f'<input type="text" id="title" value="{html.escape(data["title"])}" placeholder="Working title" aria-label="Title">'
        f'<textarea id="brief" rows="12" aria-label="Brief" placeholder="What it is about">{html.escape(data["brief"])}</textarea>'
        '<button class="act" data-act="save">Save</button></div></section>'
        f"<section><h2>Moments attached</h2>{attached_moments}</section>"
        f"<section><h2>Bodies of work attached</h2>{attached_bodies}</section>"
        "<section><h2>Moments to add</h2>"
        + _search_box("moments", "Search all moments")
        + f'<div data-list="moments">{moments}</div></section>'
        + f"<section><h2>Bodies of work used before</h2>{bodies}</section>"
        "<section><h2>Videos that fit</h2>"
        + _search_box("videos", "Search all videos: title, channel, speakers, description")
        + f'<div data-list="videos">{sources}</div></section>'
        + '<section><h2>Run</h2><div class="big">'
        '<button class="act" data-act="estimate">Estimate</button>'
        '<button class="act primary" id="run" data-act="run" hidden>Run</button></div>'
        '<label class="note"><input type="checkbox" id="fresh"> Fresh: drop the prior script</label>'
        '<p class="result" id="estimate"></p><p class="result" id="runout"></p></section>'
        "</main>"
        '<div class="saved" id="saved">Saved</div>'
        f'<script type="application/json" id="data">{json.dumps({"name": data["name"]})}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


__all__ = ["offered_moment_card", "render", "render_list", "render_results", "video_card"]
