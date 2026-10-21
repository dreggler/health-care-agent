(function () {
  'use strict';


  const PALETTE = ['blue', 'green', 'orange', 'purple', 'pink', 'teal', 'indigo', 'red', 'yellow'];
  const COLOR_HEX = {
    blue: '#007aff', green: '#34c759', red: '#ff3b30', orange: '#ff9500',
    purple: '#af52de', pink: '#ff2d55', teal: '#5ac8fa', indigo: '#5856d6', yellow: '#ffcc00',
    gray: 'rgba(120,120,128,0.65)'
  };

  function resolveColor(name, fallback) {
    if (!name) return fallback || COLOR_HEX.blue;
    if (COLOR_HEX[name]) return COLOR_HEX[name];
    return name;
  }

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function mustEl(sel) {
    const el = document.querySelector(sel);
    if (!el) console.warn('[HimeUI] container not found:', sel);
    return el;
  }


  var _bridge = {
    active: (function () {
      try { return !!window.parent && window.parent !== window; }
      catch (e) { return false; }
    })(),
    proven: false,
    seq: 0,
    pending: {},
    timeoutMs: 10000
  };

  if (_bridge.active) {
    window.addEventListener('message', function (ev) {
      if (ev.source !== window.parent) return;
      var d = ev.data;
      if (!d || d.type !== 'hime:data-response') return;
      var p = _bridge.pending[d.requestId];
      if (!p) return;
      delete _bridge.pending[d.requestId];
      clearTimeout(p.timer);
      _bridge.proven = true;
      if (d.ok) p.resolve(d.payload === undefined ? {} : d.payload);
      else p.reject(new Error(d.error || ('HTTP ' + (d.status || '?'))));
    });
  }

  function _bridgeRequest(req) {
    return new Promise(function (resolve, reject) {
      var id = 'hime-' + (++_bridge.seq) + '-' + Math.random().toString(36).slice(2, 8);
      var timer = setTimeout(function () {
        delete _bridge.pending[id];
        if (_bridge.proven) {
          reject(new Error('Request timed out'));
          return;
        }
        _bridge.active = false;
        _directRequest(req).then(resolve, reject);
      }, _bridge.timeoutMs);
      _bridge.pending[id] = { resolve: resolve, reject: reject, timer: timer };
      window.parent.postMessage({
        type: 'hime:data-request',
        requestId: id,
        pageId: req.pageId,
        method: req.method,
        params: req.params || null,
        body: req.body === undefined ? null : req.body
      }, '*');
    });
  }

  var _selfToken = (function () {
    try {
      if (window.parent && window.parent !== window) return '';
      return new URLSearchParams(window.location.search).get('token') || '';
    } catch (e) { return ''; }
  })();

  function _withToken(url) {
    if (!_selfToken) return url;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + 'token=' + encodeURIComponent(_selfToken);
  }

  async function _directRequest(req) {
    const url = '/api/personalised-pages/' + encodeURIComponent(req.pageId) + '/data';
    if (req.method === 'POST') {
      const resp = await fetch(_withToken(url), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req.body || {})
      });
      let payload = null;
      try { payload = await resp.json(); } catch (e) { payload = null; }
      if (!resp.ok) {
        const msg = (payload && (payload.detail || payload.error)) || ('HTTP ' + resp.status);
        throw new Error(msg);
      }
      return payload || {};
    }
    let getUrl = url;
    if (req.params && Object.keys(req.params).length) {
      getUrl += '?' + new URLSearchParams(req.params).toString();
    }
    const resp = await fetch(_withToken(getUrl), { credentials: 'same-origin' });
    if (!resp.ok) {
      let msg = 'HTTP ' + resp.status;
      try { const j = await resp.json(); if (j && j.detail) msg = j.detail; } catch (e) {}
      throw new Error(msg);
    }
    return resp.json();
  }

  function _request(req) {
    return _bridge.active ? _bridgeRequest(req) : _directRequest(req);
  }

  function fetchData(pageId, params) {
    return _request({ method: 'GET', pageId: pageId, params: params || null });
  }

  function postData(pageId, body) {
    return _request({ method: 'POST', pageId: pageId, body: body || {} });
  }


  let _toastEl = null;
  let _toastTimer = null;
  function toast(msg, variant) {
    if (!_toastEl) {
      _toastEl = document.createElement('div');
      _toastEl.className = 'hime-toast';
      document.body.appendChild(_toastEl);
    }
    _toastEl.className = 'hime-toast ' + (variant || 'info');
    _toastEl.textContent = String(msg);
    void _toastEl.offsetWidth;
    _toastEl.classList.add('visible');
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => {
      _toastEl.classList.remove('visible');
    }, 2400);
  }


  let _sheetEls = null;
  let _sheetEscHandler = null;
  function showSheet(html, opts) {
    opts = opts || {};
    if (!_sheetEls) {
      const backdrop = document.createElement('div');
      backdrop.className = 'hime-sheet-backdrop';
      const sheet = document.createElement('div');
      sheet.className = 'hime-sheet';
      sheet.innerHTML =
        '<div class="hime-sheet-handle"></div>' +
        '<h3 class="hime-sheet-title"></h3>' +
        '<div class="hime-sheet-body"></div>';
      document.body.appendChild(backdrop);
      document.body.appendChild(sheet);
      backdrop.addEventListener('click', hideSheet);
      _sheetEls = { backdrop, sheet,
        title: sheet.querySelector('.hime-sheet-title'),
        body: sheet.querySelector('.hime-sheet-body') };
    }
    _sheetEls.title.style.display = opts.title ? '' : 'none';
    _sheetEls.title.textContent = opts.title || '';
    _sheetEls.body.innerHTML = html || '';
    requestAnimationFrame(() => {
      _sheetEls.backdrop.classList.add('open');
      _sheetEls.sheet.classList.add('open');
    });
    if (_sheetEscHandler) document.removeEventListener('keydown', _sheetEscHandler);
    _sheetEscHandler = (e) => { if (e.key === 'Escape') hideSheet(); };
    document.addEventListener('keydown', _sheetEscHandler);
  }

  function hideSheet() {
    if (!_sheetEls) return;
    _sheetEls.backdrop.classList.remove('open');
    _sheetEls.sheet.classList.remove('open');
    if (_sheetEscHandler) {
      document.removeEventListener('keydown', _sheetEscHandler);
      _sheetEscHandler = null;
    }
  }


  function _toDate(ts) {
    const n = Number(ts);
    if (!isFinite(n)) return null;
    return new Date(n > 1e12 ? n : n * 1000);
  }

  function formatTime(ts, fmt) {
    const d = _toDate(ts);
    if (!d || isNaN(d.getTime())) return '—';
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const pad = (n) => String(n).padStart(2, '0');
    switch (fmt) {
      case 'time':
        return pad(d.getHours()) + ':' + pad(d.getMinutes());
      case 'date':
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
      case 'relative': {
        const diff = (Date.now() - d.getTime()) / 1000;
        if (diff < 60) return 'just now';
        if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
        if (diff < 86400 * 7) return Math.floor(diff / 86400) + 'd ago';
        return months[d.getMonth()] + ' ' + d.getDate();
      }
      case 'short':
      default:
        return months[d.getMonth()] + ' ' + d.getDate();
    }
  }

  function formatNum(n, decimals) {
    if (n === null || n === undefined || n === '') return '—';
    const num = Number(n);
    if (!isFinite(num)) return '—';
    const d = (decimals === undefined || decimals === null) ? 1 : decimals;
    try { return num.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }); }
    catch (e) { return num.toFixed(d); }
  }


  function badge(text, color) {
    const cls = COLOR_HEX[color] ? ('hime-bg-' + color) : 'hime-bg-blue';
    return '<span class="hime-badge ' + cls + '">' + esc(text) + '</span>';
  }

  function progress(value, max, color) {
    const v = Math.max(0, Math.min(1, (Number(value) || 0) / (Number(max) || 1)));
    const pct = (v * 100).toFixed(1);
    const fill = resolveColor(color, COLOR_HEX.blue);
    return '<div class="hime-progress"><div class="fill" style="width:' + pct + '%;background:' + fill + ';"></div></div>';
  }

  function table(headers, rows) {
    const head = '<thead><tr>' + (headers || []).map(h => '<th>' + esc(h) + '</th>').join('') + '</tr></thead>';
    const body = '<tbody>' + (rows || []).map(r =>
      '<tr>' + (r || []).map(c => '<td>' + (typeof c === 'string' && c.indexOf('<') === 0 ? c : esc(c)) + '</td>').join('') + '</tr>'
    ).join('') + '</tbody>';
    return '<table class="hime-table">' + head + body + '</table>';
  }

  function progressRing(value, max, opts) {
    opts = opts || {};
    const size = opts.size || 80;
    const stroke = Math.max(4, Math.round(size / 12));
    const r = (size - stroke) / 2;
    const c = 2 * Math.PI * r;
    const v = Math.max(0, Math.min(1, (Number(value) || 0) / (Number(max) || 1)));
    const dash = (c * v).toFixed(2);
    const color = resolveColor(opts.color, COLOR_HEX.blue);
    const label = opts.label !== undefined ? opts.label : Math.round(v * 100) + '%';
    return (
      '<div class="hime-progress-ring" style="width:' + size + 'px;height:' + size + 'px;">' +
        '<svg width="' + size + '" height="' + size + '">' +
          '<circle cx="' + (size/2) + '" cy="' + (size/2) + '" r="' + r + '" stroke="rgba(120,120,128,0.18)" stroke-width="' + stroke + '" fill="none"/>' +
          '<circle cx="' + (size/2) + '" cy="' + (size/2) + '" r="' + r + '" stroke="' + color + '" stroke-width="' + stroke + '" stroke-linecap="round" fill="none" stroke-dasharray="' + dash + ' ' + c.toFixed(2) + '"/>' +
        '</svg>' +
        '<div class="label">' + esc(label) + '</div>' +
      '</div>'
    );
  }


  function drawChart(sel, spec) {
    const host = (typeof sel === 'string') ? mustEl(sel) : sel;
    if (!host) return;
    if (typeof CanvasRenderingContext2D !== 'undefined' && host instanceof CanvasRenderingContext2D) {
      console.error('[HimeUI.drawChart] sel is a CanvasRenderingContext2D. HimeUI.drawChart is NOT Chart.js — pass a <div> selector or element instead, e.g. HimeUI.drawChart("#myChart", {...}). The function will create its own <canvas> inside that div.');
      return;
    }
    if (host instanceof HTMLCanvasElement) {
      console.error('[HimeUI.drawChart] sel is a <canvas>. HimeUI.drawChart is NOT Chart.js — replace <canvas id="x"> with <div id="x"> and pass that. The function injects its own canvas inside the div.');
      return;
    }
    if (!(host instanceof HTMLElement)) {
      console.error('[HimeUI.drawChart] sel must be a selector string or HTMLElement; got', host);
      return;
    }
    if (spec && !drawChart._loggedSpecHint) {
      const stray = [];
      if (spec.options) stray.push('options');
      if (spec.data) stray.push('data (use top-level labels/datasets)');
      const ds0 = spec.datasets && spec.datasets[0];
      if (ds0) {
        ['backgroundColor','borderColor','borderWidth','pointRadius','tension','fill','yAxisID','borderDash','pointBackgroundColor']
          .forEach(k => { if (k in ds0) stray.push('datasets[].' + k); });
      }
      if (stray.length) {
        drawChart._loggedSpecHint = true;
        console.warn('[HimeUI.drawChart] ignoring Chart.js-only fields:', stray.join(', '), '— HimeUI.drawChart only honors {type, labels, datasets:[{label, data, color?, type?, axis?}]}. See create_page_guide.md.');
      }
    }
    const labels = (spec && spec.labels) || [];
    const datasets = (spec && spec.datasets) || [];
    const type = (spec && spec.type) || 'line';

    if (!labels.length || !datasets.length) {
      host.innerHTML = '<div class="hime-chart-empty">No data</div>';
      return;
    }

    host.innerHTML =
      '<div class="hime-chart-canvas-wrap"><canvas></canvas></div>' +
      '<div class="hime-chart-legend">' +
        datasets.map((ds, i) => {
          const c = resolveColor(ds.color, COLOR_HEX[PALETTE[i % PALETTE.length]]);
          const axisTag = ds.axis === 'right' ? ' <span class="hime-axis-tag">R</span>' : '';
          return '<span><span class="swatch" style="background:' + c + '"></span>' + esc(ds.label || ('Series ' + (i+1))) + axisTag + '</span>';
        }).join('') +
      '</div>';

    const canvas = host.querySelector('canvas');
    _drawChart(canvas, type, labels, datasets);
    if (!host._himeResizeBound) {
      host._himeResizeBound = true;
      window.addEventListener('resize', () => _drawChart(canvas, type, labels, datasets));
    }
  }

  function chart(sel, spec) {
    if (!chart._deprecationLogged) {
      chart._deprecationLogged = true;
      console.warn('[HimeUI.chart] is deprecated; use HimeUI.drawChart instead (same signature). The "chart" name collides with Chart.js and has caused confusion.');
    }
    return drawChart(sel, spec);
  }

  function _drawChart(canvas, type, labels, datasets) {
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth || 320;
    const H = canvas.clientHeight || 220;
    canvas.width = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const hasRight = datasets.some(ds => ds.axis === 'right');
    const padL = 36, padR = hasRight ? 36 : 12, padT = 12, padB = 24;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    function rangeOf(filterFn) {
      let lo = Infinity, hi = -Infinity;
      datasets.forEach(ds => {
        if (!filterFn(ds)) return;
        (ds.data || []).forEach(v => {
          const n = Number(v);
          if (isFinite(n)) { if (n < lo) lo = n; if (n > hi) hi = n; }
        });
      });
      if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
      if (lo === hi) { lo -= 1; hi += 1; }
      const range = hi - lo;
      return [lo - range * 0.05, hi + range * 0.05];
    }
    const [loL, hiL] = rangeOf(ds => ds.axis !== 'right');
    const [loR, hiR] = hasRight ? rangeOf(ds => ds.axis === 'right') : [0, 1];

    const isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const axisColor = isDark ? 'rgba(235,235,245,0.3)' : 'rgba(60,60,67,0.3)';
    const gridColor = isDark ? 'rgba(235,235,245,0.12)' : 'rgba(60,60,67,0.08)';

    ctx.strokeStyle = gridColor;
    ctx.fillStyle = axisColor;
    ctx.font = '10px -apple-system, system-ui, sans-serif';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = padT + (plotH * i) / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + plotW, y); ctx.stroke();
      const valL = hiL - ((hiL - loL) * i) / 4;
      ctx.fillText(_axisFmt(valL), 4, y + 3);
      if (hasRight) {
        const valR = hiR - ((hiR - loR) * i) / 4;
        ctx.fillText(_axisFmt(valR), padL + plotW + 4, y + 3);
      }
    }

    const step = Math.max(1, Math.ceil(labels.length / 6));
    for (let i = 0; i < labels.length; i += step) {
      const x = labels.length === 1 ? padL + plotW / 2 : padL + (plotW * i) / (labels.length - 1);
      ctx.fillText(String(labels[i]), x - 12, H - 6);
    }

    const xAt = (i) => labels.length === 1 ? padL + plotW / 2 : padL + (plotW * i) / (labels.length - 1);
    const yAtL = (v) => padT + plotH * (1 - (v - loL) / (hiL - loL));
    const yAtR = (v) => padT + plotH * (1 - (v - loR) / (hiR - loR));

    const barIdxList = [];
    datasets.forEach((ds, i) => { if ((ds.type || type) === 'bar') barIdxList.push(i); });
    const barCount = barIdxList.length;

    datasets.forEach((ds, di) => {
      const color = resolveColor(ds.color, COLOR_HEX[PALETTE[di % PALETTE.length]]);
      const data = (ds.data || []).map(Number);
      const dsType = ds.type || type;
      const onRight = ds.axis === 'right';
      const yAt = onRight ? yAtR : yAtL;
      const axisLo = onRight ? loR : loL;

      if (dsType === 'bar') {
        const groupW = plotW / labels.length;
        const barSlot = barIdxList.indexOf(di);
        const barW = Math.max(2, (groupW * 0.7) / Math.max(1, barCount));
        ctx.fillStyle = color;
        data.forEach((v, i) => {
          if (!isFinite(v)) return;
          const x = padL + groupW * i + (groupW - barW * barCount) / 2 + barSlot * barW;
          const y = yAt(v);
          const baseY = yAt(Math.max(axisLo, 0));
          ctx.fillRect(x, Math.min(y, baseY), barW - 1, Math.abs(baseY - y));
        });
      } else {
        ctx.beginPath();
        let first = true;
        data.forEach((v, i) => {
          if (!isFinite(v)) return;
          const x = xAt(i), y = yAt(v);
          if (first) { ctx.moveTo(x, y); first = false; } else { ctx.lineTo(x, y); }
        });
        if (dsType === 'area') {
          const lastX = xAt(data.length - 1), firstX = xAt(0);
          ctx.lineTo(lastX, padT + plotH);
          ctx.lineTo(firstX, padT + plotH);
          ctx.closePath();
          ctx.fillStyle = _hexAlpha(color, 0.18);
          ctx.fill();
          ctx.beginPath();
          first = true;
          data.forEach((v, i) => {
            if (!isFinite(v)) return;
            const x = xAt(i), y = yAt(v);
            if (first) { ctx.moveTo(x, y); first = false; } else { ctx.lineTo(x, y); }
          });
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.lineJoin = 'round';
        ctx.stroke();
      }
    });
  }

  function _axisFmt(v) {
    if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + 'k';
    if (Math.abs(v) >= 100) return v.toFixed(0);
    if (Math.abs(v) >= 10) return v.toFixed(1);
    return v.toFixed(2);
  }

  function _hexAlpha(c, a) {
    if (c.startsWith('#') && c.length === 7) {
      const r = parseInt(c.slice(1,3),16), g = parseInt(c.slice(3,5),16), b = parseInt(c.slice(5,7),16);
      return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
    }
    if (c.startsWith('rgba(')) return c.replace(/,[^,]+\)$/, ',' + a + ')');
    if (c.startsWith('rgb(')) return c.replace('rgb(', 'rgba(').replace(')', ',' + a + ')');
    return c;
  }


  function MetricGrid(sel, items) {
    const host = mustEl(sel);
    if (!host) return;
    host.classList.add('hime-metric-grid');
    if (!Array.isArray(items) || !items.length) {
      host.innerHTML = '<div class="hime-metric-card hime-caption" style="grid-column:1/-1">No data</div>';
      return;
    }
    host.innerHTML = items.map((it, i) => {
      const colorName = it.color || PALETTE[i % PALETTE.length];
      const iconCls = 'hime-bg-' + (COLOR_HEX[colorName] ? colorName : 'blue');
      const iconHtml = it.icon ? '<span class="hime-metric-icon ' + iconCls + '">' + esc(String(it.icon).slice(0,2)) + '</span>' : '';
      const trendDir = (typeof it.trend === 'string' && it.trend.startsWith('-')) ? 'down'
                     : (typeof it.trend === 'string' && it.trend.startsWith('+')) ? 'up' : '';
      const trendHtml = it.trend ? '<div class="hime-metric-trend ' + trendDir + '">' + esc(it.trend) + '</div>' : '';
      const unitHtml = it.unit ? '<span class="hime-metric-unit">' + esc(it.unit) + '</span>' : '';
      const wideCls = it.wide ? ' hime-metric-card-wide' : '';
      return (
        '<div class="hime-metric-card' + wideCls + '">' +
          '<div class="hime-metric-header"><span>' + esc(it.label || '') + '</span>' + iconHtml + '</div>' +
          '<div class="hime-metric-value">' + esc(it.value === undefined ? '—' : it.value) + unitHtml + '</div>' +
          trendHtml +
        '</div>'
      );
    }).join('');
  }


  function Section(sel, opts) {
    const host = mustEl(sel);
    if (!host) return null;
    opts = opts || {};
    const bodyId = 'hime_sec_' + Math.random().toString(36).slice(2, 9);
    const iconHtml = opts.icon ? esc(opts.icon) + ' ' : '';
    const badgeHtml = opts.badge ? ' ' + badge(opts.badge, opts.badgeColor || 'gray') : '';
    const wrapped = opts.cardTitle !== undefined;
    const cardTitleHtml = (wrapped && opts.cardTitle)
      ? '<div class="hime-card-title">' + esc(opts.cardTitle) + '</div>' : '';
    const bodyHtml = '<div id="' + bodyId + '"></div>';
    host.innerHTML =
      '<div class="hime-section-title">' + iconHtml + esc(opts.title || '') + badgeHtml + '</div>' +
      (wrapped ? '<div class="hime-card">' + cardTitleHtml + bodyHtml + '</div>' : bodyHtml);
    return { bodySelector: '#' + bodyId, bodyEl: document.getElementById(bodyId) };
  }


  function DetailList(sel, opts) {
    const host = mustEl(sel);
    if (!host) return;
    opts = opts || {};
    const items = Array.isArray(opts.items) ? opts.items : [];
    host.classList.add('hime-detail-list');

    const titleHtml = opts.sectionTitle
      ? '<div class="hime-section-title">' + esc(opts.sectionTitle) + '</div>' : '';

    const hasAction = typeof opts.onAction === 'function';

    const rows = items.map((it, idx) => {
      const colorName = it.iconColor || PALETTE[idx % PALETTE.length];
      const iconCls = 'hime-bg-' + (COLOR_HEX[colorName] ? colorName : 'blue');
      const iconHtml = it.icon ? '<span class="hime-row-icon ' + iconCls + '">' + esc(String(it.icon).slice(0,2)) + '</span>' : '';
      let rightHtml = '';
      if (it.right !== undefined && it.right !== null && it.right !== '') {
        rightHtml = '<div class="hime-row-right">' + esc(it.right) + '</div>';
      } else if (hasAction) {
        rightHtml = '<div class="hime-row-right"><span class="hime-chevron"></span></div>';
      }
      const badgeHtml = it.badge ? ' ' + badge(it.badge, it.badgeColor || 'blue') : '';
      const subHtml = it.subtitle ? '<div class="hime-row-subtitle">' + esc(it.subtitle) + '</div>' : '';
      const actionable = hasAction ? ' actionable' : '';
      return (
        '<div class="hime-row' + actionable + '" data-idx="' + idx + '">' +
          iconHtml +
          '<div class="hime-row-body">' +
            '<div class="hime-row-title">' + esc(it.title || '') + badgeHtml + '</div>' +
            subHtml +
          '</div>' +
          rightHtml +
        '</div>'
      );
    }).join('');

    host.innerHTML = titleHtml + '<div class="hime-list-group">' + rows + '</div>';

    if (hasAction) {
      host.querySelectorAll('.hime-row.actionable').forEach(row => {
        row.addEventListener('click', () => {
          const idx = Number(row.getAttribute('data-idx'));
          const it = items[idx];
          if (!it) return;
          if (it.detail !== undefined && it.detail !== null) {
            showSheet(_renderDetailBlock(it.detail), { title: it.title || '' });
          }
          try { opts.onAction(it, idx); } catch (e) { console.error(e); }
        });
      });
    }
  }

  function _renderDetailBlock(detail) {
    if (typeof detail === 'string') return '<div class="hime-detail-block"><div class="db-text">' + detail + '</div></div>';
    if (!Array.isArray(detail)) return '';
    const parts = detail.map(block => {
      switch (block.type) {
        case 'text':    return '<div class="db-text">' + esc(block.text || '') + '</div>';
        case 'header':  return '<div class="db-header">' + esc(block.text || '') + '</div>';
        case 'divider': return '<div class="db-divider"></div>';
        case 'steps':   return '<ol class="db-steps">' + (block.items || []).map(s => '<li>' + esc(s) + '</li>').join('') + '</ol>';
        case 'metrics': return '<div class="db-metrics">' + (block.items || []).map(m =>
          '<div class="db-metric"><div class="db-metric-label">' + esc(m.label || '') + '</div>' +
          '<div class="db-metric-value">' + esc(m.value === undefined ? '—' : m.value) +
          (m.unit ? '<span class="db-metric-unit">' + esc(m.unit) + '</span>' : '') + '</div></div>'
        ).join('') + '</div>';
        case 'button': {
          const c = COLOR_HEX[block.color] ? ('hime-bg-' + block.color) : 'hime-bg-blue';
          return '<button class="hime-button hime-button-block ' + c + '" style="color:#fff" data-action-id="' +
            esc(block.id || '') + '">' + esc(block.label || 'OK') + '</button>';
        }
        case 'alert': {
          const c = resolveColor(block.color, COLOR_HEX.orange);
          return '<div class="db-alert" style="background:' + c + '">' + esc(block.text || '') + '</div>';
        }
        default: return '';
      }
    });
    return '<div class="hime-detail-block">' + parts.join('') + '</div>';
  }


  function ChartView(sel, opts) {
    const host = mustEl(sel);
    if (!host) return;
    opts = opts || {};
    const periods = Array.isArray(opts.periods) && opts.periods.length ? opts.periods : ['1W', '1M', '3M'];
    const initial = opts.defaultPeriod || periods[0];
    const dataMap = (typeof opts.dataMap === 'function') ? opts.dataMap : (r => r);
    host.classList.add('hime-chart-view');

    host.innerHTML =
      '<div class="hime-segmented-control">' +
        periods.map(p => '<button data-p="' + esc(p) + '"' + (p === initial ? ' class="active"' : '') + '>' + esc(p) + '</button>').join('') +
      '</div>' +
      '<div class="hime-chart-body"><div class="hime-chart-empty">Loading…</div></div>';

    const body = host.querySelector('.hime-chart-body');
    const buttons = host.querySelectorAll('.hime-segmented-control button');

    async function load(period) {
      try {
        const raw = await fetchData(opts.pageId, { period });
        const mapped = dataMap(raw) || {};
        drawChart(body, { type: opts.type || 'line', labels: mapped.labels || [], datasets: mapped.datasets || [] });
      } catch (e) {
        body.innerHTML = '<div class="hime-chart-empty">' + esc(e.message || 'Failed to load') + '</div>';
      }
    }

    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        load(btn.getAttribute('data-p'));
      });
    });

    load(initial);
  }


  function _buildField(f) {
    const id = 'hf_' + Math.random().toString(36).slice(2, 8);
    const label = '<div class="hime-field-label">' + esc(f.label || f.name) + '</div>';
    let control = '';
    let getValue;
    switch (f.type) {
      case 'number': {
        const v = f.value !== undefined ? f.value : '';
        const minA = (f.min !== undefined) ? ' min="' + esc(f.min) + '"' : '';
        const maxA = (f.max !== undefined) ? ' max="' + esc(f.max) + '"' : '';
        control = '<input id="' + id + '" type="number" value="' + esc(v) + '" placeholder="' + esc(f.placeholder || '') + '"' + minA + maxA + '/>';
        getValue = () => { const el = document.getElementById(id); const n = Number(el.value); return el.value === '' ? null : n; };
        break;
      }
      case 'slider': {
        const min = f.min !== undefined ? f.min : 0;
        const max = f.max !== undefined ? f.max : 100;
        const v = f.value !== undefined ? f.value : min;
        control = '<input id="' + id + '" type="range" min="' + esc(min) + '" max="' + esc(max) + '" value="' + esc(v) + '"/>' +
                  '<span class="hime-field-display" id="' + id + '_d">' + esc(f.display ? f.display(v) : v) + '</span>';
        setTimeout(() => {
          const r = document.getElementById(id);
          const d = document.getElementById(id + '_d');
          if (r && d) r.addEventListener('input', () => { d.textContent = f.display ? f.display(Number(r.value)) : r.value; });
        }, 0);
        getValue = () => Number(document.getElementById(id).value);
        break;
      }
      case 'select': {
        const opts = (f.options || []).map(o => {
          const ov = (typeof o === 'object') ? o.value : o;
          const ol = (typeof o === 'object') ? o.label : o;
          const sel = (f.value !== undefined && String(f.value) === String(ov)) ? ' selected' : '';
          return '<option value="' + esc(ov) + '"' + sel + '>' + esc(ol) + '</option>';
        }).join('');
        control = '<select id="' + id + '">' + opts + '</select>';
        getValue = () => document.getElementById(id).value;
        break;
      }
      case 'toggle': {
        const on = !!f.value;
        control = '<label class="hime-switch' + (on ? ' on' : '') + '" id="' + id + '_w"><input type="checkbox" id="' + id + '"' + (on ? ' checked' : '') + '/><span class="knob"></span></label>';
        setTimeout(() => {
          const w = document.getElementById(id + '_w');
          const i = document.getElementById(id);
          if (w && i) w.addEventListener('click', (e) => {
            e.preventDefault();
            i.checked = !i.checked;
            w.classList.toggle('on', i.checked);
          });
        }, 0);
        getValue = () => document.getElementById(id).checked;
        break;
      }
      case 'textarea':
        control = '<textarea id="' + id + '" placeholder="' + esc(f.placeholder || '') + '">' + esc(f.value || '') + '</textarea>';
        getValue = () => document.getElementById(id).value;
        break;
      case 'date':
        control = '<input id="' + id + '" type="date" value="' + esc(f.value || '') + '"/>';
        getValue = () => document.getElementById(id).value;
        break;
      case 'time':
        control = '<input id="' + id + '" type="time" value="' + esc(f.value || '') + '"/>';
        getValue = () => document.getElementById(id).value;
        break;
      case 'text':
      default:
        control = '<input id="' + id + '" type="text" value="' + esc(f.value || '') + '" placeholder="' + esc(f.placeholder || '') + '"/>';
        getValue = () => document.getElementById(id).value;
        break;
    }
    return {
      html: '<div class="hime-field">' + label + '<div class="hime-field-control">' + control + '</div></div>',
      getValue, name: f.name
    };
  }

  function InputForm(sel, opts) {
    const host = mustEl(sel);
    if (!host) return;
    opts = opts || {};
    host.classList.add('hime-input-form');
    const fields = (opts.fields || []).map(_buildField);
    host.innerHTML =
      '<div class="hime-form-group">' + fields.map(f => f.html).join('') + '</div>' +
      '<button class="hime-button hime-button-primary hime-button-block">' + esc(opts.submitLabel || 'Submit') + '</button>';

    const btn = host.querySelector('.hime-button-primary');
    btn.addEventListener('click', async () => {
      const body = Object.assign({}, opts.extraData || {});
      fields.forEach(f => { body[f.name] = f.getValue(); });
      btn.disabled = true;
      try {
        const resp = await postData(opts.pageId, body);
        if (typeof opts.onSuccess === 'function') opts.onSuccess(resp);
        else toast('Saved', 'success');
      } catch (e) {
        toast(e.message || 'Failed', 'error');
      } finally {
        btn.disabled = false;
      }
    });
  }


  function Tracker(sel, opts) {
    const host = mustEl(sel);
    if (!host) return;
    opts = opts || {};
    host.classList.add('hime-tracker');
    host.innerHTML =
      '<div class="hime-tracker-form"></div>' +
      '<div class="hime-section-title">History</div>' +
      '<div class="hime-tracker-history"><div class="hime-tracker-empty">Loading…</div></div>';
    const formHost = host.querySelector('.hime-tracker-form');
    const histHost = host.querySelector('.hime-tracker-history');
    const histKey = opts.historyKey || 'history';

    async function refresh() {
      try {
        const data = await fetchData(opts.pageId);
        const list = (data && data[histKey]) || [];
        if (!list.length) {
          histHost.innerHTML = '<div class="hime-tracker-empty">' + esc(opts.emptyText || 'No entries yet') + '</div>';
          return;
        }
        const renderer = opts.historyRender || ((it) => ({ title: String(it) }));
        const rows = list.map((it, idx) => {
          const r = renderer(it, idx) || {};
          const colorName = r.iconColor || PALETTE[idx % PALETTE.length];
          const iconCls = 'hime-bg-' + (COLOR_HEX[colorName] ? colorName : 'blue');
          const iconHtml = r.icon ? '<span class="hime-row-icon ' + iconCls + '">' + esc(String(r.icon).slice(0,2)) + '</span>' : '';
          const sub = r.subtitle ? '<div class="hime-row-subtitle">' + esc(r.subtitle) + '</div>' : '';
          const right = (r.right !== undefined && r.right !== null) ? '<div class="hime-row-right">' + esc(r.right) + '</div>' : '';
          return '<div class="hime-row">' + iconHtml +
            '<div class="hime-row-body"><div class="hime-row-title">' + esc(r.title || '') + '</div>' + sub + '</div>' +
            right + '</div>';
        }).join('');
        histHost.innerHTML = rows;
      } catch (e) {
        histHost.innerHTML = '<div class="hime-tracker-empty">' + esc(e.message || 'Failed to load') + '</div>';
      }
    }

    InputForm('#' + _ensureId(formHost), {
      pageId: opts.pageId,
      fields: opts.fields || [],
      submitLabel: opts.submitLabel || 'Add',
      extraData: opts.extraData,
      onSuccess: (resp) => { toast('Saved', 'success'); refresh(); if (typeof opts.onSuccess === 'function') opts.onSuccess(resp); }
    });

    refresh();
  }

  function _ensureId(el) {
    if (!el.id) el.id = 'hime_' + Math.random().toString(36).slice(2, 9);
    return el.id;
  }


  function renderError(msg) {
    const page = document.querySelector('.hime-page');
    if (!page) return;
    let banner = page.querySelector('.hime-error-banner');
    if (!banner) {
      banner = document.createElement('div');
      banner.className = 'hime-error-banner';
      const header = page.querySelector('.hime-header');
      if (header && header.nextSibling) page.insertBefore(banner, header.nextSibling);
      else page.insertBefore(banner, page.firstChild);
    }
    banner.innerHTML =
      '<div class="hime-error-banner-title">Couldn’t load this page</div>' +
      '<div class="hime-error-banner-msg">' + esc(String(msg || 'Unknown error')) + '</div>';
  }

  window.addEventListener('unhandledrejection', (ev) => {
    const reason = ev && ev.reason;
    const msg = (reason && (reason.message || reason)) || 'Request failed';
    renderError(msg);
  });

  window.HimeUI = {
    MetricGrid, Section, DetailList, ChartView, InputForm, Tracker,
    showSheet, hideSheet, toast, renderError,
    drawChart, progressRing, badge, progress, table,
    formatTime, formatNum, fetchData,
    chart
  };
})();
