// Drives the review page served by a local review server (tests/test_review_page_script.py).
// Usage: node local_page.js <base url>  → prints one JSON report on stdout.
// There is no window.claude: the page must pick the local backend and save through the server.
const { JSDOM } = require('jsdom');

const base = process.argv[2];
(async () => {
  const html = await (await fetch(base + '/')).text();
  const dom = new JSDOM(html, {
    url: base + '/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.scrollTo = () => {};
      window.fetch = (path, options) => fetch(new URL(path, base + '/'), options);
    },
  });
  const { window } = dom;
  const doc = window.document;
  const tick = () => new Promise((r) => setTimeout(r, 60));
  // wait for the page to settle instead of guessing a delay: a loaded machine answers slowly
  const until = async (test) => { for (let i = 0; i < 100 && !test(); i++) await tick(); };
  const shown = () => [...doc.querySelectorAll('article')].filter((a) => !a.hidden).map((a) => a.dataset.id);
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const report = {};
  await until(() => doc.getElementById('status').textContent.startsWith('Every tap is saved'));
  report.status = doc.getElementById('status').textContent;
  report.todo = shown();
  report.where = [...doc.querySelectorAll('.where')].map((w) => w.textContent);
  report.links = doc.querySelectorAll('article a').length;
  const card = [...doc.querySelectorAll('article')].find((a) => a.querySelector('.head span').textContent === 'speaker');
  report.picker = [...card.querySelectorAll('.candidate-buttons button')].map((b) => b.dataset.v);
  const pick = [...card.querySelectorAll('.candidate-buttons button')].find((b) => b.dataset.v === 'Ren');
  click(pick);
  await until(() => pick.getAttribute('aria-pressed') === 'true');
  // the tap is saved when the server lists it
  const collection = JSON.parse(doc.getElementById('data').textContent).corpora[0].collection;
  const saved = async () => (await (await fetch(base + '/api/review', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Review': '1' }, body: JSON.stringify({ method: 'list', params: { collection } }) })).json()).result.length;
  for (let i = 0; i < 100 && (await saved()) === 0; i++) await tick();
  report.pressed = pick.getAttribute('aria-pressed');
  // a second page load reads the saved answer back
  const again = new JSDOM(await (await fetch(base + '/')).text(), {
    url: base + '/', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) { w.scrollTo = () => {}; w.fetch = (p, o) => fetch(new URL(p, base + '/'), o); },
  });
  await until(() => again.window.document.getElementById('status').textContent.startsWith('Every tap is saved'));
  const card2 = [...again.window.document.querySelectorAll('article')].find((a) => a.dataset.id === card.dataset.id);
  report.reloaded_pressed = [...card2.querySelectorAll('.candidate-buttons button')].find((b) => b.dataset.v === 'Ren').getAttribute('aria-pressed');
  report.card_id = card.dataset.id;
  process.stdout.write(JSON.stringify(report));
  window.close(); again.window.close();
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
