// Drives a review page whose corpus shows one entry per source (tests/test_review_page_script.py).
// Usage: node entries_page.js <base url>  → prints one JSON report on stdout.
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
  const until = async (test) => { for (let i = 0; i < 100 && !test(); i++) await tick(); };
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const entries = () => [...doc.querySelectorAll('section.entry')].filter((s) => !s.hidden);
  const headings = () => entries().map((s) => s.querySelector('h2').textContent.trim());
  // a card is on screen when neither it nor its entry is hidden and its entry is not folded
  const cardsOn = (el) => [...el.querySelectorAll('article')].filter((a) => !a.hidden && !a.closest('.entry.shut')).length;
  const type = async (text) => {
    const box = doc.getElementById('heading-filter');
    box.value = text;
    box.dispatchEvent(new window.Event('input', { bubbles: true }));
    await tick(); await tick(); await tick(); await tick();
  };
  const report = {};
  await until(() => doc.getElementById('status').textContent.startsWith('Every tap is saved'));
  report.bar_hidden = doc.getElementById('entries').hidden;
  report.newest_first = headings();
  report.facts = entries().map((s) => s.querySelector('.facts').textContent.trim());
  report.cards_per_entry = entries().map(cardsOn);
  report.loose_cards = [...doc.querySelectorAll('article')].filter((a) => !a.closest('section.entry')).length;
  click(doc.getElementById('sort'));
  report.oldest_first = headings();
  report.sort_label = doc.getElementById('sort').textContent.trim();
  await type('garden');
  report.filtered = headings();
  report.filtered_cards = cardsOn(doc);
  report.filtered_count = doc.getElementById('count').textContent;
  await type('2026-10-03');
  report.filtered_by_date = headings();
  await type('no such heading');
  report.no_match = doc.querySelector('.empty').textContent;
  await type('');
  click(doc.getElementById('fold'));
  report.folded = headings();
  report.folded_cards = cardsOn(doc);
  click(entries()[0].querySelector('h2 button'));
  report.opened_cards = cardsOn(doc);
  click(doc.getElementById('fold'));
  const garden = entries().find((s) => s.querySelector('h2').textContent.trim() === 'Garden notes');
  click(garden.querySelector('.candidate-buttons button'));
  await until(() => headings().length === 1);
  report.after_answer = headings();
  // the tap's save ends before the window closes
  await until(() => doc.getElementById('saved').classList.contains('on'));
  process.stdout.write(JSON.stringify(report));
  window.close();
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
