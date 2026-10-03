/* ===========================================================================
   Sales Channel Dashboard - India state map.

   Kept in its own file on purpose: dashboard.js is 4,600+ lines and nothing in
   it is changed. This file only READS what that file already computed - the
   channel rows in sc2Rows, via the existing getFilteredChannelRows() - and
   wraps renderSlideTwo() from the outside so the map redraws whenever the
   channel cards do.

   The map is plain SVG. d3-geo turns the state outlines into <path> data; the
   raised edge is nine copies of those same paths stacked one step apart
   underneath the real ones, and one CSS drop-shadow under the stack. There is
   no 3D library involved.
   =========================================================================== */
(function () {
  /* Quantity accessor shared with the dashboard (dashboard.js, loaded before this
     file). Falls back to litres if it is ever missing, so the map still draws. */
  var QTY = (typeof window.QTY === 'function') ? window.QTY
          : function (r) { return Number(r && r.liter) || 0; };
  'use strict';

  var VB_W = 600, VB_H = 680, DEPTH = 9;

  var wrapEl = document.getElementById('rsmWrap');
  if (!wrapEl) return;

  var svgEl   = document.getElementById('rsmSvg');
  var emptyEl = document.getElementById('rsmEmpty');
  var tipEl   = document.getElementById('rsmTip');
  var listEl  = document.getElementById('rsmList');
  var chipEl  = document.getElementById('rsmChip');
  var scaleEl = document.getElementById('rsmScale');
  var GEO_URL = wrapEl.getAttribute('data-geo-url') || '';

  /* SAP spells several states its own way. dashboard.js already folds the
     common ones with canonState(); these extras cover the spellings that only
     matter once the name has to match the map file. */
  var ALIASES = {
    'ORISSA': 'ODISHA', 'PONDICHERRY': 'PUDUCHERRY', 'UTTARANCHAL': 'UTTARAKHAND',
    'KERELA': 'KERALA', 'CHHATISGARH': 'CHHATTISGARH', 'TAMILNADU': 'TAMIL NADU',
    'NCT OF DELHI': 'DELHI', 'NEW DELHI': 'DELHI',
    'ANDAMAN AND NICOBAR': 'ANDAMAN AND NICOBAR ISLANDS',
    'DADRA AND NAGAR HAVELI': 'DADRA AND NAGAR HAVELI AND DAMAN AND DIU',
    'DAMAN AND DIU': 'DADRA AND NAGAR HAVELI AND DAMAN AND DIU'
  };
  function norm(raw) {
    var s = String(raw || '').trim().toUpperCase()
              .replace(/&/g, ' AND ').replace(/\s+/g, ' ');
    return ALIASES[s] || s;
  }
  function titleCase(s) {
    return String(s || '').toLowerCase().replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }
  function num(n) {
    if (typeof fNp === 'function') return fNp(n, 0);
    return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 }).format(n || 0);
  }
  function money(n) {
    if (typeof fRsShort === 'function') return fRsShort(n);
    return '₹' + new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 }).format(Math.round(n || 0));
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
             .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* ---- the read-out in the map's empty margins ------------------------------
     The map colours DONE litres, and the list beside it already names the states
     and their litres - so repeating counts and top-state here said nothing new.
     These two panels carry what is genuinely missing from the page now that the
     channel cards are hidden:
       * where the OPEN ORDERS are (the map cannot show pending)
       * which states earn the most and least per litre (realise) */
  function pctBar(v, max) {
    return '<span class="rsm-mini-bar"><i style="width:' +
      Math.max(2, max ? Math.round(v / max * 100) : 0) + '%"></i></span>';
  }

  /* `saleRows` is passed in: it lives inside build(), not at this scope. */
  function drawStats(byState, extra, saleRows) {
    var pendBox = document.getElementById('rsmStats');
    var rateBox = document.getElementById('rsmZones');
    if (!pendBox || !rateBox) return;

    /* Fold Order-in-Hand and revenue onto the same state keys the map uses. */
    var st = {};
    function cell(k) {
      if (!st[k]) st[k] = { name: k, done: 0, doneValue: 0, oih: 0 };
      return st[k];
    }
    Object.keys(byState).forEach(function (k) { cell(k).done = byState[k]; });
    (saleRows || []).forEach(function (r) {
      var k = norm(typeof canonState === 'function' ? canonState(r.state) : r.state);
      if (!k || k === 'UNKNOWN') return;
      cell(k).doneValue += Number(r.line_total) || 0;
    });
    var segNow = (typeof sc2Seg === 'function') ? String(sc2Seg() || '').toUpperCase() : '';
    ((extra && extra.oih) || []).forEach(function (x) {
      if (activeChannel && !inChannel(x.main_group)) return;
      if (segNow && String(x.u_type || '').toUpperCase() !== segNow) return;
      var k = norm(typeof canonState === 'function' ? canonState(x.state) : x.state);
      if (!k || k === 'UNKNOWN') return;
      cell(k).oih += Number(x.open_qty) || 0;
    });
    var all = Object.keys(st).map(function (k) { return st[k]; });

    /* ---- open orders ---- */
    var pend = all.filter(function (r) { return r.oih > 0; })
                  .sort(function (a, b) { return b.oih - a.oih; });
    var pendTot = pend.reduce(function (a, r) { return a + r.oih; }, 0);
    var doneTot = all.reduce(function (a, r) { return a + r.done; }, 0);
    var fill = (doneTot + pendTot) > 0 ? Math.round(doneTot / (doneTot + pendTot) * 100) : 0;

    if (!pendTot) {
      pendBox.innerHTML = '<div class="rsm-mini"><h4>Open orders</h4>' +
        '<p class="rsm-mini-none">Nothing pending in this range.</p></div>';
    } else {
      var pmax = pend[0].oih;
      pendBox.innerHTML = '<div class="rsm-mini">' +
        '<h4>Open orders <em>' + num(pendTot) + ' L</em></h4>' +
        '<div class="rsm-fill"><span>' + fill + '% delivered</span>' +
          '<span class="rsm-mini-bar wide"><i style="width:' + fill + '%"></i></span></div>' +
        pend.slice(0, 5).map(function (r) {
          return '<div class="rsm-mini-row">' +
                   '<span class="rsm-mini-n">' + esc(titleCase(r.name)) + '</span>' +
                   '<span class="rsm-mini-v">' + num(r.oih) + '</span>' +
                   pctBar(r.oih, pmax) +
                 '</div>';
        }).join('') +
        (pend.length > 5 ? '<p class="rsm-mini-more">+' + (pend.length - 5) + ' more states</p>' : '') +
      '</div>';
    }

    /* ---- realise, best and weakest ---- */
    var rate = all.filter(function (r) { return r.done > 0 && r.doneValue > 0; })
                  .map(function (r) { return { name: r.name, rate: r.doneValue / r.done, done: r.done }; })
                  .sort(function (a, b) { return b.rate - a.rate; });
    if (rate.length < 2) {
      rateBox.innerHTML = '<div class="rsm-mini"><h4>Realise per litre</h4>' +
        '<p class="rsm-mini-none">Not enough states to compare.</p></div>';
      return;
    }
    var avg = all.reduce(function (a, r) { return a + r.doneValue; }, 0) / (doneTot || 1);
    function rateRow(r, cls) {
      return '<div class="rsm-mini-row">' +
               '<span class="rsm-mini-n">' + esc(titleCase(r.name)) + '</span>' +
               '<span class="rsm-mini-v ' + cls + '">₹' +
                 (typeof fNp === 'function' ? fNp(r.rate, 2) : r.rate.toFixed(2)) + '</span>' +
             '</div>';
    }
    var top = rate.slice(0, 3), low = rate.slice(-3).reverse();
    rateBox.innerHTML = '<div class="rsm-mini">' +
      '<h4>Realise per litre <em>avg ₹' +
        (typeof fNp === 'function' ? fNp(avg, 2) : avg.toFixed(2)) + '</em></h4>' +
      '<p class="rsm-mini-lbl">Highest</p>' + top.map(function (r) { return rateRow(r, 'up'); }).join('') +
      '<p class="rsm-mini-lbl">Lowest</p>' + low.map(function (r) { return rateRow(r, 'down'); }).join('') +
    '</div>';
  }

  /* ---- channel-aware map accent -------------------------------------------
     The map used to be the Ecom green whatever channel was picked, while the
     channel cards, legend and realise dots each used the channel's own colour.
     Now the map follows: pick E-Commerce and the states shade in blue, its list
     bars go blue, the scale goes blue. "All channels" keeps the green - green is
     the established colour for "everything" on this page. */
  /* One ramp per channel, derived from the channel's own --ch-* colour (the same
     hex its card and legend dot use), so map and cards can never disagree.
     "All channels" has no single hue: each state takes the colour of the channel
     that sold the most there, and the page chrome (chip, scale, list defaults)
     goes neutral slate rather than favouring any one channel. */
  var NEUTRAL_BASE = [71, 85, 105];
  var CHAN_FALLBACK = { ECOM: '#2a78d6', GT: '#eb6834', MT: '#1baf7a', CSD: '#e87ba4', HORECA: '#7a6ad8', ROI: '#8a97ab', REST: '#8a97ab' };
  function hexToRgb(h) {
    h = String(h || '').trim().replace('#', '');
    if (h.length === 3) h = h.split('').map(function (c) { return c + c; }).join('');
    var n = parseInt(h, 16);
    return isNaN(n) || h.length !== 6 ? null : [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function mixRgb(a, b, t) { return [0, 1, 2].map(function (i) { return Math.round(a[i] + (b[i] - a[i]) * t); }); }
  function rgbStr(c, alpha) { return alpha == null ? 'rgb(' + c.join(',') + ')' : 'rgba(' + c.join(',') + ',' + alpha + ')'; }
  function chanHex(name) {
    var el = document.getElementById('slideTwo');
    var v = el ? getComputedStyle(el).getPropertyValue('--ch-' + name).trim() : '';
    return v || CHAN_FALLBACK[name] || '';
  }
  function rampFrom(base) {
    var W = [255, 255, 255], K = [0, 0, 0];
    var light = mixRgb(base, W, 0.86), mid = mixRgb(base, K, 0.12), deep = mixRgb(base, K, 0.35);
    return { from: light, to: deep, mid: mid, deep: deep, light: light,
             soft: rgbStr(mixRgb(base, W, 0.90)), brd: rgbStr(mixRgb(base, W, 0.72)),
             shadow: rgbStr(deep, 0.28), glow: rgbStr(base, 0.55) };
  }
  var rampCache = {};
  function rampFor(name) {
    if (!name) return rampFrom(NEUTRAL_BASE);
    if (!rampCache[name]) rampCache[name] = rampFrom(hexToRgb(chanHex(name)) || NEUTRAL_BASE);
    return rampCache[name];
  }
  function activeRamp() { return rampFor(activeChannel); }
  /* Push the ramp into the CSS variables the map rules read. */
  function applyMapTheme() {
    var el = document.getElementById('slideTwo') || document.documentElement;
    var r = activeRamp();
    el.style.setProperty('--map-light', rgbStr(r.light));
    el.style.setProperty('--map-mid', rgbStr(r.mid));
    el.style.setProperty('--map-deep', rgbStr(r.deep));
    el.style.setProperty('--map-soft', r.soft);
    el.style.setProperty('--map-brd', r.brd);
    el.style.setProperty('--map-shadow', r.shadow);
    el.style.setProperty('--map-glow', r.glow);
  }

  /* ---- colour ------------------------------------------------------------
     Ecom's green, endpoint for endpoint: rgb(226,243,230) to rgb(21,94,48),
     and #eef1f4 where there are no sales.

     The SPACING is logarithmic, not square-root. One state routinely carries
     ~80% of the litres (Haryana today), and on a sqrt ramp that leaves ten
     states bunched between 0.03 and 0.17 - all of them near-white and
     indistinguishable. Log spacing, stretched across the full range from the
     smallest selling state to the largest, gives every state a shade you can
     actually tell apart while still being strictly ordered by litres: more
     litres is always darker. */
  var logMin = 0, logMax = 1;
  function setScale(minV, maxV) {
    logMin = Math.log(1 + Math.max(0, minV));
    logMax = Math.log(1 + Math.max(0, maxV));
    if (logMax - logMin < 1e-9) logMax = logMin + 1;   // one state only
  }
  function colorFor(v, ramp) {
    if (!v || v <= 0) return '#eef1f4';
    var t = (Math.log(1 + v) - logMin) / (logMax - logMin);
    t = Math.max(0, Math.min(1, t));
    /* Never start at the very palest end - the lightest selling state should
       still read as green, not as "no data". */
    t = 0.12 + t * 0.88;
    ramp = ramp || activeRamp();
    var from = ramp.from, to = ramp.to;
    function mix(a, b) { return Math.round(a + (b - a) * t); }
    return 'rgb(' + mix(from[0], to[0]) + ',' + mix(from[1], to[1]) + ',' + mix(from[2], to[2]) + ')';
  }

  /* ---- geometry: fetched once, then kept for the life of the page ---- */
  var geoPaths = null, geoPromise = null;
  function loadGeo() {
    if (geoPromise) return geoPromise;
    geoPromise = fetch(GEO_URL).then(function (r) {
      if (!r.ok) throw new Error('map file ' + r.status);
      return r.json();
    }).then(function (json) {
      var feats = json.features || [];
      var proj = d3.geoMercator().fitSize([VB_W, VB_H], { type: 'FeatureCollection', features: feats });
      var gen = d3.geoPath(proj);
      geoPaths = feats.map(function (f) {
        var p = f.properties || {};
        return {
          d: gen(f),
          name: norm(p.ST_NM || p.st_nm || p.NAME_1 || p.name || p.State || ''),
          area: gen.area(f)
        };
      }).filter(function (p) { return p.d; });
      /* Biggest first, so the small states and UTs paint on top of their
         neighbours instead of vanishing underneath them. */
      geoPaths.sort(function (a, b) { return b.area - a.area; });
      return geoPaths;
    });
    return geoPromise;
  }

  var firstPaint = true;
  /* Picked channel: its ramp. All channels: the ramp of the state's leading channel. */
  function fillFor(name, v) {
    var d = domByState[name];
    return colorFor(v, (!activeChannel && d && d.channel) ? rampFor(d.channel) : null);
  }
  function rowAccent(name) {
    var d = domByState[name];
    if (activeChannel || !d || !d.channel) return null;
    var r = rampFor(d.channel);
    return { deep: rgbStr(r.deep), mid: rgbStr(r.mid) };
  }
  function leadText(name) {
    var d = domByState[name];
    return (!activeChannel && d && d.channel) ? ' · ' + channelLabel(d.channel) + ' ' + Math.round(d.share * 100) + '%' : '';
  }
  function drawMap(byState) {
    var out = ['<g class="rsm-extrude">'];
    for (var i = 0; i < DEPTH; i++) {
      out.push('<g transform="translate(0,' + (DEPTH - i) + ')">');
      for (var w = 0; w < geoPaths.length; w++) out.push('<path class="rsm-wall" d="' + geoPaths[w].d + '"/>');
      out.push('</g>');
    }
    out.push('</g><g>');
    for (var s = 0; s < geoPaths.length; s++) {
      var p = geoPaths[s], v = byState[p.name] || 0;
      out.push('<path class="rsm-state' + (v > 0 ? ' has-data' : '') + '" data-state="' + p.name +
               '" fill="' + fillFor(p.name, v) + '" style="animation-delay:' + (s * 7) + 'ms" d="' + p.d + '"/>');
    }
    out.push('</g>');
    svgEl.innerHTML = out.join('');
    applyZoom();      /* keep the current zoom and re-space the walls */
    svgEl.hidden = false;
    if (scaleEl) scaleEl.hidden = false;
    emptyEl.style.display = 'none';
    if (firstPaint) {
      svgEl.classList.add('is-entering');
      setTimeout(function () { svgEl.classList.remove('is-entering'); }, 1500);
      firstPaint = false;
    }
  }

  /* ---- hover, in both directions ---- */
  var values = {}, domByState = {};
  function markActive(name) {
    var on = svgEl.querySelectorAll('.rsm-state.is-on');
    for (var i = 0; i < on.length; i++) on[i].classList.remove('is-on');
    var rows = listEl.querySelectorAll('.rsm-row.is-on');
    for (var r = 0; r < rows.length; r++) rows[r].classList.remove('is-on');
    if (!name) return;
    var face = svgEl.querySelector('.rsm-state[data-state="' + name + '"]');
    if (face) face.classList.add('is-on');
    var row = listEl.querySelector('.rsm-row[data-state="' + name + '"]');
    if (row) row.classList.add('is-on');
  }
  function hideTip() { tipEl.classList.remove('show'); markActive(null); }

  /* ---- zoom and pan ---------------------------------------------------------
     Done with the SVG viewBox: zooming shows a smaller window of the same
     600x680 drawing, so nothing is redrawn. The raised walls are re-spaced by
     1/k so their on-screen thickness stays constant instead of growing with
     the zoom. Buttons zoom about the centre; the wheel zooms about the cursor;
     drag pans. A drag that moved the mouse is not a click, so it never opens
     a state by accident. */
  var VB = { x: 0, y: 0, w: VB_W, h: VB_H }, zoomK = 1, dragged = false;
  function applyZoom() {
    svgEl.setAttribute('viewBox', VB.x + ' ' + VB.y + ' ' + VB.w + ' ' + VB.h);
    var walls = svgEl.querySelectorAll('.rsm-extrude > g');
    for (var i = 0; i < walls.length; i++) walls[i].setAttribute('transform', 'translate(0,' + ((DEPTH - i) / zoomK) + ')');
  }
  function zoomAt(factor, cx, cy) {              /* cx, cy in viewBox units */
    var k = Math.max(1, Math.min(8, zoomK * factor)); factor = k / zoomK; if (factor === 1) return;
    zoomK = k;
    var nw = VB_W / k, nh = VB_H / k;
    VB.x = cx - (cx - VB.x) / factor; VB.y = cy - (cy - VB.y) / factor; VB.w = nw; VB.h = nh;
    VB.x = Math.max(0, Math.min(VB_W - nw, VB.x)); VB.y = Math.max(0, Math.min(VB_H - nh, VB.y));
    applyZoom();
  }
  function zoomHome() { zoomK = 1; VB = { x: 0, y: 0, w: VB_W, h: VB_H }; applyZoom(); }
  function toVB(e) {                              /* mouse -> viewBox units */
    var r = svgEl.getBoundingClientRect();
    return { x: VB.x + (e.clientX - r.left) / r.width * VB.w, y: VB.y + (e.clientY - r.top) / r.height * VB.h, r: r };
  }
  var zoomBox = wrapEl.querySelector('.rsm-zoom');
  if (zoomBox) zoomBox.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-z]') : null; if (!b) return;
    var z = b.getAttribute('data-z');
    if (z === 'home') zoomHome(); else zoomAt(z === 'in' ? 1.4 : 1 / 1.4, VB.x + VB.w / 2, VB.y + VB.h / 2);
  });
  svgEl.addEventListener('wheel', function (e) {
    if (svgEl.hidden) return;
    e.preventDefault(); var p = toVB(e); zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y);
  }, { passive: false });
  var drag = null;
  svgEl.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, vx: VB.x, vy: VB.y, r: svgEl.getBoundingClientRect() }; dragged = false;
    try { svgEl.setPointerCapture(e.pointerId); } catch (err) {}
  });
  svgEl.addEventListener('pointermove', function (e) {
    if (!drag) return;
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!dragged && Math.abs(dx) + Math.abs(dy) < 4) return;
    dragged = true; svgEl.classList.add('is-dragging'); hideTip();
    VB.x = Math.max(0, Math.min(VB_W - VB.w, drag.vx - dx / drag.r.width * VB.w));
    VB.y = Math.max(0, Math.min(VB_H - VB.h, drag.vy - dy / drag.r.height * VB.h));
    applyZoom();
  });
  function endDrag() { drag = null; svgEl.classList.remove('is-dragging'); }
  svgEl.addEventListener('pointerup', endDrag); svgEl.addEventListener('pointercancel', endDrag); svgEl.addEventListener('pointerleave', endDrag);

  svgEl.addEventListener('mousemove', function (e) {
    var t = e.target;
    if (!t || !t.classList || !t.classList.contains('rsm-state')) { hideTip(); return; }
    var name = t.getAttribute('data-state'), box = wrapEl.getBoundingClientRect(), v = values[name];
    tipEl.innerHTML = '<b>' + titleCase(name) + '</b><span>' +
                      (v ? num(v) + ' ' + (typeof QU === 'function' ? QU() : 'LTR') + leadText(name)
                         : 'No sales in this range') + '</span>';
    tipEl.style.left = (e.clientX - box.left) + 'px';
    tipEl.style.top  = (e.clientY - box.top) + 'px';
    tipEl.classList.add('show');
    markActive(name);
  });
  svgEl.addEventListener('mouseleave', hideTip);
  listEl.addEventListener('mouseover', function (e) {
    var row = e.target.closest ? e.target.closest('.rsm-row') : null;
    if (row) markActive(row.getAttribute('data-state'));
  });
  listEl.addEventListener('mouseleave', function () { markActive(null); });

  function drawList(rows) {
    if (!rows.length) {
      listEl.innerHTML = '<p class="rsm-empty-note">No state came back for this range.</p>';
      return;
    }
    var top = rows[0].value || 1, html = '';
    for (var i = 0; i < rows.length; i++) {
      var pct = Math.max(2, Math.round(rows[i].value / top * 100)), acc = rowAccent(rows[i].name);
      /* No role="button" here on purpose. base.html gives every [role="button"]
         a 1px border, which boxed each row in the list. These are list rows that
         happen to be clickable, not buttons; tabindex keeps them reachable by
         keyboard and the Enter/Space handler below still opens them. */
      html += '<div class="rsm-row" tabindex="0" data-state="' + rows[i].name + '">' +
                '<span class="rsm-row-name">' + esc(titleCase(rows[i].name)) + '</span>' +
                '<span class="rsm-row-val"' + (acc ? ' style="color:' + acc.deep + '"' : '') + '>' + num(rows[i].value) + '</span>' +
                '<span class="rsm-row-bar"><i style="width:' + pct + '%;animation-delay:' + (i * 26) + 'ms'
                  + (acc ? ';background:linear-gradient(90deg,' + acc.mid + ',' + acc.deep + ')' : '') + '"></i></span>' +
              '</div>';
    }
    listEl.innerHTML = html;
  }

  /* ---- click a state: break its litres down, from the rows already loaded --- */
  var panel = null;
  function closePanel() {
    if (panel) { panel.remove(); panel = null; }
    document.removeEventListener('keydown', onEsc);
  }
  function onEsc(e) { if (e.key === 'Escape') closePanel(); }

  /* ---- the row renderer ---------------------------------------------------
     One <tr> of the single drill table. `depth` indents it, and the little
     coloured tag says which level the row is - the same idea the channel drill
     table uses, so the two read alike. */
  function rsmpMoney(v, l) {
    return (l > 0 && v) ? '₹' + (typeof fNp === 'function' ? fNp(v / l, 2) : (v / l).toFixed(2)) : '&mdash;';
  }
  function blank() {
    return { target: 0, targetRealise: 0, done: 0, doneValue: 0, oih: 0, oihValue: 0 };
  }
  function addInto(a, b) {
    a.target += b.target; a.done += b.done; a.doneValue += b.doneValue;
    a.oih += b.oih; a.oihValue += b.oihValue;
    if (b.targetRealise > 0) a.targetRealise = b.targetRealise;
    return a;
  }
  function treeRow(node, depth, hasTarget, open) {
    var a = node.agg;
    var bal = a.target - (a.done + a.oih);
    var balRlz = (a.target > 0 && bal !== 0) ? ((a.target * (a.targetRealise || 0)) - a.doneValue) / bal : NaN;
    var kids = !!(node.children && node.children.length);
    /* The twirl is only the hint that a row opens - the whole row is clickable,
       so it is decoration here rather than the control. */
    var twirl = kids
      ? '<span class="rsmp-tw' + (open ? ' is-open' : '') + '" aria-hidden="true">&#9656;</span>'
      : '<span class="rsmp-tw-gap"></span>';
    return '<tr class="rsmp-r rsmp-d' + Math.min(depth, 5) + (kids ? ' can-open' : '') +
      '" data-path="' + esc(node.path) + '"' + (kids ? ' data-kids="1"' : '') + '>' +
      '<td class="rsmp-n"><span class="rsmp-cell" style="padding-left:' + (depth * 18) + 'px">' + twirl +
        '<span class="rsmp-tag rsmp-tag-' + esc(node.dim) + '">' + esc(node.dimLabel) + '</span>' +
        '<span class="rsmp-nm">' + esc(titleCase(node.label)) + '</span></span></td>' +
      '<td class="rsmp-num">' + num(a.target) + '</td>' +
      '<td class="rsmp-num">' + (typeof fNp === 'function' ? fNp(a.targetRealise || 0, 2) : (a.targetRealise || 0).toFixed(2)) + '</td>' +
      '<td class="rsmp-num rsmp-done">' + num(a.done) + '</td>' +
      '<td class="rsmp-num">' + rsmpMoney(a.doneValue, a.done) + '</td>' +
      '<td class="rsmp-num rsmp-oih">' + num(a.oih) + '</td>' +
      '<td class="rsmp-num">' + rsmpMoney(a.oihValue, a.oih) + '</td>' +
      '<td class="rsmp-num ' + (bal < 0 ? 'rsmp-bad' : 'rsmp-good') + '">' + num(bal) + '</td>' +
      '<td class="rsmp-num">' + (isFinite(balRlz) ? '₹' + (typeof fNp === 'function' ? fNp(balRlz, 2) : balRlz.toFixed(2)) : '&mdash;') + '</td>' +
    '</tr>';
  }

  /* ---- which breakdowns the panel shows -----------------------------------
     The reader picks; the choice is remembered per browser. `only` marks a
     dimension that is pointless once a channel is selected (it would be a
     single 100% row), so it is hidden rather than shown as noise. */
  /* `sale` reads the key off a sales row, `oih` off an Order-in-Hand row and
     `target` off a target node - the three sources name the same thing
     differently. `hasTarget: false` means SAP simply holds no target at that
     level, so those columns show an em-dash instead of a misleading zero. */
  var PANEL_DIMS = [
    { key: 'channel', label: 'Channel', hint: 'GT / MT / ROI / E-Com', allOnly: true, hasTarget: true,
      sale: 'u_main_group', oih: 'main_group', target: 'main_group' },
    { key: 'product', label: 'Product', hint: 'sub-group / category', hasTarget: false,
      sale: 'u_sub_group', oih: 'u_sub_group' },
    { key: 'item', label: 'Item name', hint: 'SAP item name', limit: 15, hasTarget: false,
      sale: 'item_name', oih: 'item_name' },
    { key: 'customer', label: 'Customer', hint: 'CardName / buyer', limit: 10, hasTarget: false,
      sale: 'card_name', oih: 'card_name' },
    { key: 'person', label: 'Sales person', hint: 'SAP sales person', hasTarget: true,
      sale: 'sales_person', oih: 'sales_person', target: 'sales_person' },
    { key: 'owner', label: 'Contact person', hint: 'assigned territory owner', hasTarget: true,
      sale: function (r) { return owner(r.u_main_group, r.state); },
      oih:  function (r) { return owner(r.main_group, r.state); },
      target: function (r) { return owner(r.main_group, r.state); } },
    { key: 'type', label: 'Segment', hint: 'Premium / Commodity', hasTarget: false,
      sale: 'u_type', oih: 'u_type' }
  ];
  function owner(group, state) {
    return (typeof assignedPerson === 'function') ? (assignedPerson(group, state) || '—') : '—';
  }
  function keyOf(pick, row) {
    if (!pick) return null;
    var v = (typeof pick === 'function') ? pick(row) : row[pick];
    return String(v == null ? '' : v).trim().toUpperCase() || '—';
  }
  /* Target nodes and OIH rows are not filtered by the map's channel picker the
     way the sales rows are, so they get the same test applied here. */
  function inChannel(mainGroup) {
    if (!activeChannel) return true;
    var blk = blockOf(activeChannel);
    if (!blk) return true;
    var g = String(mainGroup || '').trim().toUpperCase();
    for (var i = 0; i < blk.members.length; i++) {
      if (String(blk.members[i]).toUpperCase() === g) return true;
    }
    return false;
  }
  /* The picked dimensions, IN THE ORDER THEY WERE TICKED - that order is the
     drill path, so ticking Product then Item name gives Product > Item name. */
  var DIMS_KEY = 'cp:realise:statePanelPath:v2';
  var dimPath = (function () {
    try {
      var saved = JSON.parse(localStorage.getItem(DIMS_KEY));
      if (Array.isArray(saved) && saved.length) return saved;
    } catch (e) { /* private window, blocked storage - fall through */ }
    return ['product', 'item'];
  })();
  function saveDims() {
    try { localStorage.setItem(DIMS_KEY, JSON.stringify(dimPath)); } catch (e) { /* ignore */ }
  }
  function dimByKey(k) {
    for (var i = 0; i < PANEL_DIMS.length; i++) if (PANEL_DIMS[i].key === k) return PANEL_DIMS[i];
    return null;
  }

  /* ---- Target / Order-in-Hand, fetched the same way the cards do ----------
     Both helpers below live in dashboard.js and cache their own results, so
     calling them again is cheap after the first time. */
  var extraPromise = null;
  function fetchExtra() {
    if (extraPromise) return extraPromise;
    if (typeof fetchTargetNodes !== 'function' || typeof fetchOrderInHandRows !== 'function'
        || typeof getResolvedChannelPeriod !== 'function') {
      extraPromise = Promise.resolve({ targets: [], oih: [] });
      return extraPromise;
    }
    var period = getResolvedChannelPeriod();
    extraPromise = Promise.all([
      fetchTargetNodes(period.month, period.year),
      fetchOrderInHandRows()
    ]).then(function (r) { return { targets: r[0] || [], oih: r[1] || [] }; })
      .catch(function () { return { targets: [], oih: [] }; });
    return extraPromise;
  }


  function openState(name) {
    if (typeof getFilteredChannelRows !== 'function') return;
    /* Same scope the map is currently showing, so opening a state never reports
       more than the shading led you to expect. */
    var scope = rowsFor(getFilteredChannelRows() || [], activeChannel);
    var rows = scope.filter(function (r) {
      return norm(typeof canonState === 'function' ? canonState(r.state) : r.state) === name;
    });
    if (!rows.length) return;

    var litres = 0, value = 0;
    rows.forEach(function (r) { litres += QTY(r); value += Number(r.line_total) || 0; });
    var rate = litres ? value / litres : 0;
    var share = (function () {
      var all = 0;
      scope.forEach(function (r) { all += QTY(r); });
      return all ? Math.round(litres / all * 100) : 0;
    })();

    closePanel();
    panel = document.createElement('div');
    panel.className = 'rsmp-back';
    panel.innerHTML =
      '<div class="rsmp" role="dialog" aria-modal="true" aria-label="' + esc(titleCase(name)) + ' sales">' +
        '<header class="rsmp-head">' +
          '<div><h3>' + esc(titleCase(name)) + '</h3>' +
          '<p>' + rows.length + ' lines · ' + share + '% of ' +
            (activeChannel ? esc(channelLabel(activeChannel)) : 'all channels') + '</p></div>' +
          '<div class="rsmp-acts">' +
            '<button type="button" class="rsmp-full" aria-pressed="false" title="Fill the screen" aria-label="Fill the screen">&#10063;</button>' +
            '<button type="button" class="rsmp-x" aria-label="Close">&times;</button>' +
          '</div>' +
        '</header>' +
        '<div class="rsmp-kpis">' +
          '<div><span>Litres</span><b>' + num(litres) + '</b></div>' +
          '<div><span>Revenue</span><b>' + money(value) + '</b></div>' +
          '<div><span>Realise</span><b>₹' + (typeof fNp === 'function' ? fNp(rate, 2) : rate.toFixed(2)) + '<small>/LTR</small></b></div>' +
        '</div>' +
        '<div class="rsmp-pick" id="rsmpPick"></div>' +
        '<div class="rsmp-body" id="rsmpBody"></div>' +
      '</div>';
    document.body.appendChild(panel);

    /* Dimensions that make sense here. "Channel" drops out once a channel is
       already selected on the map. */
    function usable() {
      return PANEL_DIMS.filter(function (d) { return !(d.allOnly && activeChannel); });
    }
    function chosen() {
      var out = [];
      dimPath.forEach(function (k) {
        var d = dimByKey(k);
        if (d && !(d.allOnly && activeChannel)) out.push(d);
      });
      return out;
    }
    function drawPick() {
      var list = usable(), h =
        '<div class="rsmp-pick-head"><span>Break down by</span>' +
          '<div><button type="button" data-none="1"' + (dimPath.length ? '' : ' disabled') +
            '>Clear all</button></div></div><div class="rsmp-pick-list">';
      list.forEach(function (d) {
        var on = dimPath.indexOf(d.key) >= 0;
        h += '<label class="rsmp-pick-item' + (on ? ' is-on' : '') + '">' +
               '<input type="checkbox" data-dim="' + d.key + '"' + (on ? ' checked' : '') + '>' +
               '<span><b>' + esc(d.label) + '</b><i>' + esc(d.hint) + '</i></span>' +
             '</label>';
      });
      panel.querySelector('#rsmpPick').innerHTML = h + '</div>';
    }

    /* The Premium/Commodity filter the sales rows already went through; the OIH
       rows have not, so they are filtered to match. */
    var segOn = (typeof sc2Seg === 'function') ? String(sc2Seg() || '').toUpperCase() : '';
    var openPaths = {};

    /* Flatten the three sources into leaves carrying every dimension key, so the
       tree can be grouped in any order without re-reading the sources. */
    function buildLeaves(extra) {
      var map = {}, dims = usable();
      function leaf(keys) {
        var id = dims.map(function (d) { return keys[d.key]; }).join('|');
        if (!map[id]) map[id] = { keys: keys, agg: blank() };
        return map[id].agg;
      }
      function keysFrom(row, which) {
        var k = {};
        dims.forEach(function (d) { k[d.key] = d[which] ? keyOf(d[which], row) : '—'; });
        return k;
      }
      rows.forEach(function (r) {
        var a = leaf(keysFrom(r, 'sale'));
        a.done += QTY(r); a.doneValue += Number(r.line_total) || 0;
      });
      (extra.oih || []).forEach(function (x) {
        if (norm(typeof canonState === 'function' ? canonState(x.state) : x.state) !== name) return;
        if (activeChannel && !inChannel(x.main_group)) return;
        if (segOn && String(x.u_type || '').toUpperCase() !== segOn) return;
        var a = leaf(keysFrom(x, 'oih'));
        a.oih += Number(x.open_qty) || 0; a.oihValue += Number(x.open_value) || 0;
      });
      (extra.targets || []).forEach(function (t) {
        if (norm(typeof canonState === 'function' ? canonState(t.state) : t.state) !== name) return;
        if (activeChannel && !inChannel(t.main_group)) return;
        var a = leaf(keysFrom(t, 'target'));
        a.target += Number(t.target_ltrs) || 0;
        var tr = Number(t.target_realise) || 0; if (tr > 0) a.targetRealise = tr;
      });
      return Object.keys(map).map(function (k) { return map[k]; });
    }

    /* Group the leaves level by level, following the ticked order. */
    function buildTree(leaves, path, depth, prefix) {
      if (depth >= path.length) return [];
      var d = path[depth], groups = {}, order = [];
      leaves.forEach(function (lf) {
        var k = lf.keys[d.key] || '—';
        if (!groups[k]) { groups[k] = []; order.push(k); }
        groups[k].push(lf);
      });
      return order.map(function (k) {
        var kids = groups[k], agg = blank();
        kids.forEach(function (lf) { addInto(agg, lf.agg); });
        var pathId = prefix + d.key + ':' + k;
        return {
          label: k, dim: d.key, dimLabel: d.label.toUpperCase(), path: pathId, agg: agg,
          children: buildTree(kids, path, depth + 1, pathId + '>')
        };
      }).filter(function (n) {
        /* SAP holds no target below channel / state / person, so those rows land
           in a "-" bucket. Drop it when it carries nothing but target - the TOTAL
           still counts it, exactly as the channel cards do. */
        if (n.label === '—' && !n.agg.done && !n.agg.oih) return false;
        return n.agg.done || n.agg.oih || n.agg.target;
      }).sort(function (a, b) { return b.agg.done - a.agg.done; });
    }

    function flatten(nodes, depth, hasTarget, out) {
      nodes.forEach(function (n) {
        var open = !!openPaths[n.path];
        out.push(treeRow(n, depth, hasTarget, open));
        if (open && n.children.length) flatten(n.children, depth + 1, hasTarget, out);
      });
      return out;
    }

    var lastTree = null, lastTot = null, lastHasTarget = false, lastHead = '';
    function paint() {
      var body = panel.querySelector('#rsmpBody');
      var tot = lastTot, hasTarget = lastHasTarget;
      var bal = tot.target - (tot.done + tot.oih);
      var balRlz = (tot.target > 0 && bal !== 0) ? ((tot.target * tot.targetRealise) - tot.doneValue) / bal : NaN;
      body.innerHTML =
        '<div class="rsmp-scroll"><table class="rsmp-tbl"><thead><tr>' +
          '<th class="rsmp-h-n">' + esc(lastHead) + '</th>' +
          '<th>TARGET L</th><th>TGT REALISE</th><th>DONE L</th><th>DONE REALISE</th>' +
          '<th>ORDER IN HAND</th><th>OIH REALISE</th><th>BAL</th><th>BAL REALISE</th>' +
        '</tr></thead><tbody>' +
          (flatten(lastTree, 0, hasTarget, []).join('') ||
            '<tr><td colspan="9" class="rsmp-none">No rows for this state.</td></tr>') +
          '<tr class="rsmp-total"><td class="rsmp-n">TOTAL</td>' +
            '<td class="rsmp-num">' + num(tot.target) + '</td>' +
            '<td class="rsmp-num">' + (typeof fNp === 'function' ? fNp(tot.targetRealise || 0, 2) : (tot.targetRealise || 0).toFixed(2)) + '</td>' +
            '<td class="rsmp-num rsmp-done">' + num(tot.done) + '</td>' +
            '<td class="rsmp-num">' + rsmpMoney(tot.doneValue, tot.done) + '</td>' +
            '<td class="rsmp-num rsmp-oih">' + num(tot.oih) + '</td>' +
            '<td class="rsmp-num">' + rsmpMoney(tot.oihValue, tot.oih) + '</td>' +
            '<td class="rsmp-num ' + (bal < 0 ? 'rsmp-bad' : 'rsmp-good') + '">' + num(bal) + '</td>' +
            '<td class="rsmp-num">' + (isFinite(balRlz) ? '₹' + (typeof fNp === 'function' ? fNp(balRlz, 2) : balRlz.toFixed(2)) : '&mdash;') + '</td>' +
          '</tr>' +
        '</tbody></table></div>';
    }

    function drawBody() {
      var path = chosen();
      var body = panel.querySelector('#rsmpBody');
      if (!path.length) {
        body.innerHTML = '<p class="rsmp-none">Nothing selected. Tick a box above to break this state down.</p>';
        return;
      }
      body.innerHTML = '<p class="rsmp-none">Loading&hellip;</p>';
      fetchExtra().then(function (extra) {
        if (!panel) return;                       // closed while the fetch was in flight
        /* Every column is always shown, exactly as the channel cards show them:
           a level SAP holds no target for reads 0, and Bal becomes the negative of
           Done + Order in Hand. Blanking them made the panel disagree with the
           cards, which is worse than showing an honest zero. */
        lastHasTarget = true;
        var leaves = buildLeaves(extra);
        lastTree = buildTree(leaves, path, 0, '');
        var tot = blank();
        leaves.forEach(function (lf) { addInto(tot, lf.agg); });
        lastTot = tot;
        lastHead = path.map(function (d) { return d.label.toUpperCase(); }).join(' › ');
        paint();
      });
    }
    drawPick(); drawBody();

    /* Ticking appends to the path, so the ORDER you tick is the order you
       drill: Product then Item name gives Product > Item name. */
    panel.addEventListener('change', function (e) {
      var k = e.target && e.target.getAttribute && e.target.getAttribute('data-dim');
      if (!k) return;
      var at = dimPath.indexOf(k);
      if (e.target.checked) { if (at < 0) dimPath.push(k); }
      else if (at >= 0) dimPath.splice(at, 1);
      openPaths = {};                      // the tree changed shape
      saveDims(); drawPick(); drawBody();
    });

    panel.addEventListener('click', function (e) {
      if (e.target === panel || e.target.classList.contains('rsmp-x')) { closePanel(); return; }
      /* expand / collapse one row - repaint from the tree already in memory,
         so it is instant and never refetches */
      /* Full screen toggle. */
      if (e.target.closest && e.target.closest('.rsmp-full')) {
        var box = panel.querySelector('.rsmp');
        var full = box.classList.toggle('is-full');
        var b = panel.querySelector('.rsmp-full');
        b.setAttribute('aria-pressed', full ? 'true' : 'false');
        b.title = full ? 'Back to a window' : 'Fill the screen';
        b.innerHTML = full ? '&#10066;' : '&#10063;';
        return;
      }
      /* A whole row opens it, not just the twirl - the twirl is only the hint
         that it can. Rows with no children have no data-kids and stay inert. */
      var rowEl = e.target.closest ? e.target.closest('tr.rsmp-r[data-kids]') : null;
      if (rowEl) {
        var path = rowEl.getAttribute('data-path');
        openPaths[path] = !openPaths[path];
        if (lastTree) paint();
        return;
      }
      if (e.target.getAttribute && e.target.getAttribute('data-none')) {
        dimPath = []; openPaths = {};
        saveDims(); drawPick(); drawBody();
      }
    });

    document.addEventListener('keydown', onEsc);
  }

  /* The map no longer opens anything - hovering it reports the figure under the
     cursor, and opening a state is the job of the "States by litres" list beside it,
     which already had the same click and is easier to hit than a small border state. */
  listEl.addEventListener('click', function (e) {
    var row = e.target.closest ? e.target.closest('.rsm-row') : null;
    if (row) openState(row.getAttribute('data-state'));
  });
  listEl.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var row = e.target.closest ? e.target.closest('.rsm-row') : null;
    if (row) { e.preventDefault(); openState(row.getAttribute('data-state')); }
  });

  /* ---- channel picker -----------------------------------------------------
     '' means every channel added together. Otherwise it is a CHANNEL_BLOCKS
     name (GT, MT, ROI, ECOM, ...), the same blocks the cards below are built
     from, so the two can never describe different things.

     GT and ROI additionally carry a fixed state whitelist - their cards only
     count the states on that list. Applying it here is what makes the map's
     total match the card's TOTAL row exactly instead of being quietly higher. */
  var activeChannel = '';
  var tabsEl = document.getElementById('rsmTabs');
  var subEl  = document.getElementById('rsmSub');

  function blocks() {
    return (typeof CHANNEL_BLOCKS !== 'undefined' && CHANNEL_BLOCKS) ? CHANNEL_BLOCKS : [];
  }
  function blockOf(name) {
    var b = blocks();
    for (var i = 0; i < b.length; i++) if (b[i].name === name) return b[i];
    return null;
  }
  function channelLabel(name) {
    return { GT: 'General Trade', MT: 'Modern Trade', ROI: 'Rest of India',
             ECOM: 'E-Commerce', HORECA: 'Horeca', CSD: 'CSD', REST: 'Other' }[name] || name;
  }

  /* Rows for the chosen channel, whitelist included. */
  function rowsFor(all, name) {
    if (!name) return all;
    var blk = blockOf(name);
    if (!blk) return all;
    var members = {};
    blk.members.forEach(function (m) { members[String(m).toUpperCase()] = 1; });
    var wl = (typeof CHANNEL_STATE_WHITELIST !== 'undefined' && CHANNEL_STATE_WHITELIST)
               ? CHANNEL_STATE_WHITELIST[name] : null;
    return all.filter(function (r) {
      if (!members[String(r.u_main_group || '').trim().toUpperCase()]) return false;
      /* channelStateLabel returns null when the state is not on this channel's
         list - exactly how the card drops it. */
      if (wl && typeof channelStateLabel === 'function') {
        return channelStateLabel(name, r.state) !== null;
      }
      return true;
    });
  }

  function litresOf(rows) {
    var t = 0;
    for (var i = 0; i < rows.length; i++) t += QTY(rows[i]);
    return t;
  }

  function buildTabs(all) {
    if (!tabsEl) return;
    var html = '<button type="button" class="rsm-tab' + (activeChannel === '' ? ' is-on' : '') +
               '" data-ch="">All channels<b>' + num(litresOf(all)) + '</b></button>';
    blocks().forEach(function (b) {
      var n = litresOf(rowsFor(all, b.name));
      if (n <= 0) return;                       // hide a channel with nothing in it
      html += '<button type="button" class="rsm-tab' + (activeChannel === b.name ? ' is-on' : '') +
              '" data-ch="' + b.name + '">' + esc(channelLabel(b.name)) + '<b>' + num(n) + '</b></button>';
    });
    tabsEl.innerHTML = html;
  }

  if (tabsEl) {
    tabsEl.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('.rsm-tab') : null;
      if (!b) return;
      activeChannel = b.getAttribute('data-ch') || '';
      firstPaint = true;                        // replay the reveal on the new set
      build();
    });
  }

  /* ---- channel mix donut (the Ecom "By platform" card, for channels) -------
     Always the whole picture - every channel's share of done litres - even
     while one channel is focused; the focused one is highlighted and the rest
     dimmed. Clicking a slice or a legend row focuses that channel, exactly as
     the legend in the rail does. */
  function drawDonut(all) {
    var box = document.getElementById('rsmDonut');
    if (!box) return;
    var parts = blocks().map(function (b) { return { name: b.name, v: litresOf(rowsFor(all, b.name)) }; })
                        .filter(function (p) { return p.v > 0; })
                        .sort(function (a, b) { return b.v - a.v; });
    var tot = parts.reduce(function (a, p) { return a + p.v; }, 0);
    if (!tot) { box.innerHTML = '<p class="rsm-mini-none">Nothing in this range.</p>'; return; }
    var cx = 46, cy = 46, R = 42, r = 28, a0 = -Math.PI / 2, paths = '';
    parts.forEach(function (p) {
      var a1 = a0 + 2 * Math.PI * p.v / tot, big = (a1 - a0) > Math.PI ? 1 : 0;
      var P = function (ang, rad) { return (cx + rad * Math.cos(ang)).toFixed(2) + ',' + (cy + rad * Math.sin(ang)).toFixed(2); };
      var d = 'M' + P(a0, R) + ' A' + R + ',' + R + ' 0 ' + big + ' 1 ' + P(a1, R) + ' L' + P(a1, r) + ' A' + r + ',' + r + ' 0 ' + big + ' 0 ' + P(a0, r) + 'Z';
      if (parts.length === 1) d = 'M' + P(-Math.PI / 2, R) + ' A' + R + ',' + R + ' 0 1 1 ' + P(Math.PI / 2 * 3 - 0.0001, R) + ' L' + P(Math.PI / 2 * 3 - 0.0001, r) + ' A' + r + ',' + r + ' 0 1 0 ' + P(-Math.PI / 2, r) + 'Z';
      paths += '<path d="' + d + '" fill="' + chCol(p.name) + '" stroke="#fff" stroke-width="2" data-ch="' + p.name +
               '" class="' + (activeChannel && activeChannel !== p.name ? 'dim' : '') + '"><title>' + esc(channelLabel(p.name)) + ' · ' + num(p.v) + ' L · ' + (p.v / tot * 100).toFixed(1) + '%</title></path>';
      a0 = a1;
    });
    box.innerHTML = '<div class="dn">' +
      '<svg viewBox="0 0 92 92" role="img" aria-label="Done litres by channel">' + paths +
        '<text class="dn-c" x="46" y="44" text-anchor="middle">' + parts.length + '</text>' +
        '<text class="dn-l" x="46" y="56" text-anchor="middle">CHAN</text></svg>' +
      '<div class="lg">' + parts.map(function (p) {
        return '<button type="button" data-ch="' + p.name + '" class="' + (activeChannel === p.name ? 'is-on' : '') + '">' +
               '<i style="background:' + chCol(p.name) + '"></i><span>' + esc(channelLabel(p.name)) + '</span><b>' + (p.v / tot * 100).toFixed(0) + '%</b></button>';
      }).join('') + '</div></div>';
    box.querySelectorAll('[data-ch]').forEach(function (el) {
      el.addEventListener('click', function () {
        var name = el.getAttribute('data-ch');
        var tab = document.querySelector('#rsmTabs .rsm-tab[data-ch="' + (activeChannel === name ? '' : name) + '"]');
        if (tab) tab.click();                    /* same path as the rail legend: toggles focus */
      });
    });
  }

  /* ---- the only entry point: read the rows slide two already has ---- */
  function build() {
    if (typeof getFilteredChannelRows !== 'function') return;
    var all = getFilteredChannelRows() || [];
    buildTabs(all);
    drawDonut(all);
    applyMapTheme();
    var rows = rowsFor(all, activeChannel);
    if (subEl) {
      subEl.textContent = activeChannel
        ? channelLabel(activeChannel) + ' only — matches that card below'
        : 'All channels together';
    }
    var agg = {}, order = [];
    for (var i = 0; i < rows.length; i++) {
      var raw = rows[i].state;
      if (!raw) continue;
      var key = norm(typeof canonState === 'function' ? canonState(raw) : raw);
      if (!key || key === 'UNKNOWN') continue;
      if (agg[key] === undefined) { agg[key] = 0; order.push(key); }
      agg[key] += QTY(rows[i]);
    }
    /* Which channel leads in each state - drives the All-channels colouring. */
    domByState = {};
    if (!activeChannel) {
      var memberOf = {};
      blocks().forEach(function (b) { b.members.forEach(function (m) { memberOf[String(m).toUpperCase()] = b.name; }); });
      var per = {};
      for (var q = 0; q < rows.length; q++) {
        var r2 = rows[q], k2 = norm(typeof canonState === 'function' ? canonState(r2.state) : r2.state);
        if (!k2 || k2 === 'UNKNOWN') continue;
        var ch = memberOf[String(r2.u_main_group || '').trim().toUpperCase()] || 'REST';
        per[k2] = per[k2] || {};
        per[k2][ch] = (per[k2][ch] || 0) + (QTY(r2));
      }
      Object.keys(per).forEach(function (k3) {
        var best = null, bestV = -1, tot = 0;
        Object.keys(per[k3]).forEach(function (c2) { tot += per[k3][c2]; if (per[k3][c2] > bestV) { bestV = per[k3][c2]; best = c2; } });
        if (tot > 0) domByState[k3] = { channel: best, share: bestV / tot };
      });
    }
    var list = order.map(function (k) { return { name: k, value: agg[k] }; })
                    .filter(function (r) { return r.value > 0; })
                    .sort(function (a, b) { return b.value - a.value; });

    if (!list.length) {
      chipEl.textContent = 'No data yet';
      emptyEl.style.display = '';
      svgEl.hidden = true;
      if (scaleEl) scaleEl.hidden = true;
      listEl.innerHTML = '<p class="rsm-empty-note">Fetch a date range and the list fills in.</p>';
      var sb = document.getElementById('rsmStats'), zb = document.getElementById('rsmZones');
      if (sb) sb.innerHTML = '';
      if (zb) zb.innerHTML = '';
      return;
    }

    setScale(list[list.length - 1].value, list[0].value);
    values = agg;
    chipEl.textContent = list.length + ' states · ' +
      num(list.reduce(function (s, r) { return s + r.value; }, 0)) +
      ' ' + (typeof QU === 'function' ? QU() : 'LTR');
    drawList(list);
    fetchExtra().then(function (extra) { drawStats(agg, extra, rows); });
    loadGeo().then(function () { drawMap(agg); })
             .catch(function () {
               emptyEl.textContent = 'Map file did not load. The list on the right is still correct.';
             });
  }

  /* Wrap renderSlideTwo instead of editing it, so every path that redraws the
     channel cards refreshes the map too. Debounced: slide two can re-render
     several times in a row while a fetch settles. */
  var pending = null;
  function schedule() {
    clearTimeout(pending);
    pending = setTimeout(build, 140);
  }
  if (typeof window.renderSlideTwo === 'function') {
    var orig = window.renderSlideTwo;
    window.renderSlideTwo = function () {
      var out = orig.apply(this, arguments);
      schedule();
      return out;
    };
  }
  /* Segment dropdown filters the rows without always going through
     renderSlideTwo, so listen to it as well. */
  var seg = document.getElementById('sc2Segment');
  if (seg) seg.addEventListener('change', schedule);


  /* =========================================================================
     WORKBENCH panes. Everything here reads what the page already has:
     dashboard.js fills the KPI ids, state_map.js has the channel rows, and
     fetchExtra() brings targets + open orders. Nothing is fetched twice.
     ========================================================================= */
  var CH_COL = { ECOM:'--ch-ECOM', GT:'--ch-GT', MT:'--ch-MT', CSD:'--ch-CSD', HORECA:'--ch-HORECA', ROI:'--ch-ROI', REST:'--ch-REST' };
  var slideEl = document.getElementById('slideTwo');
  function tok(v) { return slideEl ? getComputedStyle(slideEl).getPropertyValue(v).trim() : ''; }
  function chCol(name) { return tok(CH_COL[name] || '--ch-ROI'); }
  function lakh(n) { return n >= 1e5 ? (n / 1e5).toFixed(2).replace(/\.?0+$/, '') + ' L' : num(n); }
  function parseNum(t) { var v = parseFloat(String(t || '').replace(/[^0-9.\-]/g, '')); return isFinite(v) ? v : 0; }

  /* ---- tabs ---- */
  /* button[data-pane], not every button in the row: the MT / Litres switch also lives
     on this row, and picking it up here meant clicking it ran the tab handler too -
     which cleared .on from every pane (the switch has no data-pane to match) and left
     the whole page blank until a tab was clicked again. */
  var tabBtns = document.querySelectorAll('#slideTwo .wb-tabs button[data-pane]');
  tabBtns.forEach(function (b) {
    b.addEventListener('click', function () {
      tabBtns.forEach(function (x) { x.classList.remove('on'); x.setAttribute('aria-selected', 'false'); });
      b.classList.add('on'); b.setAttribute('aria-selected', 'true');
      document.querySelectorAll('#slideTwo .wb-pane').forEach(function (p) { p.classList.toggle('on', p.id === b.dataset.pane); });
      try { localStorage.setItem('cp:realise:wbTab', b.dataset.pane); } catch (e) {}
    });
  });
  try {
    var savedTab = localStorage.getItem('cp:realise:wbTab');
    var tb = savedTab && document.querySelector('#slideTwo .wb-tabs button[data-pane="' + savedTab + '"]');
    if (tb) tb.click();
  } catch (e) {}

  /* ---- segment buttons drive the hidden <select> dashboard.js reads ---- */
  var segSel = document.getElementById('sc2Segment');
  document.querySelectorAll('#wbSegs button').forEach(function (b) {
    b.addEventListener('click', function () {
      if (!segSel) return;
      segSel.value = b.dataset.v;
      segSel.dispatchEvent(new Event('change', { bubbles: true }));
      document.querySelectorAll('#wbSegs button').forEach(function (x) { x.classList.toggle('on', x === b); });
    });
  });

  /* ---- KPI tiles: bars from the values dashboard.js writes ---- */
  function wbKpis() {
    var t = parseNum((document.getElementById('sc2KpiTarget') || {}).textContent);
    var d = parseNum((document.getElementById('sc2KpiDone') || {}).textContent);
    var o = parseNum((document.getElementById('sc2KpiOih') || {}).textContent);
    var b = parseNum((document.getElementById('sc2KpiBal') || {}).textContent);
    var max = Math.max(t, d, o, Math.abs(b), 1);
    var set = function (id, v) { var el = document.getElementById(id); if (el) el.style.width = Math.max(0, Math.min(100, v / max * 100)) + '%'; };
    set('wbBarTarget', t); set('wbBarDone', d); set('wbBarOih', o); set('wbBarBal', Math.abs(b));
    var bal = document.getElementById('sc2KpiBal');
    if (bal) { bal.classList.toggle('neg', b < 0); bal.classList.toggle('pos', b > 0); }
    /* pipeline bands */
    var pipe = document.getElementById('wbPipe');
    if (pipe) {
      var pm = Math.max(t, d, o, 1);
      var rows = [['done', 'Done', 'shipped', d], ['open', 'Open orders', 'not yet shipped', o], ['target', 'Target', 'for the month', t]];
      pipe.innerHTML = rows.map(function (r) {
        return '<div class="wb-band ' + r[0] + '"><div class="k">' + r[1] + '<small>' + r[2] + '</small></div>' +
          '<div class="b"><i style="width:' + (r[3] / pm * 100) + '%"></i><b>' + (r[3] ? lakh(r[3]) : '—') + '</b>' +
          (r[0] !== 'target' && t ? '<em style="left:' + (t / pm * 100) + '%"></em>' : '') + '</div></div>';
      }).join('');
    }
    /* today bars */
    var pr = parseNum((document.getElementById('sc2TodayPrem') || {}).textContent);
    var co = parseNum((document.getElementById('sc2TodayComm') || {}).textContent);
    var tm = Math.max(pr, co, 1);
    var pb = document.getElementById('wbTodayPremBar'), cb = document.getElementById('wbTodayCommBar');
    if (pb) pb.style.height = Math.max(4, pr / tm * 100) + '%';
    if (cb) cb.style.height = Math.max(4, co / tm * 100) + '%';
  }
  /* dashboard.js writes those spans directly; watch them rather than patch it */
  var kpiHost = document.getElementById('channelKpis'), todayHost = document.querySelector('#slideTwo .wb-today');
  var kpiTimer = null;
  function kpiSoon() { clearTimeout(kpiTimer); kpiTimer = setTimeout(wbKpis, 60); }
  if (window.MutationObserver) {
    var mo = new MutationObserver(kpiSoon);
    if (kpiHost) mo.observe(kpiHost, { childList: true, subtree: true, characterData: true });
    if (todayHost) mo.observe(todayHost, { childList: true, subtree: true, characterData: true });
  }
  kpiSoon();

  /* ---- channel small multiples + realise pane ---- */
  function wbLeaves(all, extra) {
    /* per channel -> per state: done, doneValue, open */
    var out = {};
    blocks().forEach(function (b) {
      var rows = rowsFor(all, b.name);
      var st = {}, openVal = 0;
      rows.forEach(function (r) {
        var k = norm(typeof canonState === 'function' ? canonState(r.state) : r.state);
        if (!k || k === 'UNKNOWN') return;
        st[k] = st[k] || { name: k, done: 0, val: 0, open: 0 };
        st[k].done += QTY(r); st[k].val += Number(r.line_total) || 0;
      });
      var members = {}; b.members.forEach(function (m) { members[String(m).toUpperCase()] = 1; });
      var segNow = (typeof sc2Seg === 'function') ? String(sc2Seg() || '').toUpperCase() : '';
      ((extra && extra.oih) || []).forEach(function (x) {
        if (!members[String(x.main_group || '').trim().toUpperCase()]) return;
        if (segNow && String(x.u_type || '').toUpperCase() !== segNow) return;
        var k = norm(typeof canonState === 'function' ? canonState(x.state) : x.state);
        if (!k || k === 'UNKNOWN') return;
        st[k] = st[k] || { name: k, done: 0, val: 0, open: 0 };
        st[k].open += Number(x.open_qty) || 0;
        openVal += Number(x.open_value) || 0;
      });
      var list = Object.keys(st).map(function (k) { return st[k]; }).filter(function (r) { return r.done || r.open; })
                       .sort(function (a, b2) { return b2.done - a.done; });
      var done = list.reduce(function (a, r) { return a + r.done; }, 0);
      var val = list.reduce(function (a, r) { return a + r.val; }, 0);
      var open = list.reduce(function (a, r) { return a + r.open; }, 0);
      if (done || open) out[b.name] = { name: b.name, states: list, done: done, val: val, open: open, rate: done ? val / done : 0,
                                       openRate: (open > 0 && openVal > 0) ? openVal / open : 0 };
    });
    return out;
  }

  function smallSvg(ch, color, SMAX) {
    var W = 300, H = 118, l = 96, r = 8, T0 = 6, rows = ch.states.slice(0, 4), step = (H - T0) / Math.max(rows.length, 1);
    var x = function (v) { return l + (W - l - r) * Math.sqrt(Math.min(1, v / SMAX)); };
    var out = '';
    rows.forEach(function (sRow, i) {
      var y = T0 + step * i, bh = Math.min(8, (step - 10) / 2);
      out += '<text class="wb-ax" x="' + (l - 8) + '" y="' + (y + bh + 5) + '" text-anchor="end">' + esc(titleCase(sRow.name)) + '</text>' +
        '<rect x="' + l + '" y="' + y + '" width="' + Math.max(2, x(sRow.done) - l) + '" height="' + bh + '" rx="3" fill="' + color + '"><title>' + esc(titleCase(sRow.name)) + ' · done ' + num(sRow.done) + ' L</title></rect>' +
        '<rect x="' + l + '" y="' + (y + bh + 2) + '" width="' + Math.max(2, x(sRow.open) - l) + '" height="' + bh + '" rx="3" fill="' + color + '" opacity=".3"><title>' + esc(titleCase(sRow.name)) + ' · open ' + num(sRow.open) + ' L</title></rect>';
    });
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(channelLabel(ch.name)) + ' by state">' + out + '</svg>';
  }

  function wbRender(all, extra) {
    var multi = document.getElementById('wbMulti'), rl = document.getElementById('wbRealiseMulti');
    if (!multi || !rl) return;
    var data = wbLeaves(all, extra);
    var names = Object.keys(data).filter(function (k) { return !activeChannel || k === activeChannel; });
    if (!names.length) {
      multi.innerHTML = '<p class="wb-empty">No channel rows in this range.</p>';
      rl.innerHTML = '<p class="wb-empty">No channel rows in this range.</p>';
      return;
    }
    var tot = names.reduce(function (a, k) { return a + data[k].done; }, 0) || 1;
    var SMAX = Math.max.apply(null, names.map(function (k) { return Math.max.apply(null, data[k].states.map(function (s2) { return Math.max(s2.done, s2.open); })); }).concat([1]));
    var target = parseNum((document.getElementById('sc2KpiTargetRealise') || {}).textContent) || 0;
    multi.innerHTML = names.sort(function (a, b) { return data[b].done - data[a].done; }).map(function (k) {
      var c = data[k], col = chCol(k);
      return '<article class="wb-sm" data-ch="' + k + '" title="Focus ' + esc(channelLabel(k)) + '">' +
        '<h3><i style="background:' + col + '"></i>' + esc(channelLabel(k)) + '<span>' + (c.done / tot * 100).toFixed(1) + '%</span></h3>' +
        smallSvg(c, col, SMAX) +
        '<div class="f"><span>done <b>' + lakh(c.done) + '</b></span><span>open <b>' + lakh(c.open) + '</b></span>' +
        '<span>₹/L <b class="' + (target ? (c.rate >= target ? 'pos' : 'neg') : '') + '">' + (c.rate ? c.rate.toFixed(0) : '—') + '</b></span></div></article>';
    }).join('');
    /* a card focuses that channel, same as clicking its legend row */
    multi.querySelectorAll('.wb-sm').forEach(function (card) {
      card.addEventListener('click', function () {
        var row = document.querySelector('#rsmTabs .rsm-tab[data-ch="' + card.dataset.ch + '"]');
        if (row) row.click();
      });
    });

    /* realise pane: lollipops per channel, plus best / weakest states */
    var byState = {};
    names.forEach(function (k) { data[k].states.forEach(function (s2) { byState[s2.name] = byState[s2.name] || { name: s2.name, done: 0, val: 0 }; byState[s2.name].done += s2.done; byState[s2.name].val += s2.val; }); });
    var states = Object.keys(byState).map(function (k) { return byState[k]; }).filter(function (r) { return r.done > 0 && r.val > 0; })
                       .map(function (r) { return { name: r.name, rate: r.val / r.done }; }).sort(function (a, b) { return b.rate - a.rate; });
    var avg = names.reduce(function (a, k) { return a + data[k].val; }, 0) / (names.reduce(function (a, k) { return a + data[k].done; }, 0) || 1);
    var groups = [
      { k: 'By channel', rows: names.map(function (k) { return [channelLabel(k), data[k].rate, chCol(k)]; }).filter(function (r) { return r[1] > 0; }).sort(function (a, b) { return b[1] - a[1]; }) },
      { k: 'Highest states', rows: states.slice(0, 5).map(function (r) { return [titleCase(r.name), r.rate, tok('--good')]; }) }
    ];
    var cap = Math.max(320, Math.ceil((avg * 1.8) / 50) * 50);
    rl.innerHTML = groups.map(function (g) {
      var Wd = 300, Hd = Math.max(90, g.rows.length * 26 + 16), ld = 104, iwd = Wd - ld - 56;
      var xv = function (v) { return ld + iwd * Math.min(v, cap) / cap; };
      var q = target ? '<line x1="' + xv(target) + '" x2="' + xv(target) + '" y1="4" y2="' + (Hd - 10) + '" stroke="' + tok('--wb-ink3') + '" stroke-dasharray="3 4"/>' : '';
      g.rows.forEach(function (r2, i) {
        var y = 12 + i * 26, base = target ? xv(target) : ld;
        q += '<text class="wb-ax" x="' + (ld - 8) + '" y="' + (y + 4) + '" text-anchor="end">' + esc(r2[0]) + '</text>' +
          '<line x1="' + base + '" x2="' + xv(r2[1]) + '" y1="' + y + '" y2="' + y + '" stroke="' + r2[2] + '" stroke-width="3" stroke-linecap="round"/>' +
          '<circle cx="' + xv(r2[1]) + '" cy="' + y + '" r="5" fill="' + r2[2] + '" stroke="#fff" stroke-width="2"><title>' + esc(r2[0]) + ' · ₹' + r2[1].toFixed(2) + ' per litre</title></circle>' +
          '<text class="wb-axn" x="' + (xv(r2[1]) + 10) + '" y="' + (y + 4) + '">₹' + num(r2[1]) + (r2[1] > cap ? ' ▸' : '') + '</text>';
      });
      return '<article class="wb-sm" style="cursor:default"><h3>' + g.k + (g.k === 'By channel' ? '<span>avg ₹' + avg.toFixed(2) + '</span>' : '') + '</h3>' +
        '<svg viewBox="0 0 ' + Wd + ' ' + Hd + '" role="img" aria-label="' + g.k + '">' + q + '</svg></article>';
    }).join('');

    /* Card 3. Plain and concrete: how much money each channel brought in over
       the selected range. One bar per channel in the channel's own colour,
       biggest first, the amount at the end of the bar and its share of the
       total under the name. It pairs with card 1 - ₹ per litre there, total ₹
       here - and needs no legend to read. */
    (function () {
      var rows = names.map(function (k) { return [k, data[k].val]; })
                      .filter(function (r) { return r[1] > 0; })
                      .sort(function (a, b) { return b[1] - a[1]; });
      if (!rows.length) return;
      var tot = rows.reduce(function (a, r) { return a + r[1]; }, 0), max = rows[0][1];
      var Wd = 300, Hd = rows.length * 34 + 12, ld = 104, iwd = Wd - ld - 84;
      var q = '';
      rows.forEach(function (r, i) {
        var y = 10 + i * 34, w = Math.max(3, iwd * r[1] / max), name = channelLabel(r[0]);
        q += '<text class="wb-ax" x="' + (ld - 8) + '" y="' + (y + 9) + '" text-anchor="end">' + esc(name) + '</text>' +
             '<text class="wb-ax" x="' + (ld - 8) + '" y="' + (y + 22) + '" text-anchor="end" style="fill:' + tok('--wb-ink3') + '">' + (r[1] / tot * 100).toFixed(1) + '%</text>' +
             '<rect x="' + ld + '" y="' + y + '" width="' + w + '" height="16" rx="4" fill="' + chCol(r[0]) + '"><title>' + esc(name) + ' · ' + money(r[1]) + '</title></rect>' +
             '<text class="wb-axn" x="' + (ld + w + 8) + '" y="' + (y + 12) + '">' + money(r[1]) + '</text>';
      });
      rl.innerHTML += '<article class="wb-sm" style="cursor:default"><h3>Revenue by channel<span>total ' + money(tot) + '</span></h3>' +
        '<svg viewBox="0 0 ' + Wd + ' ' + Hd + '" role="img" aria-label="Revenue by channel">' + q + '</svg>' +
        '<div class="f"><span>₹ earned in the selected range · biggest first</span></div></article>';
    })();
    var osub = document.getElementById('wbOverviewSub');
    if (osub) osub.textContent = (activeChannel ? channelLabel(activeChannel) + ' only' : 'All channels') + ' · done vs open, one scale across every card';
  }

  /* hook: every time the map rebuilds, rebuild the panes from the same rows */
  var _wbOrigBuild = build;
  build = function () {
    _wbOrigBuild.apply(this, arguments);
    if (typeof getFilteredChannelRows !== 'function') return;
    var all = getFilteredChannelRows() || [];
    fetchExtra().then(function (extra) { wbRender(all, extra); wbKpis(); });
  };

  schedule();
})();
