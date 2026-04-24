'use strict';

// Returns the subset of tab.metrics that are present in state.meta.metrics,
// preserving order and returning full metric objects.
function resolvedTabMetrics(tab) {
  const byLabel = new Map(state.meta.metrics.map(m => [m.label, m]));
  return tab.metrics.flatMap(label => {
    const m = byLabel.get(label);
    return m ? [m] : [];
  });
}

// ── State ───────────────────────────────────────────────────────────────────
const state = {
  meta:           null,   // {hosts, metrics: [{label, chart, y_title, unit, command}], tabs}
  chartMap:       {},     // {series_label: chart_key} — populated from /api/data
  series:         {},     // {"hostname/label": [{ts, value, meta}]} — all series
  events:         [],     // [{id, timestamp, label, source, color, dash, end_timestamp}]
  metricPlots:    [],     // [{div, metrics: [metric,...], tabId, initialized}] — one entry per chart group
  activeTab:      null,
  isLive:         false,
  paused:         false,
  selectedHost:   null,
  editingEventId: null,
  spanStartId:    null,   // event id of the open span start, or null
};

// ── Host selector ───────────────────────────────────────────────────────────

function populateHostSelect() {
  const sel   = document.getElementById('host-select');
  const hosts = state.meta.hosts || [];
  sel.innerHTML = '';

  if (hosts.length === 0) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'historical';
    sel.appendChild(opt);
    state.selectedHost = null;
    return;
  }

  const placeholder = document.createElement('option');
  placeholder.value    = '';
  placeholder.textContent = 'Select host';
  placeholder.disabled = true;
  placeholder.selected = true;
  sel.appendChild(placeholder);

  hosts.forEach(h => {
    const opt = document.createElement('option');
    opt.value = h;
    opt.textContent = h;
    sel.appendChild(opt);
  });
  state.selectedHost = null;

  sel.addEventListener('change', () => {
    state.selectedHost = sel.value || null;
    if (state.metricPlots.length === 0) {
      initTabCharts();
    } else {
      refreshPlot();
    }
  });
}

// Return the series key for the selected host + a metric label.
// Falls back to bare label when there is no host prefix (historical data).
function seriesKey(metricLabel) {
  return state.selectedHost ? `${state.selectedHost}/${metricLabel}` : metricLabel;
}


// The chart area is the drawable rectangle where data lines are plotted.
// Total div height = CHART_AREA_HEIGHT + topMargin() + bottomMargin, where
// topMargin grows with event annotation label length and bottomMargin grows
// with the number of legend rows — so the chart area stays fixed.
const CHART_AREA_HEIGHT = 160;   // fixed height of the data-drawing area, all plots
const AXIS_BOTTOM_PX    = 40;    // space below chart for x-axis ticks/labels
const LEGEND_ROW_PX     = 20;    // height of one horizontal legend row (font-size 10)
const LEGEND_PAD_PX     = 4;     // padding below the last legend row
const ITEMS_PER_ROW     = 6;     // estimated legend items per row (horizontal)

// ── Plotly helpers ──────────────────────────────────────────────────────────

function plotTheme() {
  const light = document.body.classList.contains('light');
  return {
    paper: light ? '#f5f6fa' : '#0f1117',
    plot:  light ? '#eef0f7' : '#13151f',
    grid:  light ? '#d0d3e8' : '#2a2d3e',
    tick:  light ? '#555'    : '#aaa',
    axis:  light ? '#666'    : '#888',
    font:  light ? '#1a1a2e' : '#e0e0e0',
  };
}

function topMargin() {
  if (!state.events.length) return 36;
  const maxLen = Math.max(...state.events.map(ev => ev.label.length));
  // Annotations are rotated -45°; estimate vertical reach above anchor.
  return Math.max(36, Math.round(40 + maxLen * 4));
}

// Returns axis styling shared by all plots (x and y alike).
function sharedAxisStyle(t) {
  return { gridcolor: t.grid, zerolinecolor: t.grid, tickfont: { color: t.tick, size: 10 } };
}

// Count how many horizontal legend rows a set of traces will produce.
// Returns 0 when there is ≤1 named trace (no legend rendered).
function legendRows(traces) {
  const n = traces.filter(tr => tr.showlegend !== false && tr.name).length;
  return n > 1 ? Math.ceil(n / ITEMS_PER_ROW) : 0;
}

// Unified layout builder for every plot.
// The chart area (drawable rectangle) is always CHART_AREA_HEIGHT px.
// margin.t grows with annotation label length; margin.b grows with legend rows.
function buildLayout(traces, { yaxisTitle }) {
  const t    = plotTheme();
  const rows = legendRows(traces);
  // When there is no legend, only the x-axis ticks need bottom space.
  // When there is a legend, stack it below the x-axis ticks.
  const bMar = rows > 0
    ? AXIS_BOTTOM_PX + rows * LEGEND_ROW_PX + LEGEND_PAD_PX
    : AXIS_BOTTOM_PX;
  return {
    xaxis:         { type: 'date', hoverformat: '%H:%M:%S.%L', ...sharedAxisStyle(t) },
    paper_bgcolor: t.paper,
    plot_bgcolor:  t.plot,
    font:          { color: t.font },
    margin:        { t: topMargin(), b: bMar, l: 56, r: 20 },
    shapes:        buildShapes(),
    annotations:   buildAnnotations(),
    height:        CHART_AREA_HEIGHT + topMargin() + bMar,
    yaxis:         {
      title:     { text: yaxisTitle, font: { size: 11, color: t.axis } },
      rangemode: 'tozero',
      ...sharedAxisStyle(t),
    },
    showlegend: rows > 0,
    // Legend anchor (top edge) is placed just below the x-axis tick labels.
    // y is in Plotly's plot-area coordinates: 0 = chart bottom, negative = below.
    ...(rows > 0 && {
      legend: {
        orientation: 'h',
        y:           -(AXIS_BOTTOM_PX / CHART_AREA_HEIGHT),
        yanchor:     'top',
        font:        { size: 10, color: t.font },
      },
    }),
  };
}

function metaText(meta) {
  if (!meta) return '';
  return Object.entries(meta).map(([k, v]) => `${k}: ${v}`).join('<br>');
}

// Build one trace per metric in the group (for a shared Plotly chart).
function buildMetricTraces(metrics) {
  return metrics.map(metric => {
    const key  = seriesKey(metric.label);
    const pts  = state.series[key] || [];
    const name = metric.label.startsWith('proc/') ? metric.label.slice(5) : metric.label;
    const trace = {
      type:        'scattergl',
      mode:        'lines+markers',
      name,
      x:           pts.map(p => p.ts),
      y:           pts.map(p => p.value),
      connectgaps: false,
      line:        { width: 1.5 },
      marker:      { size: 3 },
    };
    if (pts.some(p => p.meta)) {
      trace.text = pts.map(p => metaText(p.meta));
      trace.hovertemplate = `<b>${name}</b>: %{y:.2f}${metric.unit}<br>%{text}<br>%{x}<extra></extra>`;
    } else {
      trace.hovertemplate = `<b>${name}</b>: %{y:.2f}${metric.unit}<br>%{x}<extra></extra>`;
    }
    return trace;
  });
}

function hexToRgba(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function buildShapes() {
  return state.events.flatMap(ev => {
    if (ev.end_timestamp) {
      // Span event: borderless filled rect + two vertical edge lines (left and right only)
      const edgeLine = { color: ev.color, width: 1, dash: ev.dash };
      return [
        {
          type:      'rect',
          xref:      'x',
          yref:      'paper',
          x0:        ev.timestamp,
          x1:        ev.end_timestamp,
          y0:        0,
          y1:        1,
          fillcolor: hexToRgba(ev.color, 0.12),
          line:      { width: 0 },
          layer:     'below',
        },
        { type: 'line', xref: 'x', yref: 'paper', x0: ev.timestamp,     x1: ev.timestamp,     y0: 0, y1: 1, line: edgeLine, layer: 'below' },
        { type: 'line', xref: 'x', yref: 'paper', x0: ev.end_timestamp, x1: ev.end_timestamp, y0: 0, y1: 1, line: edgeLine, layer: 'below' },
      ];
    }
    // Instantaneous event: single vertical line
    return [{
      type: 'line',
      xref: 'x',
      yref: 'paper',
      x0:   ev.timestamp, x1: ev.timestamp,
      y0:   0,            y1: 1,
      line: { color: ev.color, width: 1.5, dash: ev.dash },
    }];
  });
}

function buildAnnotations() {
  return state.events.map(ev => ({
    xref:      'x',
    yref:      'paper',
    x:         ev.timestamp,
    y:         1,
    yanchor:   'bottom',
    text:      ev.label,
    showarrow: false,
    textangle: -45,
    font:      { size: 9, color: ev.color },
  }));
}

// ── Expand / collapse ────────────────────────────────────────────────────────

function collapseExpanded() {
  document.querySelectorAll('.expanded-plot').forEach(el => el.classList.remove('expanded-plot'));
  document.querySelectorAll('.expanded-title').forEach(el => el.classList.remove('expanded-title'));
  document.querySelectorAll('.expanded-section').forEach(el => el.classList.remove('expanded-section'));
  document.body.classList.remove('plot-expanded');
  document.querySelectorAll('.expand-btn').forEach(b => { b.textContent = 'Expand'; });
  refreshPlot();
}

function toggleExpand(dividerEl, plotDiv, sectionEl, btn) {
  const isExpanded = plotDiv.classList.contains('expanded-plot');
  collapseExpanded();
  if (isExpanded) return;

  const header   = document.querySelector('header');
  const eventBar = document.getElementById('event-bar');
  const availH   = window.innerHeight - header.offsetHeight - eventBar.offsetHeight - dividerEl.offsetHeight;

  plotDiv.classList.add('expanded-plot');
  dividerEl.classList.add('expanded-title');
  sectionEl.classList.add('expanded-section');
  document.body.classList.add('plot-expanded');
  btn.textContent = 'Collapse';

  Plotly.relayout(plotDiv, { height: availH });
}

function makeExpandButton(dividerEl, plotDiv, sectionEl) {
  const btn = document.createElement('button');
  btn.className   = 'expand-btn';
  btn.textContent = 'Expand';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleExpand(dividerEl, plotDiv, sectionEl, btn);
  });
  dividerEl.appendChild(btn);
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.body.classList.contains('plot-expanded')) collapseExpanded();
});

// ── Tab management ──────────────────────────────────────────────────────────

function activateTab(tabId) {
  if (document.body.classList.contains('plot-expanded')) collapseExpanded();
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === `tab-${tabId}`);
  });
  // Apply tab state before initializing so the container has correct dimensions.
  state.activeTab = tabId;
  initTabPlots(tabId);
}

// Initialize Plotly plots for a tab the first time it becomes visible.
// Deferred so the container has correct dimensions (not display:none).
function initTabPlots(tabId) {
  const config = {
    responsive:             false,
    displaylogo:            false,
    modeBarButtonsToRemove: ['lasso2d', 'select2d'],
  };
  for (const mp of state.metricPlots) {
    if (mp.tabId !== tabId || mp.initialized) continue;
    mp.initialized = true;
    const traces = buildMetricTraces(mp.metrics);
    Plotly.newPlot(mp.div, traces, buildLayout(traces, { yaxisTitle: mp.metrics[0].y_title }), config);
    mp.div.on('plotly_clickannotation', (data) => {
      const ev = state.events[data.index];
      if (!ev) return;
      openPopover(ev, data.event);
    });
    const dividerEl = mp.div.previousElementSibling;
    const sectionEl = mp.div.closest('.tab-charts');
    if (dividerEl && sectionEl) makeExpandButton(dividerEl, mp.div, sectionEl);
  }
}

// Add a chart group (one or more metrics sharing a Plotly chart) to a tab's charts container.
// Plotly.newPlot is deferred to initTabPlots(), called when the tab first becomes visible.
// chartLabel is the section-divider heading; metrics is an array of metric objects.
function addMetricPlotToContainer(container, metrics, tabId, chartLabel) {
  const divider = document.createElement('div');
  divider.className   = 'section-divider';
  divider.textContent = chartLabel;
  container.appendChild(divider);

  const div = document.createElement('div');
  div.className = 'metric-plot';
  container.appendChild(div);

  state.metricPlots.push({ div, metrics, tabId, initialized: false });
}

// Scan loaded series and register each one with its chart group using chart_map.
// Called after metricPlots are created but before the first render so that all
// historical series (including extra LoadParser/PidCpuParser series) appear correctly.
function initSeriesFromData(chartMap) {
  // Group discovered series labels by chart key
  const byChart = {};
  for (const key of Object.keys(state.series)) {
    if (!state.series[key].length) continue;
    const slash = key.indexOf('/');
    const label = slash >= 0 ? key.slice(slash + 1) : key;
    const chart = chartMap[label];
    if (!chart) continue;
    if (!byChart[chart]) byChart[chart] = [];
    if (!byChart[chart].includes(label)) byChart[chart].push(label);
  }

  for (const [chart, labels] of Object.entries(byChart)) {
    const mp = state.metricPlots.find(p => p.metrics.some(m => m.chart === chart));
    if (!mp) continue;
    const metaMeta = state.meta.metrics.find(m => m.chart === chart);

    // Remove placeholder entry: chart key used as series label but not a real series
    const placeholderIdx = mp.metrics.findIndex(
      m => m.label === chart && !Object.prototype.hasOwnProperty.call(chartMap, m.label)
    );
    if (placeholderIdx >= 0) mp.metrics.splice(placeholderIdx, 1);

    for (const label of labels) {
      if (!mp.metrics.some(m => m.label === label)) {
        mp.metrics.push({ label, chart, y_title: metaMeta?.y_title ?? '', unit: metaMeta?.unit ?? '' });
      }
    }
  }
}

function initTabCharts() {

  const tabBar = document.getElementById('tab-bar');
  let firstTabId = null;

  (state.meta.tabs || []).forEach(tab => {
    const tabMetrics = resolvedTabMetrics(tab);
    if (tabMetrics.length === 0) return;

    // Create tab button
    const btn = document.createElement('button');
    btn.className    = 'tab-btn';
    btn.dataset.tab  = tab.id;
    btn.textContent  = tab.label;
    btn.addEventListener('click', () => activateTab(tab.id));
    tabBar.appendChild(btn);

    // Create tab panel + charts container and insert into DOM
    const panel = document.createElement('div');
    panel.id        = `tab-${tab.id}`;
    panel.className = 'tab-panel';

    const container = document.createElement('div');
    container.id        = `charts-${tab.id}`;
    container.className = 'tab-charts';
    panel.appendChild(container);

    document.body.appendChild(panel);

    // Group metrics by chart key; metrics sharing the same chart go on one Plotly chart.
    const chartGroups = new Map();
    tabMetrics.forEach(metric => {
      const key = metric.chart;
      if (!chartGroups.has(key)) chartGroups.set(key, []);
      chartGroups.get(key).push(metric);
    });
    chartGroups.forEach((metrics, chartKey) =>
      addMetricPlotToContainer(container, metrics, tab.id, chartKey)
    );

    if (!firstTabId) firstTabId = tab.id;
  });

  document.getElementById('clear-events-btn').disabled = false;

  // Discover historical series from loaded data and register them with their chart groups.
  initSeriesFromData(state.chartMap);

  if (firstTabId) activateTab(firstTabId);
}

function refreshPlot() {
  for (const mp of state.metricPlots) {
    if (!mp.initialized) continue;
    const traces = buildMetricTraces(mp.metrics);
    Plotly.react(mp.div, traces, buildLayout(traces, { yaxisTitle: mp.metrics[0].y_title }));
  }
}

function appendMetricPoint(msg) {
  const { key, host, label, ts, value } = msg;
  const meta = msg.meta ?? null;
  if (!state.series[key]) state.series[key] = [];
  state.series[key].push({ ts, value, meta });

  if (state.paused) return;

  if (host === state.selectedHost) {
    const chart = msg.chart;

    // Check if this label already belongs to an existing chart group.
    let targetMp = state.metricPlots.find(mp => mp.metrics.some(m => m.label === label));

    if (!targetMp) {
      // Check if another metric in the same chart group already has a container
      // (e.g. Load (5m) arriving after Load (1m) chart was already created).
      targetMp = state.metricPlots.find(mp => mp.metrics[0].chart === chart);
      if (targetMp) {
        // Add this new series to the existing chart, replacing any placeholder first.
        const metric = { label, chart, y_title: msg.y_title ?? '', unit: msg.unit ?? '' };
        const placeholderIdx = targetMp.metrics.findIndex(
          m => m.label === chart && label !== chart
        );
        if (placeholderIdx >= 0) {
          targetMp.metrics[placeholderIdx] = metric;
        } else {
          targetMp.metrics.push(metric);
        }
        if (targetMp.initialized) {
          const traces = buildMetricTraces(targetMp.metrics);
          Plotly.react(targetMp.div, traces, buildLayout(traces, { yaxisTitle: targetMp.metrics[0].y_title }));
        }
      } else {
        // Completely new chart group — create a container in the appropriate tab.
        const tab = (state.meta.tabs || []).find(t => t.metrics.includes(label))
          || state.meta.tabs?.[0];
        if (tab) {
          const container = document.getElementById(`charts-${tab.id}`);
          if (container) {
            const metric = { label, chart, y_title: msg.y_title ?? '', unit: msg.unit ?? '' };
            addMetricPlotToContainer(container, [metric], tab.id, chart);
            if (state.activeTab === tab.id) initTabPlots(tab.id);
            targetMp = state.metricPlots[state.metricPlots.length - 1];
          }
        }
      }
    }

    // Extend the correct trace in the chart (trace index = position in metrics array).
    if (targetMp?.initialized) {
      const traceIdx = targetMp.metrics.findIndex(m => m.label === label);
      if (traceIdx >= 0) {
        const extend = { x: [[ts]], y: [[value]] };
        if (meta) extend.text = [[metaText(meta)]];
        Plotly.extendTraces(targetMp.div, extend, [traceIdx]);
      }
    }
  }
}

function addEventToPlot(ev) {
  state.events.push(ev);
  if (state.paused) return;
  // refreshPlot recomputes the full layout (shapes, annotations, topMargin, height).
  refreshPlot();
}

// ── Bootstrap ───────────────────────────────────────────────────────────────

async function init() {
  const [metaRes, dataRes] = await Promise.all([
    fetch('/api/meta'),
    fetch('/api/data'),
  ]);
  state.meta = await metaRes.json();
  if (!state.meta.live) document.body.classList.add('historical');
  const data = await dataRes.json();

  state.series   = data.series    || {};
  state.events   = data.events    || [];
  state.chartMap = data.chart_map || {};

  document.title = `Otto Monitor`;

  populateHostSelect();
  // Historical mode (no hosts): render charts immediately.
  // Multi-host mode: defer until a host is selected.
  if (state.meta.hosts.length === 0) {
    initTabCharts();
  }
  startSSE();
}

// ── SSE ─────────────────────────────────────────────────────────────────────

function startSSE() {
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  const src = new EventSource('/api/stream');

  src.onopen = () => {
    if (state.meta.live) {
      dot.className     = 'live';
      label.textContent = 'Live';
      state.isLive      = true;
      markEventBox.setEnabled(true);
      spanEventBox.setEnabled(true);
      document.getElementById('pause-btn').disabled = false;
    } else {
      dot.className     = 'history';
      label.textContent = 'Historical';
    }
  };

  src.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'metric') {
      appendMetricPoint(msg);
    } else if (msg.type === 'event') {
      addEventToPlot(msg);
    } else if (msg.type === 'event_deleted') {
      state.events = state.events.filter(ev => ev.id !== msg.id);
      if (state.editingEventId === msg.id) hidePopover();
      refreshPlot();
      refreshProcChart();
    } else if (msg.type === 'event_updated') {
      const idx = state.events.findIndex(ev => ev.id === msg.id);
      if (idx >= 0) state.events[idx] = msg;
      refreshPlot();
      refreshProcChart();
    }
  };

  src.onerror = () => {
    // Reset pause state — nothing left to pause
    state.paused = false;
    const pauseBtn = document.getElementById('pause-btn');
    pauseBtn.textContent = '⏸';
    pauseBtn.title    = 'Pause live updates';
    pauseBtn.disabled = true;

    // If a span was in progress, abandon it (the start event remains as a line)
    if (state.spanStartId !== null) {
      state.spanStartId = null;
      spanEventBox.setButtonText('Start event');
      spanEventBox.removeButtonClass('active');
    }
    spanEventBox.setEnabled(false);

    if (state.meta.live && state.isLive) {
      dot.className     = 'disconnected';
      label.textContent = 'Disconnected';
    } else {
      dot.className     = 'history';
      label.textContent = 'Historical';
    }
    src.close();
  };
}

// ── Event popover ───────────────────────────────────────────────────────────

function openPopover(ev, mouseEvent) {
  state.editingEventId = ev.id;
  document.getElementById('popover-label').value = ev.label;
  document.getElementById('popover-color').value  = ev.color;
  document.getElementById('popover-dash').value   = ev.dash;

  const pop = document.getElementById('event-popover');
  pop.classList.add('visible');

  // Position near the click, keeping within viewport
  const margin = 8;
  const pw = pop.offsetWidth  || 240;
  const ph = pop.offsetHeight || 140;
  let x = mouseEvent.clientX + margin;
  let y = mouseEvent.clientY + margin;
  if (x + pw > window.innerWidth)  x = mouseEvent.clientX - pw - margin;
  if (y + ph > window.innerHeight) y = mouseEvent.clientY - ph - margin;
  pop.style.left = `${Math.max(0, x)}px`;
  pop.style.top  = `${Math.max(0, y)}px`;

  document.getElementById('popover-label').focus();
}

function hidePopover() {
  state.editingEventId = null;
  document.getElementById('event-popover').classList.remove('visible');
}

document.getElementById('popover-save').addEventListener('click', async () => {
  const id = state.editingEventId;
  if (id === null) return;
  const label = document.getElementById('popover-label').value.trim();
  const color = document.getElementById('popover-color').value;
  const dash  = document.getElementById('popover-dash').value;
  hidePopover();
  await fetch(`/api/event/${id}`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ label, color, dash }),
  });
});

document.getElementById('popover-delete').addEventListener('click', async () => {
  const id = state.editingEventId;
  if (id === null) return;
  hidePopover();
  await fetch(`/api/event/${id}`, { method: 'DELETE' });
});

document.getElementById('popover-cancel').addEventListener('click', hidePopover);

// Close popover when clicking outside it
document.addEventListener('click', (e) => {
  const pop = document.getElementById('event-popover');
  if (pop.classList.contains('visible') && !pop.contains(e.target)) {
    hidePopover();
  }
});

// ── Event bar ───────────────────────────────────────────────────────────────

class EventBox {
  constructor(inputId, colorId, dashId, btnId) {
    this.input = document.getElementById(inputId);
    this.color = document.getElementById(colorId);
    this.dash  = document.getElementById(dashId);
    this.btn   = document.getElementById(btnId);
  }
  get values() {
    return { label: this.input.value.trim(), color: this.color.value, dash: this.dash.value };
  }
  clearInput()           { this.input.value = ''; }
  setEnabled(on)         { this.btn.disabled = !on; }
  setButtonText(text)    { this.btn.textContent = text; }
  addButtonClass(cls)    { this.btn.classList.add(cls); }
  removeButtonClass(cls) { this.btn.classList.remove(cls); }
  onAction(callback) {
    this.btn.addEventListener('click', () => callback(this.values));
    this.input.addEventListener('keydown', e => { if (e.key === 'Enter') this.btn.click(); });
  }
}

const markEventBox = new EventBox('event-label', 'event-color', 'event-dash', 'event-btn');
const spanEventBox = new EventBox('span-label',  'span-color',  'span-dash',  'span-btn');

markEventBox.onAction(async ({ label, color, dash }) => {
  if (!label) return;
  markEventBox.setEnabled(false);
  try {
    await fetch('/api/event', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ label, color, dash }),
    });
    markEventBox.clearInput();
  } finally {
    markEventBox.setEnabled(true);
  }
});

document.getElementById('clear-events-btn').addEventListener('click', async () => {
  if (!confirm('Are you sure you want to clear all event markers?\nThis action cannot be undone.')) return;
  // Snapshot ids before SSE callbacks mutate state.events
  const ids = state.events.map(ev => ev.id);
  await Promise.all(ids.map(id => fetch(`/api/event/${id}`, { method: 'DELETE' })));
});

spanEventBox.onAction(async ({ label, color, dash }) => {
  if (state.spanStartId === null) {
    // ── Start the span: POST a normal event, then switch to "End event" mode ──
    if (!label) return;
    spanEventBox.setEnabled(false);
    try {
      const res = await fetch('/api/event', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ label, color, dash }),
      });
      if (!res.ok) return;
      const ev = await res.json();
      state.spanStartId = ev.id;
      spanEventBox.setButtonText('End event');
      spanEventBox.addButtonClass('active');
      // The event already arrived via SSE and shows as a vertical line
    } finally {
      spanEventBox.setEnabled(true);
    }
  } else {
    // ── End the span: POST to /end — server records datetime.now() ──────────
    const id = state.spanStartId;
    state.spanStartId = null;
    spanEventBox.setEnabled(false);
    try {
      await fetch(`/api/event/${id}/end`, { method: 'POST' });
      // SSE event_updated will update state.events and refresh plots
      spanEventBox.clearInput();
    } finally {
      spanEventBox.setButtonText('Start event');
      spanEventBox.removeButtonClass('active');
      spanEventBox.setEnabled(true);
    }
  }
});

// ── Pause toggle ─────────────────────────────────────────────────────────────

document.getElementById('pause-btn').addEventListener('click', () => {
  state.paused = !state.paused;
  const btn   = document.getElementById('pause-btn');
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  if (state.paused) {
    btn.textContent   = '▶';
    btn.title         = 'Resume live updates';
    label.textContent = 'Paused';
    dot.classList.add('paused');
  } else {
    btn.textContent   = '⏸';
    btn.title         = 'Pause live updates';
    label.textContent = 'Live';
    dot.classList.remove('paused');
    refreshPlot();
  }
});

// ── Theme toggle ─────────────────────────────────────────────────────────────

function applyTheme(light) {
  document.body.classList.toggle('light', light);
  document.getElementById('theme-btn').title       = light ? 'Switch to dark mode' : 'Switch to light mode';
  document.getElementById('theme-btn').textContent = light ? '🌙' : '☀️';
}

document.getElementById('theme-btn').addEventListener('click', () => {
  const light = !document.body.classList.contains('light');
  applyTheme(light);
  localStorage.setItem('otto-theme', light ? 'light' : 'dark');
  refreshPlot();
});

// Restore saved preference (dark is default, so only act if 'light' was saved)
if (localStorage.getItem('otto-theme') === 'light') applyTheme(true);

// ── Go ───────────────────────────────────────────────────────────────────────
init().catch(err => {
  document.getElementById('tab-bar').textContent = `Error loading dashboard: ${err}`;
});
