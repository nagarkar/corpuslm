"""Teleprompter page: a monologue's settled script in large type, scrolled by tap
(corpuslm.monologues, nagarkar/corpuslm#52).

One self-contained page at `/teleprompter/<monologue name>` on the review server: the title, then
the script's paragraphs at a size readable from a studio distance, scrolling on its own once
tapped, faster or slower with two buttons, paused by a second tap. The page shows the settled
text of the latest script tab, so edits made in the Doc are what is read. No input, no saving.

Never imports: cli, clikit, db, store, any model transport.
"""

from __future__ import annotations

import html

from . import brand

_CSS = """
:root{--ground:#000;--ink:#f3efe6;--muted:#9a948a;--accent:#5ad38b}
*{box-sizing:border-box}html,body{margin:0;background:var(--ground);color:var(--ink);
font:28px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
header{position:fixed;top:0;left:0;right:0;display:flex;gap:8px;justify-content:space-between;align-items:center;
padding:8px 12px;background:rgba(0,0,0,.75);font-size:14px;color:var(--muted);z-index:2}
header button{font:inherit;font-size:16px;font-weight:700;min-width:44px;min-height:44px;border-radius:10px;
border:1px solid var(--muted);background:transparent;color:var(--ink)}
main{max-width:860px;margin:0 auto;padding:40vh 24px 60vh}
h1{font-size:1.1em;color:var(--muted);margin:0 0 1em;font-weight:600}
p{margin:0 0 1.1em}
.hint{position:fixed;bottom:0;left:0;right:0;margin:0;padding:10px;text-align:center;font-size:14px;color:var(--muted);background:rgba(0,0,0,.8)}
@media (min-width:900px){html,body{font-size:40px}}
"""

_SCRIPT = """
(function(){
  var speed=1.0,running=false,last=null,pos=window.scrollY;
  var rate=document.getElementById('rate');
  function show(){rate.textContent=(running?'scrolling ':'paused ')+speed.toFixed(1)+'x'}
  function step(t){if(!running){last=null;return}
    if(last!==null){pos+=(t-last)/1000*40*speed;window.scrollTo(0,pos)}
    last=t;requestAnimationFrame(step)}
  function toggle(){running=!running;pos=window.scrollY;show();if(running)requestAnimationFrame(step)}
  document.getElementById('faster').addEventListener('click',function(e){e.stopPropagation();speed=Math.min(3,speed+0.2);show()});
  document.getElementById('slower').addEventListener('click',function(e){e.stopPropagation();speed=Math.max(0.2,speed-0.2);show()});
  document.getElementById('top').addEventListener('click',function(e){e.stopPropagation();running=false;pos=0;window.scrollTo(0,0);show()});
  document.body.addEventListener('click',function(e){if(e.target.tagName!=='BUTTON')toggle()});
  document.addEventListener('keydown',function(e){if(e.code==='Space'){e.preventDefault();toggle()}});
  show();
})();
"""


def render(name: str, title: str, text: str) -> str:
    """`text` is the settled script with paragraphs separated by blank lines; the heading line,
    if any, is left out."""
    paragraphs = [
        block.strip()
        for block in text.split("\n\n")
        if block.strip() and not block.strip().startswith("# ")
    ]
    body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs) or (
        "<p>No script yet.</p>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>{brand.HEAD}"
        f"<title>{html.escape(title or name)}</title><style>{_CSS}</style></head><body>"
        f'<header><span id="rate"></span><span>'
        f'<button id="slower" aria-label="Slower">−</button> '
        f'<button id="faster" aria-label="Faster">+</button> '
        f'<button id="top" aria-label="Back to the top">↑</button></span></header>'
        f"<main><h1>{html.escape(title or name)}</h1>{body}</main>"
        '<p class="hint">Tap to scroll, tap again to pause</p>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


__all__ = ["render"]
