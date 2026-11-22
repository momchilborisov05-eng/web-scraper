/* Trawl dashboard: reads the JSON API and draws runs, throughput, status codes, latency and item changes. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const charts = {};
  let runs = [];
  let selectedRun = null;
  let selectedItem = null;

  const fmt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString("en-US"));
  const money = (v) => (typeof v === "number" ? "£" + v.toFixed(2) : v === null || v === undefined ? "-" : String(v));

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function palette() {
    return {
      accent: css("--accent") || "#f59e0b",
      accent2: css("--accent-2") || "#f97316",
      ink: css("--accent-ink") || "#b45309",
      text: css("--text") || "#111",
      muted: css("--text-3") || "#666",
      line: css("--border") || "rgba(127,127,127,.2)",
      good: css("--success") || "#16a34a",
      bad: css("--danger") || "#dc2626",
      info: css("--info") || "#0ea5e9",
      violet: "#8b5cf6",
      grey: "#94a3b8",
    };
  }

  async function api(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(path + " -> HTTP " + res.status);
    return res.json();
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else node.setAttribute(k, v);
      }
    }
    (children || []).forEach((c) => node.append(c instanceof Node ? c : document.createTextNode(String(c))));
    return node;
  }

  function chart(id, config) {
    if (typeof Chart === "undefined") return null;
    if (charts[id]) charts[id].destroy();
    const p = palette();
    Chart.defaults.color = p.muted;
    Chart.defaults.borderColor = p.line;
    Chart.defaults.font.family = "\"IBM Plex Sans\", system-ui, sans-serif";
    config.options = Object.assign({ responsive: true, maintainAspectRatio: false, animation: { duration: 500 } },
      config.options || {});
    charts[id] = new Chart($(id), config);
    return charts[id];
  }

  /* ---------- KPIs ---------- */
  function setKpi(id, value, sub) {
    $(id).textContent = value;
    if (sub !== undefined) $(id + "Sub").textContent = sub;
  }

  function renderKpis(summary, run) {
    const s = (run && run.stats) || {};
    const codes = s.status_counts || {};
    const items = s.items || {};
    const fetched = Object.values(codes).reduce((a, b) => a + b, 0);
    const notModified = codes["304"] || 0;
    setKpi("kPages", fmt(s.pages), (s.retries || 0) + " retries, " + (s.failed || 0) + " failed");
    setKpi("kRate", s.pages_per_sec !== undefined ? s.pages_per_sec + "/s" : "-", fmt(s.seconds) + " s wall time");
    setKpi("kSaved", fetched ? Math.round((notModified / fetched) * 100) + "%" : "-",
      fmt(notModified) + " answered 304 Not Modified");
    setKpi("kItems", fmt(summary.items), fmt(summary.runs) + " runs, " + fmt(summary.changes) + " changes total");
    setKpi("kChanged", fmt(items.changed || 0), "new " + fmt(items.new || 0) + ", removed " + fmt(items.removed || 0));
    const lat = s.latency_ms || {};
    setKpi("kLat", lat.p50 !== undefined ? Math.round(lat.p50) + " / " + Math.round(lat.p95) : "-",
      lat.p50 !== undefined ? "ms, exact " + lat.p50 + " / " + lat.p95 : "milliseconds per fetch");
    $("runLabel").textContent = run ? "Showing run #" + run.id + " (" + run.status + ")." : "";
    if (typeof gsap !== "undefined") {
      gsap.from(".kpi .value", { opacity: 0, y: 6, duration: 0.35, stagger: 0.04, overwrite: true });
    }
  }

  /* ---------- Charts ---------- */
  function renderRunsChart() {
    const p = palette();
    const ordered = runs.slice().reverse();
    chart("runsChart", {
      data: {
        labels: ordered.map((r) => "#" + r.id),
        datasets: [
          { type: "bar", label: "pages/s", data: ordered.map((r) => r.stats.pages_per_sec || 0),
            backgroundColor: p.accent, borderRadius: 6, yAxisID: "y" },
          { type: "line", label: "items changed", data: ordered.map((r) => (r.stats.items || {}).changed || 0),
            borderColor: p.violet, backgroundColor: p.violet, tension: 0.3, yAxisID: "y1" },
        ],
      },
      options: {
        plugins: { legend: { position: "bottom" } },
        scales: {
          y: { beginAtZero: true, title: { display: true, text: "pages/s" } },
          y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false },
            title: { display: true, text: "changed items" } },
        },
      },
    });
  }

  function renderMix(run) {
    const p = palette();
    const codes = (run && run.stats.status_counts) || {};
    const keys = Object.keys(codes).sort();
    const color = (k) => (k === "200" ? p.good : k === "304" ? p.info : k.startsWith("3") ? p.violet
      : k.startsWith("4") ? p.accent2 : k.startsWith("5") ? p.bad : p.grey);
    $("mixBadge").textContent = keys.length + " status codes";
    chart("mixChart", {
      type: "doughnut",
      data: {
        labels: keys.map((k) => (k === "0" ? "network error" : "HTTP " + k)),
        datasets: [{ data: keys.map((k) => codes[k]), backgroundColor: keys.map(color), borderWidth: 0 }],
      },
      options: { cutout: "62%", plugins: { legend: { position: "right" } } },
    });
  }

  function renderThroughput(detail) {
    const p = palette();
    const series = detail.throughput || [];
    chart("throughputChart", {
      type: "line",
      data: {
        labels: series.map((_, i) => i + "s"),
        datasets: [{ label: "pages", data: series, borderColor: p.accent, fill: true, tension: 0.25, pointRadius: 0,
          backgroundColor: p.accent + "33" }],
      },
      options: { plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, title: { display: true, text: "pages in that second" } } } },
    });
  }

  function renderLatency(detail) {
    const p = palette();
    const h = detail.latency_histogram || { bounds_ms: [], counts: [] };
    const labels = h.bounds_ms.map((b, i) => (i === 0 ? "≤" + b : (h.bounds_ms[i - 1]) + "-" + b) + " ms");
    labels.push(">" + h.bounds_ms[h.bounds_ms.length - 1] + " ms");
    chart("latencyChart", {
      type: "bar",
      data: { labels, datasets: [{ label: "fetches", data: h.counts, backgroundColor: p.accent2, borderRadius: 5 }] },
      options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
    });
  }

  /* ---------- Tables ---------- */
  function renderRunsTable() {
    const body = $("runsBody");
    body.replaceChildren();
    runs.forEach((r) => {
      const s = r.stats || {};
      const it = s.items || {};
      const badge = el("span", { class: "badge " + (r.status === "completed" ? "info" : r.status === "running" ? "neutral" : "warn") },
        [r.status]);
      const tr = el("tr", { class: "clickable" + (selectedRun === r.id ? " sel" : ""), tabindex: "0" }, [
        el("td", { class: "mono" }, ["#" + r.id]),
        el("td", null, [badge]),
        el("td", { class: "n" }, [fmt(s.pages)]),
        el("td", { class: "n" }, [fmt(s.seconds)]),
        el("td", { class: "n" }, [fmt(s.pages_per_sec)]),
        el("td", { class: "n" }, [fmt((s.status_counts || {})["304"] || 0)]),
        el("td", { class: "n" }, [fmt(s.retries || 0)]),
        el("td", { class: "n" }, [fmt(it.new || 0)]),
        el("td", { class: "n" }, [fmt(it.changed || 0)]),
        el("td", { class: "n" }, [fmt(it.removed || 0)]),
      ]);
      const pick = () => selectRun(r.id);
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
      body.append(tr);
    });
  }

  function changeSummary(changes) {
    const keys = Object.keys(changes);
    if (keys.length === 1 && keys[0] === "price") return "price";
    return keys.join(", ");
  }

  async function renderChanges(runId) {
    const data = await api("/api/changes?limit=200" + (runId ? "&run=" + runId : ""));
    const body = $("changesBody");
    body.replaceChildren();
    $("changesBadge").textContent = data.changes.length + (runId ? " in run #" + runId : " total");
    if (!data.changes.length) {
      body.append(el("tr", null, [el("td", { colspan: "4", class: "muted" }, ["No changed items in this run."])]));
      return data.changes;
    }
    data.changes.forEach((c) => {
      const price = c.changes.price;
      const priceCell = el("td", { class: "n chg" });
      if (price) {
        priceCell.append(el("s", null, [money(price[0])]), " " + money(price[1]) + " ");
        if (c.price_change_pct !== null) {
          priceCell.append(el("span", { class: c.price_change_pct < 0 ? "delta-down" : "delta-up" },
            [(c.price_change_pct > 0 ? "+" : "") + c.price_change_pct + "%"]));
        }
      } else {
        priceCell.append(el("span", { class: "muted" }, ["-"]));
      }
      const tr = el("tr", { class: "clickable" + (selectedItem === c.key ? " sel" : ""), tabindex: "0" }, [
        el("td", { class: "title-cell", title: c.title }, [c.title]),
        el("td", { class: "muted" }, [changeSummary(c.changes)]),
        priceCell,
        el("td", { class: "n mono" }, ["#" + c.run_id]),
      ]);
      const pick = () => selectItem(c.spec, c.key, tr);
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
      body.append(tr);
    });
    return data.changes;
  }

  async function selectItem(spec, key, row) {
    selectedItem = key;
    document.querySelectorAll("#changesBody tr").forEach((tr) => tr.classList.remove("sel"));
    if (row) row.classList.add("sel");
    const h = await api("/api/items/" + encodeURIComponent(spec) + "/" + encodeURIComponent(key) + "/history");
    const last = h.versions[h.versions.length - 1].data;
    const meta = $("itemMeta");
    meta.replaceChildren(el("b", null, [last.title || key]),
      el("span", null, [last.category || spec]), el("span", null, ["UPC " + key]),
      el("span", null, [h.versions.length + " versions"]));
    $("histBadge").textContent = "now " + money(last.price);
    const p = palette();
    chart("historyChart", {
      type: "line",
      data: {
        labels: h.versions.map((v) => "run #" + v.run_id),
        datasets: [{ label: "price", data: h.versions.map((v) => v.data.price), borderColor: p.accent, stepped: true,
          backgroundColor: p.accent, pointRadius: 4 }],
      },
      options: { plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => money(ctx.parsed.y) } } },
        scales: { y: { ticks: { callback: (v) => money(v) } } } },
    });
  }

  async function selectRun(runId) {
    selectedRun = runId;
    $("runSelect").value = String(runId);
    renderRunsTable();
    const [detail, summary] = await Promise.all([api("/api/runs/" + runId), api("/api/summary")]);
    renderKpis(summary, detail.run);
    renderMix(detail.run);
    renderThroughput(detail);
    renderLatency(detail);
    const changes = await renderChanges(runId);
    if (changes.length) {
      const withPrice = changes.find((c) => c.changes.price) || changes[0];
      await selectItem(withPrice.spec, withPrice.key, $("changesBody").querySelector("tr"));
    }
  }

  async function load() {
    try {
      const [health, runList] = await Promise.all([api("/health"), api("/api/runs?limit=100")]);
      runs = runList.runs;
      $("dbText").textContent = health.runs + " runs · v" + health.version;
      $("dbBadge").className = "badge info";
      const select = $("runSelect");
      select.replaceChildren(...runs.map((r) => el("option", { value: String(r.id) }, ["#" + r.id + " · " + r.status])));
      renderRunsChart();
      if (!runs.length) {
        renderKpis(await api("/api/summary"), null);
        $("changesBody").replaceChildren(el("tr", null, [el("td", { colspan: "4", class: "muted" },
          ["No runs yet. Try: python -m trawl demo"])]));
        return;
      }
      const target = runs.find((r) => r.id === selectedRun) ? selectedRun
        : (runs.find((r) => (r.stats.items || {}).changed) || runs[0]).id;
      await selectRun(target);
    } catch (err) {
      $("dbText").textContent = "API unavailable";
      $("dbBadge").className = "badge warn";
      console.error(err);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("runSelect").addEventListener("change", (e) => selectRun(Number(e.target.value)));
    $("refresh").addEventListener("click", load);
    document.addEventListener("themechange", () => { renderRunsChart(); if (selectedRun) selectRun(selectedRun); });
    load();
  });
})();
