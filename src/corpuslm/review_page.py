"""Review page: the self-contained phone page for answering review items, for any source kind.

One page for a workspace with a corpus dropdown (or one corpus when asked), open items only,
one card per distinct span, every tap saved to the artifact's database
(`reviews/<corpus_id>/items/<card_id>`), read back into a decisions file that lands into the SAME
verdict rows the vault note feeds (corpuslm.review). The page is an input surface; the rows are
the record.

`page_data` is the deterministic truth (cards, ids, the keys behind each card); `render` turns it
into HTML; `split` cuts the document into parts under a byte budget (measured, never trimmed);
`decisions_to_verdicts` maps a decisions file back to per-key verdicts, recomputing `page_data`
so the mapping never depends on the page file.

What differs per source kind is a `corpuslm.review.ReviewKind`: how a card links to the moment
in the source (`anchor_url`, None when the source cannot be linked: the page then shows a time
label only), what it calls the source (`source_label`), and the speaker names the picker adds. The
page text, styles and script are the same for every kind, so a kind adds no code path here. The
cards keep the key names `video_id` and `title` for the source's id and label: they are the
format the page script and the decisions file already use.

Where answers go: to the artifact's database when the page is published in a chat (claude.ai), or,
when `window.claude` is absent and a local review server answers (corpuslm.review_server), to that
server's file on this machine. A copy opened from a file saves nothing and says so.

Where user data leaves the machine: into claude.ai, when someone publishes the page in a chat.
This module publishes nothing. The page embeds every open card's text; whether a source kind's
recordings may be put on a page is the owner's decision per kind.

Never imports: cli, clikit, any model transport.

Decisions:
- There is one card per distinct normalized span, and one tap answers every key behind it.
  Enforced by: tests/test_review_page.py::test_one_card_per_distinct_span_across_sources.
- A resolved item never joins an open card. Enforced by:
  tests/test_review_page.py::test_a_resolved_item_never_rejoins_an_open_card.
- A source with no link gets a card with a time label and no link, and the page script says so.
  Enforced by: tests/test_review_page.py::test_a_kind_without_links_gives_time_labels_only.
- An author's speaker name stays first in the picker and the speaker is not to-do. Enforced by:
  tests/test_review_page.py::test_an_author_named_speaker_is_not_to_do_and_is_listed_first.
- The page script uses the local review server only when `window.claude` is absent; with it, the
  calls are exactly the artifact database's. Enforced by:
  tests/test_review_page_script.py::test_without_window_claude_the_page_saves_to_the_local_server_and_reads_it_back.
- A workspace page lists only the corpora its kind's tool reads (a corpus made only of another
  kind's sources is left out). Enforced by:
  tests/test_review_page.py::test_a_workspace_page_lists_only_the_corpora_of_its_kind.
- A page is split by whole corpora at the byte budget, never trimmed. Enforced by:
  tests/test_review_page.py::test_split_cuts_by_whole_corpora_and_never_trims.

Terms:
- **card**: one item on the review page: all review items sharing a normalized span collapse into
  one card; one tap answers them all.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from .files import write_atomic
from .identity import normalize_span
from .review import ReviewKind, reading_options
from .store import Store
from .transcript import loads as transcript_loads
from .transcript import timestamp

DEFAULT_PAGE_BYTES = 5 * 1024 * 1024  # 5 MB opened on the author's phone


def _card_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _context(seg: dict, span: str, applied: str) -> str:
    """The line as the recognizer wrote it, the heard words in bold: the
    author judges what was heard, not the clean-up model's edit (a card once
    read "Causal Set Theory" where "causal set" was heard, 2026-09-29).
    `applied` is the reading now in the text (the pick or a resolution)."""
    raw = seg.get("raw", "")
    if span and span in raw:
        return raw.replace(span, f"**{span}**", 1)
    text = seg["text"]
    for needle in (applied, span):
        if needle and needle in text:
            return text.replace(needle, f"**{span or needle}**", 1)
    return text


def page_data(db, sources_directory: Path, corpus_name: str, *, kind: ReviewKind) -> dict:
    """Cards for one corpus. `sources_directory/<source id>/transcript.json` (and
    `speaker_map.json`) hold each source's documents. The cards keep the key names
    `video_id` and `title`: they are the page's stored and served format."""
    store = Store(db)
    corpus = store.corpus_by_name(corpus_name, exact=True)
    if corpus is None:
        raise KeyError(f"no corpus named {corpus_name!r}")
    videos = store.member_sources(corpus["id"])
    groups: dict[str, dict] = {}
    speakers: list[dict] = []
    open_items = 0
    for order, video in enumerate(videos):
        vid = video["id"]
        t_path = sources_directory / vid / "transcript.json"
        if not t_path.exists():
            continue
        t = transcript_loads(t_path.read_text("utf-8"))
        segments = t["segments"]
        for a in t.get("ambiguities") or []:
            if a["status"] in ("capped", "expired"):
                continue
            # A pick the arbiter applied stays answerable until the author
            # answers it (ytlm.arbiter), but is not to-do.
            jev = a.get("arbiter") if a["status"] == "auto_resolved" else None
            resolved = a["status"] != "pending" and not jev
            if a["status"] == "pending":
                open_items += 1
            seg = segments[a["idx"]]
            norm = normalize_span(a["span"])
            # A resolved item never joins an open card (ytlm.review: never re-asked). A
            # Jev-picked item groups like a pending one, so its card keeps the
            # id it had while pending and an answer saved on the page before
            # Jev ran still lands on it.
            gkey = norm if not resolved else f"{norm}\x1f{vid}\x1f{a['key']}"
            g = groups.get(gkey)
            if g is None:
                g = groups[gkey] = {
                    "id": _card_id("amb", corpus["id"], gkey),
                    "kind": "ambiguity",
                    "span": a["span"],
                    "span_norm": norm,
                    "resolved": resolved,
                    "resolution": a.get("resolution", "") if resolved else "",
                    "candidates": list(dict.fromkeys(a["candidates"])),
                    "pick": a["pick"],
                    "context": _context(seg, a["span"], a.get("resolution") or a["pick"]),
                    "first": {
                        "video_id": vid,
                        "title": kind.source_label(video),
                        "at": timestamp(seg["t0"]),
                        "url": kind.anchor_url(video, seg["t0"]),
                        "order": order,
                    },
                    "items": [],
                    "count": 0,
                }
                if jev:
                    g["jev"] = {
                        "option": a["resolution"],
                        "p": jev["p_combined"],
                        "overrode": jev["overrode"],
                    }
            if jev:
                for o in reading_options(a):
                    if o not in g["candidates"]:
                        g["candidates"].append(o)
            elif g.get("jev"):
                # one of the card's places is still pending: the card is to-do
                del g["jev"]
            for c in a["candidates"]:
                if c not in g["candidates"]:
                    g["candidates"].append(c)
            g["items"].append(
                {
                    "video_id": vid,
                    "key": a["key"],
                    "idx": a["idx"],
                    "at": timestamp(seg["t0"]),
                    "url": kind.anchor_url(video, seg["t0"]),
                    "title": kind.source_label(video),
                }
            )
            g["count"] += 1
        m_path = sources_directory / vid / "speaker_map.json"
        if m_path.exists():
            m = json.loads(m_path.read_text("utf-8"))
            for role, entry in (m.get("roles") or {}).items():
                anchor_idx = entry.get("idx")
                t0 = (
                    segments[anchor_idx]["t0"]
                    if isinstance(anchor_idx, int) and anchor_idx < len(segments)
                    else 0.0
                )
                names: list[str] = []
                if entry.get("name"):
                    names.append(entry["name"])
                if entry.get("status") == "NEEDS_AUTHOR" and entry.get("anchor"):
                    names += [n.strip() for n in re.findall(r"([^;(]+) \(", entry["anchor"])]
                for c in [*(m.get("candidates") or []), *kind.extra_speaker_names(video)]:
                    if c not in names:
                        names.append(c)
                if entry.get("status") == "NEEDS_AUTHOR":
                    open_items += 1
                speakers.append(
                    {
                        "id": _card_id("spk", corpus["id"], vid, role),
                        "kind": "speaker",
                        "video_id": vid,
                        "title": kind.source_label(video),
                        "role": role,
                        "status": entry.get("status", ""),
                        "current": entry.get("name", ""),
                        "key": entry.get("key", f"spk:{role}"),
                        "names": names,
                        "anchor": entry.get("anchor", ""),
                        "at": timestamp(t0),
                        "url": kind.anchor_url(video, t0),
                        "open": entry.get("status") == "NEEDS_AUTHOR",
                        "resolved": entry.get("status") == "AUTHOR",
                        "order": order,
                    }
                )
    # Grouped by video in published order. A cross-video card sits
    # where its FIRST occurrence falls; resolved cards follow the open ones and
    # load behind the page's Resolved filter.
    cards = sorted(
        groups.values(), key=lambda g: (g["resolved"], g["first"]["order"], g["items"][0]["idx"])
    )
    cards += sorted(speakers, key=lambda s: (s["resolved"], not s["open"], s["order"], s["role"]))
    return {
        "corpus": corpus_name,
        "corpus_id": corpus["id"],
        "collection": f"reviews/{corpus['id']}/items",
        "videos": len(videos),
        "open_items": open_items,
        "open_cards": sum(
            1
            for c in cards
            if not c["resolved"] and not c.get("jev") and (c["kind"] == "ambiguity" or c["open"])
        ),
        "resolved_cards": sum(1 for c in cards if c["resolved"]),
        "cards": cards,
    }


def workspace_data(db, sources_directory: Path, *, kind: ReviewKind) -> dict:
    """Every corpus, each as its own page_data, for the one workspace page
    (one link, a corpus dropdown). Corpora with no cards at all are listed but carry
    nothing. Only the corpora the kind's tool reads are listed: a corpus made only of another
    kind's sources is left out."""
    corpora = []
    for name in Store(db).corpus_names(kind.name):
        corpora.append(page_data(db, sources_directory, name, kind=kind))
    corpora.sort(key=lambda c: (-c["open_items"], c["corpus"].lower()))
    return {
        "title": "Corpus review",
        "corpora": corpora,
        "open_items": sum(c["open_items"] for c in corpora),
        "cards": sum(len(c["cards"]) for c in corpora),
    }


# ---- render ----------------------------------------------------------------

_CSS = """
:root{--ground:#f6f4ef;--panel:#fff;--ink:#1c1b18;--muted:#6b675c;--line:#ddd8cc;--band:#2f4858;
--band-ink:#fff;--good:#0a7a3c;--good-soft:#dff3e7;--chip:#ece8de;--focus:#7b4fb0;--warn:#b7410e}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--ground:#15171a;--panel:#20242a;
--ink:#ece9e1;--muted:#a19c8f;--line:#343a42;--band:#3f6076;--good:#5ad38b;--good-soft:#153a25;
--chip:#2c323a;--focus:#c4a3ee;--warn:#ff9b6a}}
:root[data-theme="dark"]{--ground:#15171a;--panel:#20242a;--ink:#ece9e1;--muted:#a19c8f;--line:#343a42;
--band:#3f6076;--good:#5ad38b;--good-soft:#153a25;--chip:#2c323a;--focus:#c4a3ee;--warn:#ff9b6a}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);
font:16px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--band);color:var(--band-ink);padding:10px 14px 8px}
header .row{display:flex;justify-content:space-between;align-items:baseline;max-width:640px;margin:0 auto;gap:12px}
header h1{margin:0;font-size:18px}
#corpus{font:inherit;font-size:16px;font-weight:700;color:var(--band-ink);background:transparent;border:1px solid rgba(255,255,255,.45);border-radius:9px;padding:6px 10px;max-width:70%}
#corpus option{color:#111;background:#fff}
header .count{font-variant-numeric:tabular-nums;font-size:13px;opacity:.9}
.chips{display:flex;gap:6px;max-width:640px;margin:8px auto 0;overflow-x:auto}
.chips button{flex:0 0 auto;border:1px solid rgba(255,255,255,.45);background:transparent;color:var(--band-ink);
border-radius:999px;padding:6px 12px;font:inherit;font-size:13px;font-weight:600}
.chips button[aria-pressed="true"]{background:var(--band-ink);color:var(--band)}
#q{display:block;width:100%;max-width:640px;margin:8px auto 0;font:inherit;font-size:15px;padding:8px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.45);background:rgba(255,255,255,.12);color:var(--band-ink);min-height:40px}
#q::placeholder{color:rgba(255,255,255,.7)}
.empty{max-width:640px;margin:24px auto;padding:0 14px;text-align:center;color:var(--muted)}
.note{max-width:640px;margin:10px auto 0;padding:0 14px;font-size:13px;color:var(--muted)}.note.warn{color:var(--warn)}
main{max-width:640px;margin:0 auto;padding:12px 10px 80px;display:grid;gap:14px}
article{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
article[hidden]{display:none}
.head{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.state{padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--ink);text-transform:none;letter-spacing:0}
.state.done{background:var(--good-soft);color:var(--good)}
.ctx{margin:8px 0;font-size:17px}.ctx b{background:var(--chip);padding:0 3px;border-radius:4px}
.where{font-size:13px;color:var(--muted);margin-bottom:8px}.where a{color:inherit}
.opts{display:grid;gap:8px}
.opts button{font:inherit;font-weight:600;font-size:15px;padding:11px 10px;border-radius:9px;border:1.5px solid var(--line);
background:transparent;color:var(--ink);min-height:44px;text-align:left}
.opts button[aria-pressed="true"]{background:var(--good);border-color:var(--good);color:#fff}
.opts button.jev{border:2px solid var(--good)}
.opts button.jev::before{content:"✓";display:inline-grid;place-items:center;width:20px;height:20px;margin-right:10px;border-radius:5px;background:var(--good);color:#fff;font-size:14px;line-height:1;vertical-align:-2px}
.opts button.jev[aria-pressed="true"]::before{background:#fff;color:var(--good)}
.opts button.skip[aria-pressed="true"]{background:var(--muted);border-color:var(--muted)}
input[type=text]{width:100%;font:inherit;font-size:15px;padding:9px 10px;border-radius:8px;border:1.5px solid var(--line);
background:var(--ground);color:var(--ink);min-height:44px}
details{margin-top:8px}summary{font-size:13px;color:var(--muted);cursor:pointer}
details ul{margin:6px 0 0;padding-left:18px;font-size:13px}details a{color:inherit}
button:focus-visible,input:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.saved{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);background:var(--ink);color:var(--ground);
padding:8px 14px;border-radius:999px;font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none}.saved.on{opacity:.92}
@media (prefers-reduced-motion: reduce){*{transition:none !important}}
"""

_JS = r"""
(function(){
  const DOC = JSON.parse(document.getElementById('data').textContent);
  let DATA = DOC.corpora[0] || {cards: [], collection: ''}; let cards = DATA.cards; let decisions = {};
  let db = null; let applyHint = 'Tell Claude to apply the review.'; let filter = 'todo'; let unsubscribe = null; let current = 0;
  // per corpus index, the ids of cards answered on this page and not yet applied
  // (read once for the corpora not on screen, live for the one that is)
  const answered = {};
  // A tapped pick leaves the To-do view at once (no scrolling back); a typed
  // reading keeps its card on screen until the field loses focus, so the
  // input is never rebuilt mid-phrase. `shown` holds the cards pinned that way.
  let shown = new Set();
  let query = '';
  // everything a card shows, lowercased, for the search box
  function haystack(c){
    const bits = [c.span, c.context, c.resolution, c.role, c.title, c.status, c.current, c.anchor,
      ...(c.candidates||[]), ...(c.names||[]), ...((c.items||[]).map(i => i.title + ' ' + i.at + ' ' + i.key))];
    if (c.first) bits.push(c.first.title, c.first.at);
    const d = decisions[c.id]; if (d && d.verdict) bits.push(d.verdict);
    return bits.filter(Boolean).join(' ').toLowerCase();
  }
  function matches(c){ if (!query) return true; return query.split(/\s+/).filter(Boolean).every(w => haystack(c).includes(w)); }
  const $ = (s, el=document) => el.querySelector(s);
  const esc = s => String(s==null?'':s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  const ctx = s => esc(s).replace(/\*\*(.+?)\*\*/, '<b>$1</b>');
  const label = s => s === 'jev' ? 'Jev picked' : s;
  function state(c){ if (c.resolved) return 'resolved'; const d = decisions[c.id]; if (d && d.verdict) return 'answered'; if (c.jev) return 'jev'; return c.kind==='speaker' && !c.open ? 'named' : 'todo'; }
  function inTab(c){ if (shown.has(c.id)) return true; const s = state(c); if (filter==='all') return true; return s===filter; }
  function visible(c){ return inTab(c) && matches(c); }
  function refilter(){ shown = new Set(); for (const c of cards) if (inTab(c)) shown.add(c.id); }
  function todoCards(c){ return c.cards.filter(x => !x.resolved && !x.jev && (x.kind !== 'speaker' || x.open)); }
  function doneCount(i){
    const todo = todoCards(DOC.corpora[i]);
    if (i === current) return todo.filter(x => state(x) === 'answered').length;
    const ids = answered[i] || new Set(); return todo.filter(x => ids.has(x.id)).length;
  }
  function relabel(){
    // the dropdown counts what is left after the answers saved on this page
    const opts = $('#corpus').options;
    DOC.corpora.forEach((c, i) => {
      const left = todoCards(c).length - doneCount(i); const done = doneCount(i);
      opts[i].textContent = `${c.corpus} · ${left} to do` + (done ? ` · ${done} answered` : '');
    });
  }
  function options(c){
    const d = decisions[c.id] || {}; const list = c.kind==='ambiguity' ? c.candidates : c.names;
    const btn = (label, value, cls='') => `<button class="${cls}" data-v="${esc(value)}" aria-pressed="${d.verdict===value}"${cls==='jev'?` aria-label="${esc(label)}, Jev's pick"`:''}>${esc(label)}</button>`;
    const custom = d.verdict && !list.includes(d.verdict) && d.verdict!=='skip' ? d.verdict : '';
    if (c.resolved) return `<div class="where">Resolved: "${esc(c.resolution || c.current)}"</div>`;
    return `<div class="opts">${list.map(v => btn(v, v, c.jev && v===c.jev.option ? 'jev' : '')).join('')}
      <input type="text" placeholder="other: type the right reading" aria-label="Other" value="${esc(custom)}">
      ${btn('skip', 'skip', 'skip')}</div>`;
  }
  function updateCard(id){
    // in-place refresh of one card's buttons and state chip; the text input is left alone
    const art = $(`article[data-id="${id}"]`); if (!art) return; const c = cards.find(x => x.id===id); const d = decisions[id] || {};
    for (const b of art.querySelectorAll('.opts button')) b.setAttribute('aria-pressed', d.verdict===b.dataset.v);
    const chip = art.querySelector('.state'); const s = state(c); chip.textContent = label(s); chip.className = 'state ' + (s==='answered'||s==='resolved'?'done':'');
    const open = cards.filter(x => state(x)==='todo').length; $('#count').textContent = `${open} to do · ${cards.length} cards`;
    relabel();
  }
  function settle(id){
    // the card leaves the view when it no longer matches the filter
    shown.delete(id); const c = cards.find(x => x.id===id); const art = $(`article[data-id="${id}"]`);
    if (art) art.hidden = !visible(c);
  }
  function render(){
    $('#cards').innerHTML = cards.map(c => {
      const s = state(c);
      __LINK_FUNCTION__
      const where = c.kind==='ambiguity'
        ? `${c.count} place${c.count>1?'s':''} · first at ${link(c.first.url, c.first.at)} in ${esc(c.first.title)}`
        : `${esc(c.role)} in ${esc(c.title)} · ${link(c.url, c.at)}${c.current?` · currently "${esc(c.current)}"`:''}`;
      const jev = c.jev ? `<div class="where">Jev picked "${esc(c.jev.option)}" · ${Math.round(c.jev.p*100)}%${c.jev.overrode?' · changed the clean-up model\'s pick':''}. Tap another option to overrule.</div>` : '';
      const body = c.kind==='ambiguity'
        ? `<div class="ctx">${ctx(c.context)}</div>${jev}`
        : `<div class="ctx">Who is ${esc(c.role)}?${c.anchor?` <span style="font-size:14px;color:var(--muted)">Heard: ${esc(c.anchor)}</span>`:''}</div>`;
      const more = c.kind==='ambiguity' && c.count>1
        ? `<details><summary>All ${c.count} places</summary><ul>${c.items.map(i => `<li>${link(i.url, i.at)} ${esc(i.title)} (${esc(i.key)})</li>`).join('')}</ul></details>` : '';
      return `<article data-id="${c.id}" ${visible(c)?'':'hidden'}>
        <div class="head"><span>${c.kind==='ambiguity'?`heard "${esc(c.span)}"`:'speaker'}</span><span class="state ${s==='answered'?'done':''}">${label(s)}</span></div>
        ${body}<div class="where">${where}</div>${options(c)}${more}</article>`;
    }).join('');
    const open = cards.filter(c => state(c)==='todo').length;
    const hits = query ? ` · ${cards.filter(visible).length} match` : '';
    $('#count').textContent = `${open} to do · ${cards.length} cards${hits}`;
    if (!cards.some(visible)){
      const done = doneCount(current);
      const why = query ? 'No card here matches the search.'
        : filter === 'todo' && done ? `Nothing left to do here: ${done} answered and saved. ${applyHint}`
        : filter === 'todo' ? 'Nothing to do here.' : 'Nothing here yet.';
      $('#cards').insertAdjacentHTML('beforeend', `<p class="empty">${why}</p>`);
    }
    relabel();
  }
  async function save(id){
    const d = decisions[id]; if (!db || !d) return;   // nothing typed or tapped: nothing to save
    try { await db.doc(`${DATA.collection}/${id}`).set({verdict: d.verdict||'', at: new Date().toISOString()}); flash(); }
    catch(e){ $('#status').textContent = 'Save failed for one card: ' + (e && e.message ? e.message : e) + '. Tap it again.'; $('#status').className = 'note warn'; }
  }
  let flashT; function flash(){ const el=$('#saved'); el.classList.add('on'); clearTimeout(flashT); flashT=setTimeout(()=>el.classList.remove('on'), 900); }
  $('#chips').addEventListener('click', e => { const b = e.target.closest('button'); if (!b) return; filter = b.dataset.f;
    for (const x of $('#chips').children) x.setAttribute('aria-pressed', x===b); refilter(); render();
    window.scrollTo({top: 0, behavior: 'auto'}); });
  let qTimer;
  $('#q').addEventListener('input', e => { clearTimeout(qTimer); qTimer = setTimeout(() => { query = e.target.value.trim().toLowerCase(); render(); window.scrollTo({top: 0, behavior: 'auto'}); }, 150); });
  $('#q').addEventListener('search', e => { query = e.target.value.trim().toLowerCase(); render(); });
  $('#cards').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return; const id = b.closest('article').dataset.id;
    const cur = decisions[id] || (decisions[id] = {}); cur.verdict = cur.verdict === b.dataset.v ? '' : b.dataset.v;
    const inp = b.closest('article').querySelector('input[type=text]'); if (inp && cur.verdict !== inp.value.trim()) inp.value = '';
    updateCard(id); save(id); settle(id);
  });
  const timers = {};
  $('#cards').addEventListener('input', e => {
    // typing never re-renders: the input keeps focus; the phrase is saved
    // 1.5 s after the last keystroke and on blur, without rebuilding the card
    const el = e.target; if (!el.matches('input[type=text]')) return; const id = el.closest('article').dataset.id;
    const cur = decisions[id] || (decisions[id] = {}); cur.verdict = el.value.trim();
    shown.add(id); updateCard(id);
    clearTimeout(timers[id]); timers[id] = setTimeout(() => save(id), 1500);
  });
  $('#cards').addEventListener('focusout', e => {
    const el = e.target; if (!el.matches('input[type=text]')) return; const id = el.closest('article').dataset.id;
    clearTimeout(timers[id]); save(id); settle(id);
  });
  function selectCorpus(i){
    answered[current] = new Set(Object.keys(decisions).filter(id => decisions[id].verdict));
    current = i; DATA = DOC.corpora[i]; cards = DATA.cards; decisions = {}; query = ''; $('#q').value = '';
    refilter(); render(); window.scrollTo({top: 0, behavior: 'auto'}); subscribe();
  }
  $('#corpus').addEventListener('change', e => selectCorpus(Number(e.target.value)));
  refilter(); render();
  function subscribe(){
    if (!db) return;
    if (typeof unsubscribe === 'function') { try { unsubscribe(); } catch(e){} }
    unsubscribe = null;
    const status = $('#status');
    let first = true;
    unsubscribe = db.collection(DATA.collection).onSnapshot(snap => {
      let changed = false;
      for (const ch of snap.docChanges()){ if (ch.doc.metadata && ch.doc.metadata.hasPendingWrites) continue; const v = ch.doc.data(); if (!v) continue;
        const mine = decisions[ch.doc.id]; if (mine && mine.verdict === (v.verdict||'')) continue;
        if (document.activeElement && document.activeElement.closest && document.activeElement.closest(`article[data-id="${ch.doc.id}"]`)) continue;
        decisions[ch.doc.id] = {verdict: v.verdict||''}; changed = true; }
      if (first){ first=false; refilter(); render(); status.textContent = `Every tap is saved. Tap again to undo. When you are done, ${applyHint.charAt(0).toLowerCase() + applyHint.slice(1)}`; status.className='note'; }
      else if (changed) for (const id of Object.keys(decisions)) updateCard(id);
    });
  }
  // The local backend: the same calls the page makes on claude.ai's database (doc().set, collection().get
  // and .onSnapshot), answered by the local review server's one API route. Null when no server answers.
  async function localDatabase(){
    const call = async (method, params) => {
      const r = await fetch('api/review', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review': '1'}, body: JSON.stringify({method, params})});
      if (!r.ok) throw new Error('the review server answered ' + r.status);
      return (await r.json()).result;
    };
    try { await call('health', {}); } catch(e){ return null; }
    const snapshot = async collection => {
      const rows = await call('list', {collection});
      const docs = rows.map(row => ({id: row.id, exists: true, data: () => row.data, metadata: {hasPendingWrites: false}}));
      return {docs, docChanges: () => docs.map(doc => ({doc}))};
    };
    return {
      doc: path => ({set: data => call('set', {path, data})}),
      collection: collection => ({
        get: () => snapshot(collection),
        onSnapshot: callback => { snapshot(collection).then(callback).catch(() => {}); return () => {}; },
      }),
    };
  }
  (async () => {
    const status = $('#status');
    if (!window.claude || !window.claude.use){
      // Not inside claude.ai: a page served by a local review server (corpuslm.review_server) keeps its
      // answers there; a copy opened from a file keeps none.
      db = await localDatabase();
      if (!db){ status.textContent = 'Read-only copy: decisions are not saved here. Open the page from claude.ai.'; status.className='note warn'; return; }
      applyHint = 'Run the apply verb to land the answers.';
    } else { db = await claude.use('db'); }
    if (!db){ status.textContent = 'Not connected: decisions are not being saved.'; status.className='note warn'; return; }
    subscribe();
    // one read per other corpus with open cards, so the dropdown counts what is really left
    DOC.corpora.forEach((c, i) => {
      if (i === current || !todoCards(c).length) return;
      db.collection(c.collection).get().then(snap => {
        answered[i] = new Set(snap.docs.filter(d => d.exists && d.data() && d.data().verdict).map(d => d.id)); relabel();
      }).catch(() => {});
    });
  })();
})();
"""


def render(data: dict, *, links_note: str = "", part: int = 1, parts: int = 1) -> str:
    """`data` is a workspace document ({corpora: [...]}) or, for backward
    compatibility, one corpus's page_data (wrapped into a one-corpus document).
    `links_note` is the kind's footnote about its links (trusted markup, empty for none);
    when any card has no link, the page script shows a time label for it."""
    if "corpora" not in data:
        data = {
            "title": "Corpus review",
            "corpora": [data],
            "open_items": data["open_items"],
            "cards": len(data["cards"]),
        }
    title = data.get("title", "Corpus review") + (f" ({part}/{parts})" if parts > 1 else "")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    options = "".join(
        f'<option value="{i}">{html.escape(c["corpus"])} · {c["open_cards"]} to do</option>'
        for i, c in enumerate(data["corpora"])
    )
    return (
        f"<title>{html.escape(title)}</title>\n<style>{_CSS}</style>\n"
        f'<header><div class="row"><select id="corpus" aria-label="Corpus">'
        f"{options}</select>"
        f'<span class="count" id="count"></span></div>'
        f'<div class="chips" id="chips" role="group" aria-label="Show">'
        f'<button data-f="todo" aria-pressed="true">To do</button>'
        f'<button data-f="jev" aria-pressed="false">Jev picked</button>'
        f'<button data-f="answered" aria-pressed="false">Answered</button>'
        f'<button data-f="named" aria-pressed="false">Named speakers</button>'
        f'<button data-f="resolved" aria-pressed="false">Resolved</button>'
        f'<button data-f="all" aria-pressed="false">All</button></div>'
        f'<input type="search" id="q" placeholder="Search this tab" '
        f'aria-label="Search this tab" autocomplete="off"></header>\n'
        f'<p class="note" id="status">Loading…</p>\n'
        + (f'<p class="note">{links_note}</p>\n' if links_note else "")
        + '<main id="cards"></main>\n'
        f'<div class="saved" id="saved">Saved</div>\n'
        f'<script type="application/json" id="data">{payload}</script>\n'
        f"<script>{_script(data)}</script>\n"
    )


def split(data: dict, *, page_bytes: int, links_note: str = "") -> list[dict]:
    """Cut a document into parts under the budget, never trimmed. A
    workspace document splits by whole corpora (a corpus is never divided
    between parts); a one-corpus document splits by cards."""
    if "corpora" in data:
        base = dict(data, corpora=[])
        overhead = len(render(base, links_note=links_note).encode("utf-8"))
        parts: list[list[dict]] = [[]]
        used = overhead
        for corpus in data["corpora"]:
            size = len(json.dumps(corpus, ensure_ascii=False).encode("utf-8")) + 2
            if parts[-1] and used + size > page_bytes:
                parts.append([])
                used = overhead
            parts[-1].append(corpus)
            used += size
        return [
            dict(
                data,
                corpora=group,
                open_items=sum(c["open_items"] for c in group),
                cards=sum(len(c["cards"]) for c in group),
            )
            for group in parts
        ]
    base = dict(data, cards=[])
    overhead = len(render(base, links_note=links_note).encode("utf-8"))
    parts = [[]]
    used = overhead
    for card in data["cards"]:
        size = len(json.dumps(card, ensure_ascii=False).encode("utf-8")) + 2
        if parts[-1] and used + size > page_bytes:
            parts.append([])
            used = overhead
        parts[-1].append(card)
        used += size
    return [dict(data, cards=cards) for cards in parts]


def write_pages(
    data: dict, out: Path, *, page_bytes: int = DEFAULT_PAGE_BYTES, links_note: str = ""
) -> list[dict]:
    out = Path(out)
    chunks = split(data, page_bytes=page_bytes, links_note=links_note)
    written = []
    for n, chunk in enumerate(chunks, start=1):
        path = out if n == 1 else out.with_name(f"{out.stem}-{n}{out.suffix}")
        text = render(chunk, links_note=links_note, part=n, parts=len(chunks))
        write_atomic(path, text)
        n_cards = chunk["cards"] if isinstance(chunk.get("cards"), int) else len(chunk["cards"])
        written.append({"path": str(path), "cards": n_cards, "bytes": len(text.encode("utf-8"))})
    return written


# ---- decisions file → per-key verdicts ---------------------------------------


def decisions_to_verdicts(
    db, sources_directory: Path, corpus_name: str, decisions: dict, *, kind: ReviewKind
) -> tuple[list[dict], list[str]]:
    """Map a decisions file ({corpus_id, decisions: {card_id: {verdict}}}) to
    per-key verdicts by recomputing the page data. Returns (verdicts,
    unknown card ids)."""
    data = page_data(db, sources_directory, corpus_name, kind=kind)
    if decisions.get("corpus_id") and decisions["corpus_id"] != data["corpus_id"]:
        raise ValueError(
            f"decisions are for corpus {decisions['corpus_id']}, not {data['corpus_id']}"
        )
    entries = decisions.get("decisions")
    if not isinstance(entries, dict):
        raise ValueError(
            "decisions file: 'decisions' must be an object of card id → {\"verdict\": ...}"
        )
    by_id = {c["id"]: c for c in data["cards"]}
    out: list[dict] = []
    unknown: list[str] = []
    for card_id, d in entries.items():
        if not isinstance(d, dict) or not isinstance(d.get("verdict", ""), str):
            raise ValueError(f'decisions file: entry {card_id!r} must be {{"verdict": str}}')
        verdict = d.get("verdict", "").strip()
        if not verdict:
            continue
        card = by_id.get(card_id)
        if card is None or card.get("resolved"):
            unknown.append(card_id)
            continue
        if card["kind"] == "ambiguity":
            for item in card["items"]:
                out.append(
                    {
                        "video_id": item["video_id"],
                        "key": item["key"],
                        "kind": "ambiguity",
                        "verdict": verdict,
                        "span": card["span"],
                        "idx": item["idx"],
                    }
                )
        else:
            out.append(
                {
                    "video_id": card["video_id"],
                    "key": card["key"],
                    "kind": "speaker",
                    "verdict": verdict,
                    "role": card["role"],
                }
            )
    return out, unknown


def land_page_verdicts(
    db, verdicts: list[dict], *, created_by: str, source: str = "phone page"
) -> list[str]:
    """Record each verdict `decisions_to_verdicts` returned as a row with provenance
    (corpuslm.review.record_verdict), through the Store; returns `<source id>:<key>` for each.
    An answer given again adds no row."""
    from .review import record_verdict

    landed = []
    for v in verdicts:
        record_verdict(
            db,
            source_id=v["video_id"],
            key=v["key"],
            kind=v["kind"],
            verdict=v["verdict"],
            span=v.get("span", ""),
            role=v.get("role", ""),
            created_by=created_by,
            source=source,
            idx=v.get("idx"),
        )
        landed.append(f"{v['video_id']}:{v['key']}")
    return landed


# The link line of the page script. The anchored line is the page's original; the time-label line
# is used only when some card has no link, so a page of linked cards is byte-identical to before.
_LINK_ANCHORED = (
    'const link = (url, at) => `<a href="${esc(url)}" target="_blank" '
    'rel="noopener">▶ ${esc(at)}</a>`;'
)
_LINK_OR_TIME_LABEL = (
    'const link = (url, at) => url ? `<a href="${esc(url)}" target="_blank" '
    'rel="noopener">▶ ${esc(at)}</a>` : `<span>${esc(at)}</span>`;'
)


def _has_unlinked_card(data: dict) -> bool:
    corpora = data["corpora"] if "corpora" in data else [data]
    for corpus in corpora:
        for card in corpus["cards"]:
            first = card.get("first") or {}
            places = [
                card.get("url", ""),
                first.get("url", ""),
                *(i.get("url", "") for i in card.get("items", [])),
            ]
            if any(url is None for url in places):
                return True
    return False


def _script(data: dict) -> str:
    link = _LINK_OR_TIME_LABEL if _has_unlinked_card(data) else _LINK_ANCHORED
    return _JS.replace("__LINK_FUNCTION__", link)


__all__ = [
    "DEFAULT_PAGE_BYTES",
    "decisions_to_verdicts",
    "land_page_verdicts",
    "page_data",
    "render",
    "split",
    "workspace_data",
    "write_pages",
]
