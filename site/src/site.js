// Charts, the model table, the language picker and the "also in your language" line.
// Numbers live here (they are the same in every language); words come from the page's #t block.
(function () {
  const T = JSON.parse(document.getElementById('t').textContent);
  const LANGS = JSON.parse(document.getElementById('langs').textContent);
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const fill = (s, o) => s.replace(/\{(\w+)\}/g, (_, k) => o[k]);
  const tip = document.getElementById('tip');
  function showTip(e, html) { tip.innerHTML = html; tip.hidden = false; moveTip(e); }
  function moveTip(e) {
    const x = Math.max(8, Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8));
    const y = Math.max(8, e.clientY - tip.offsetHeight - 10);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function hideTip() { tip.hidden = true; }
  const NS = 'http://www.w3.org/2000/svg';
  function el(tag, attrs, parent) { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); if (parent) parent.appendChild(n); return n; }
  function txt(parent, x, y, s, cls, anchor) { const t = el('text', { x, y, class: cls || '', 'text-anchor': anchor || 'start', 'dominant-baseline': 'middle' }, parent); t.textContent = s; return t; }
  function hover(node, html) {
    node.addEventListener('mouseenter', (e) => showTip(e, html));
    node.addEventListener('mousemove', moveTip);
    node.addEventListener('mouseleave', hideTip);
  }
  // a horizontal bar with a 4px rounded data end and a square base
  function barPath(x0, y, w, h) {
    const r = Math.min(4, w, h / 2);
    return `M${x0},${y} H${x0 + w - r} Q${x0 + w},${y} ${x0 + w},${y + r} V${y + h - r} Q${x0 + w},${y + h} ${x0 + w - r},${y + h} H${x0} Z`;
  }
  const money = (v) => '$' + v.toFixed(v < 0.001 ? 5 : 4);
  const pct = (v) => Math.round(v * 100) + '%';
  function legend(id, items) {
    document.getElementById(id).innerHTML = items.map(([n, v]) => `<span><i style="background:var(${v})"></i>${n}</span>`).join('');
  }

  // ---------- savings ----------
  const phaseColors = ['--s1', '--s2', '--s3'];
  const seats = [
    ['Pip', [0.00469, 0.00346, 0.00031], [10, 6, 2]],
    ['Corra', [0.00109, 0.00124, 0.00075], [13, 9, 3]],
    ['Ssareth', [0.00223, 0.00127, 0.00114], [10, 14, 5]],
  ];
  function savings() {
    const S = T.savings;
    legend('leg-savings', S.phases.map((p, i) => [p, phaseColors[i]]));
    const host = document.getElementById('chart-savings'); host.innerHTML = '';
    const W = 760, L = 78, R = 120, bh = 16, gap = 2, gg = 22, top = 10;
    const H = top + seats.length * (3 * bh + 2 * gap) + (seats.length - 1) * gg + 34;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}` }, host);
    const max = 0.005, x = (v) => L + (v / max) * (W - L - R);
    const plotBottom = H - 24;
    for (let t = 0; t <= max + 1e-9; t += 0.001) {
      el('line', { x1: x(t), x2: x(t), y1: top - 4, y2: plotBottom, class: 'gridline' }, svg);
      txt(svg, x(t), plotBottom + 14, '$' + t.toFixed(3), '', 'middle');
    }
    let y = top;
    seats.forEach(([name, vals, ns]) => {
      const gh = 3 * bh + 2 * gap;
      txt(svg, L - 10, y + gh / 2, name, 'cat', 'end');
      vals.forEach((v, i) => {
        const yy = y + i * (bh + gap);
        el('path', { d: barPath(L, yy, Math.max(2, x(v) - L), bh), fill: css(phaseColors[i]) }, svg);
        txt(svg, x(v) + 6, yy + bh / 2, `${money(v)}  n=${ns[i]}`, 'val');
        const hit = el('rect', { x: L, y: yy, width: W - L - R + 100, height: bh, fill: 'transparent' }, svg);
        hover(hit, `<b>${name}</b><br>${S.phases[i]}<br>${fill(S.tip, { cost: money(v), n: ns[i] })}`);
      });
      y += gh + gg;
    });
    el('line', { x1: L, x2: L, y1: top - 4, y2: plotBottom, class: 'axis' }, svg);
  }

  // ---------- models ----------
  const models = [
    // name, slept before (null = not run), slept after, calls, cost, median s
    ['deepseek-v4-flash', 1.0, 1.0, 8.0, 0.0032, 31],
    ['claude-haiku-5.5', null, 1.0, 3.2, 0.0021, 7],
    ['gpt-6-luna', 1.0, 1.0, 5.8, 0.0021, 10],
    ['mimo-v2.6-flash', 0.75, 1.0, 10.5, 0.0041, 11],
    ['deepseek-v4.1-flash', 0.75, 1.0, 8.2, 0.0060, 36],
    ['hy4-flash', 0.25, 0.75, 15.2, 0.0033, 92],
    ['glm-5.3-flashx', 0.5, 1.0, 5.5, 0.0120, 63],
    ['minimax-m3.1-flash', 0.25, 1.0, 21.2, 0.0121, 115],
  ];
  function modelTable() {
    document.getElementById('model-rows').innerHTML = models.map(m =>
      `<tr><td><code>${m[0]}</code></td><td class="num">${pct(m[2])}</td><td class="num">${m[3].toFixed(1)}</td><td class="num">${money(m[4])}</td><td class="num">${m[5]} s</td><td>${T.models.notes[m[0]] || ''}</td></tr>`).join('');
  }
  function slept() {
    const M = T.models;
    legend('leg-slept', [[M.slept_legend[0], '--s2'], [M.slept_legend[1], '--s1']]);
    const host = document.getElementById('chart-slept'); host.innerHTML = '';
    const W = 520, L = 140, R = 40, row = 28, top = 8;
    const H = top + models.length * row + 30;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}` }, host);
    const x = (v) => L + v * (W - L - R), plotBottom = top + models.length * row;
    [0, 0.25, 0.5, 0.75, 1].forEach(t => { el('line', { x1: x(t), x2: x(t), y1: top, y2: plotBottom, class: 'gridline' }, svg); txt(svg, x(t), plotBottom + 14, pct(t), '', 'middle'); });
    models.forEach((m, i) => {
      const cy = top + i * row + row / 2;
      txt(svg, L - 10, cy, m[0], 'cat', 'end');
      if (m[1] != null && m[1] !== m[2]) el('line', { x1: x(m[1]), x2: x(m[2]), y1: cy, y2: cy, stroke: css('--rule'), 'stroke-width': 2 }, svg);
      if (m[1] != null) el('circle', { cx: x(m[1]), cy, r: 6, fill: css('--s2'), stroke: css('--surface'), 'stroke-width': 2 }, svg);
      el('circle', { cx: x(m[2]), cy, r: 6, fill: css('--s1'), stroke: css('--surface'), 'stroke-width': 2 }, svg);
      const hit = el('rect', { x: 0, y: cy - row / 2, width: W, height: row, fill: 'transparent' }, svg);
      hover(hit, `<b>${m[0]}</b><br>${fill(M.slept_tip_before, { v: m[1] == null ? M.not_run : pct(m[1]) })}<br>${fill(M.slept_tip_after, { v: pct(m[2]) })}`);
    });
  }
  function cost() {
    const M = T.models;
    const host = document.getElementById('chart-cost'); host.innerHTML = '';
    const sorted = [...models].sort((a, b) => a[4] - b[4]);
    const W = 520, L = 140, R = 70, row = 28, bh = 16, top = 8;
    const H = top + sorted.length * row + 30;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}` }, host);
    const max = 0.014, x = (v) => L + (v / max) * (W - L - R), plotBottom = top + sorted.length * row;
    [0, 0.004, 0.008, 0.012].forEach(t => { el('line', { x1: x(t), x2: x(t), y1: top, y2: plotBottom, class: 'gridline' }, svg); txt(svg, x(t), plotBottom + 14, '$' + t.toFixed(3), '', 'middle'); });
    sorted.forEach((m, i) => {
      const yy = top + i * row + (row - bh) / 2;
      txt(svg, L - 10, yy + bh / 2, m[0], 'cat', 'end');
      el('path', { d: barPath(L, yy, Math.max(2, x(m[4]) - L), bh), fill: css('--s1') }, svg);
      txt(svg, x(m[4]) + 6, yy + bh / 2, money(m[4]), 'val');
      const hit = el('rect', { x: 0, y: yy - 4, width: W, height: bh + 8, fill: 'transparent' }, svg);
      hover(hit, `<b>${m[0]}</b><br>${fill(M.cost_tip, { cost: money(m[4]), calls: m[3].toFixed(1), s: m[5] })}`);
    });
    el('line', { x1: L, x2: L, y1: top, y2: plotBottom, class: 'axis' }, svg);
  }

  // ---------- Jev ----------
  const jev = [[11, 11], [11, 11], [13, 14], [14, 15], [15, 16], [9, 12]];
  function jevChart() {
    const J = T.jev;
    const host = document.getElementById('chart-jev'); host.innerHTML = '';
    const W = 520, R = 70, row = 30, bh = 16, top = 8;
    const H = top + jev.length * row + 30;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}` }, host);
    // the label column fits the longest translated name
    const probe = J.names.map(n => txt(svg, 0, 0, n, 'cat'));
    const L = Math.max(130, ...probe.map(t => t.getComputedTextLength() + 16));
    probe.forEach(t => t.remove());
    const x = (v) => L + v * (W - L - R), plotBottom = top + jev.length * row;
    [0, 0.5, 1].forEach(t => { el('line', { x1: x(t), x2: x(t), y1: top, y2: plotBottom, class: 'gridline' }, svg); txt(svg, x(t), plotBottom + 14, pct(t), '', 'middle'); });
    jev.forEach(([k, n], i) => {
      const yy = top + i * row + (row - bh) / 2, v = k / n;
      txt(svg, L - 10, yy + bh / 2, J.names[i], 'cat', 'end');
      el('path', { d: barPath(L, yy, Math.max(2, x(v) - L), bh), fill: css('--s1') }, svg);
      txt(svg, x(v) + 6, yy + bh / 2, `${k}/${n}`, 'val');
      const hit = el('rect', { x: 0, y: yy - 4, width: W, height: bh + 8, fill: 'transparent' }, svg);
      hover(hit, `<b>${J.names[i]}</b><br>${fill(J.tip, { k, n })} (${pct(v)})`);
    });
    el('line', { x1: L, x2: L, y1: top, y2: plotBottom, class: 'axis' }, svg);
  }

  function drawAll() { savings(); slept(); cost(); jevChart(); }
  modelTable();
  drawAll();
  // the bars take their colors from the theme tokens: redraw when the theme changes
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', drawAll);
  new MutationObserver(drawAll).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  // ---------- languages ----------
  const here = document.documentElement.dataset.code;
  const root = document.querySelector('link[rel=stylesheet][href$="site.css"]').getAttribute('href').replace('site.css', '');
  const pick = (code) => { try { localStorage.setItem('bh-lang', code); } catch (e) {} };
  document.getElementById('lang').addEventListener('change', (e) => {
    pick(e.target.value);
    location.href = root + LANGS[e.target.value].path + location.hash;
  });
  // on a first visit, offer the reader's own language once; never redirect
  let chosen = null;
  try { chosen = localStorage.getItem('bh-lang'); } catch (e) {}
  if (!chosen) {
    const want = (navigator.languages || [navigator.language || '']).map(l => l.toLowerCase().slice(0, 2)).find(c => LANGS[c]);
    if (want && want !== here) {
      const bar = document.getElementById('suggest');
      document.getElementById('suggest-text').innerHTML = `<a href="${root + LANGS[want].path}" lang="${want}" dir="${LANGS[want].dir}">${LANGS[want].suggest}</a>`;
      bar.hidden = false;
      document.getElementById('suggest-close').addEventListener('click', () => { pick(here); bar.hidden = true; });
    }
  }
})();
