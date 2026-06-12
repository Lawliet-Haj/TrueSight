// ParcVue — fiche poste : inventaire, graphiques CPU/RAM (24 h), console de commande.
// Rendu adapté au thème sombre « poste de pilotage ». La logique remote vit dans remote.js.
(function () {
  "use strict";

  var pageData = document.getElementById("page-data");
  if (!pageData) return;
  var AGENT_ID = pageData.getAttribute("data-agent-id");
  var IS_ADMIN = pageData.getAttribute("data-is-admin") === "1";
  var DETAIL_REFRESH_MS = 10000;

  var cpuChart = null;
  var ramChart = null;

  // --- Palette / options Chart.js sombres ---
  var C = {
    signal: "#2BE3C6",
    info: "#5AB6FF",
    grid: "rgba(255,255,255,.06)",
    tick: "#5A6773",
    text: "#93A1B0",
    panel: "#0C1116",
    line2: "rgba(255,255,255,.11)",
  };

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    return d.toLocaleString("fr-FR");
  }

  function fmtUptime(secs) {
    if (secs === null || secs === undefined) return "—";
    var s = Number(secs);
    var days = Math.floor(s / 86400);
    var hours = Math.floor((s % 86400) / 3600);
    var mins = Math.floor((s % 3600) / 60);
    var out = [];
    if (days) out.push(days + " j");
    if (hours) out.push(hours + " h");
    out.push(mins + " min");
    return out.join(" ");
  }

  // --- Détail (agent + matériel + dernier metric) ---
  async function loadDetail() {
    try {
      var resp = await fetch("/api/v1/agents/" + AGENT_ID, { headers: { Accept: "application/json" } });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var d = await resp.json();
      renderHeader(d);
      renderHardware(d);
      renderCurrent(d);
    } catch (e) {
      console.error("Échec du chargement du détail :", e);
    }
  }

  function renderHeader(d) {
    document.getElementById("title-hostname").textContent = d.hostname || d.id;
    var badge = document.getElementById("status-badge");
    if (d.status === "online") {
      badge.textContent = "En ligne";
      badge.className = "badge ok";
    } else {
      badge.textContent = "Hors ligne";
      badge.className = "badge off";
    }
    document.getElementById("last-seen").textContent = "Dernière activité : " + fmtDate(d.last_seen_at);
  }

  function kv(label, value, mono) {
    return (
      '<div class="kv"><dt>' + esc(label) + "</dt>" +
      '<dd' + (mono ? ' class="mono"' : "") + ">" + esc(value) + "</dd></div>"
    );
  }

  function renderHardware(d) {
    var el = document.getElementById("hardware");
    var hw = d.hardware;
    var html = "";
    html += kv("Système", d.os_version || "—");
    html += kv("Version agent", d.agent_version || "—");
    if (hw) {
      html += kv("Fabricant", hw.manufacturer || "—");
      html += kv("Modèle", hw.model || "—");
      html += kv("N° de série", hw.serial_number || "—", true);
      html += kv("Processeur", hw.cpu_model || "—");
      html += kv("Cœurs", hw.cpu_cores != null ? hw.cpu_cores : "—");
      html += kv("RAM totale", hw.ram_total_mb != null ? (hw.ram_total_mb + " Mo") : "—");
      if (hw.disks && hw.disks.length) {
        var disks = hw.disks.map(function (dk) {
          return esc(dk.drive) + " " + (dk.free_gb != null ? dk.free_gb : "?") + " / " +
            (dk.total_gb != null ? dk.total_gb : "?") + " Go";
        }).join("<br>");
        html += '<div class="kv"><dt>Disques</dt><dd class="mono">' + disks + "</dd></div>";
      }
      if (hw.mac_addresses && hw.mac_addresses.length) {
        html += '<div class="kv"><dt>MAC</dt><dd class="mono">' +
          hw.mac_addresses.map(esc).join("<br>") + "</dd></div>";
      }
    } else {
      html += '<div class="dl-empty">Inventaire matériel non encore collecté.</div>';
    }
    html += kv("Logiciels", d.software_count != null ? d.software_count : "—");
    el.innerHTML = html;
  }

  function renderCurrent(d) {
    var el = document.getElementById("current-metrics");
    var m = d.last_metric;
    if (!m) { el.textContent = ""; return; }
    var parts = [];
    if (m.cpu_pct != null) parts.push("CPU " + Number(m.cpu_pct).toFixed(0) + "%");
    if (m.ram_used_pct != null) parts.push("RAM " + Number(m.ram_used_pct).toFixed(0) + "%");
    if (m.logged_in_user) parts.push(m.logged_in_user);
    if (m.uptime_seconds != null) parts.push("↑ " + fmtUptime(m.uptime_seconds));
    el.textContent = parts.join("  ·  ");
  }

  // --- Graphiques (métriques 24 h) ---
  async function loadMetrics() {
    try {
      var resp = await fetch("/api/v1/agents/" + AGENT_ID + "/metrics?hours=24", { headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var rows = await resp.json();
      var labels = rows.map(function (r) {
        var dt = new Date(r.ts);
        return isNaN(dt.getTime()) ? "" : dt.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
      });
      var cpu = rows.map(function (r) { return r.cpu_pct; });
      var ram = rows.map(function (r) { return r.ram_used_pct; });
      cpuChart = upsertChart(cpuChart, "chart-cpu", labels, cpu, C.signal);
      ramChart = upsertChart(ramChart, "chart-ram", labels, ram, C.info);
    } catch (e) {
      console.error("Échec du chargement des métriques :", e);
    }
  }

  // Convertit une couleur hex en rgba avec alpha (pour le remplissage).
  function rgba(hex, a) {
    var h = hex.replace("#", "");
    var r = parseInt(h.substring(0, 2), 16);
    var g = parseInt(h.substring(2, 4), 16);
    var b = parseInt(h.substring(4, 6), 16);
    return "rgba(" + r + "," + g + "," + b + "," + a + ")";
  }

  function upsertChart(chart, canvasId, labels, data, color) {
    var ctx = document.getElementById(canvasId);
    if (!ctx) return chart;
    if (chart) {
      chart.data.labels = labels;
      chart.data.datasets[0].data = data;
      chart.update("none");
      return chart;
    }
    // Dégradé vertical pour le remplissage.
    var ctx2d = ctx.getContext("2d");
    var grad = ctx2d.createLinearGradient(0, 0, 0, 120);
    grad.addColorStop(0, rgba(color, 0.28));
    grad.addColorStop(1, rgba(color, 0.0));

    return new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [{
          data: data,
          borderColor: color,
          backgroundColor: grad,
          borderWidth: 1.8,
          pointRadius: 0,
          pointHoverRadius: 3,
          pointHoverBackgroundColor: color,
          tension: 0.3,
          fill: true,
          spanGaps: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: C.panel,
            borderColor: C.line2,
            borderWidth: 1,
            titleColor: C.text,
            bodyColor: "#E8EEF4",
            titleFont: { family: "'JetBrains Mono', monospace", size: 10 },
            bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
            padding: 9,
            displayColors: false,
            callbacks: { label: function (c) { return c.parsed.y != null ? c.parsed.y.toFixed(0) + " %" : ""; } },
          },
        },
        scales: {
          y: {
            min: 0, max: 100,
            grid: { color: C.grid, drawTicks: false },
            border: { display: false },
            ticks: { stepSize: 25, color: C.tick, font: { family: "'JetBrains Mono', monospace", size: 9 }, padding: 6 },
          },
          x: {
            grid: { display: false },
            border: { color: C.grid },
            ticks: { maxTicksLimit: 8, autoSkip: true, color: C.tick, font: { family: "'JetBrains Mono', monospace", size: 9 }, maxRotation: 0 },
          },
        },
      },
    });
  }

  // --- Logiciels ---
  var softwareCache = [];
  async function loadSoftware() {
    try {
      var resp = await fetch("/api/v1/agents/" + AGENT_ID + "/software", { headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      softwareCache = await resp.json();
      renderSoftware();
    } catch (e) {
      document.getElementById("software-body").innerHTML =
        '<tr><td colspan="4" class="empty-cell err-cell">Erreur de chargement</td></tr>';
    }
  }

  function renderSoftware() {
    var body = document.getElementById("software-body");
    var filterEl = document.getElementById("sw-filter");
    var filter = (filterEl ? filterEl.value : "").toLowerCase();
    var rows = softwareCache.filter(function (s) {
      if (!filter) return true;
      return [s.name, s.publisher, s.version].join(" ").toLowerCase().indexOf(filter) !== -1;
    });
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="4" class="empty-cell">Aucun logiciel</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (s) {
      return "<tr>" +
        '<td>' + esc(s.name || "—") + "</td>" +
        '<td class="mono text-dim">' + esc(s.version || "—") + "</td>" +
        '<td class="text-dim">' + esc(s.publisher || "—") + "</td>" +
        '<td class="mono text-faint">' + esc(s.install_date || "—") + "</td>" +
        "</tr>";
    }).join("");
  }

  var swFilter = document.getElementById("sw-filter");
  if (swFilter) swFilter.addEventListener("input", renderSoftware);

  // --- Console de commande (admin uniquement) ---
  function setupConsole() {
    var runBtn = document.getElementById("cmd-run");
    if (!runBtn) return;
    var statusEl = document.getElementById("cmd-status");
    var outputEl = document.getElementById("cmd-output");
    var pollTimer = null;

    runBtn.addEventListener("click", async function () {
      var shell = document.getElementById("cmd-shell").value;
      var text = document.getElementById("cmd-text").value.trim();
      var timeout = parseInt(document.getElementById("cmd-timeout").value, 10) || 120;
      if (!text) { statusEl.textContent = "Veuillez saisir une commande."; return; }

      if (!window.confirm("Exécuter cette commande sur le poste ?\n\n" + text)) return;

      runBtn.disabled = true;
      statusEl.textContent = "Envoi de la commande…";
      outputEl.classList.add("hidden");
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

      try {
        var resp = await fetch("/api/v1/agents/" + AGENT_ID + "/commands", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ shell: shell, command_text: text, timeout_seconds: timeout }),
        });
        var data = await resp.json();
        if (!resp.ok) {
          statusEl.textContent = "Erreur : " + (data.error || resp.status);
          runBtn.disabled = false;
          return;
        }
        statusEl.textContent = "Commande mise en file (en attente d'exécution)…";
        pollResult(data.command_id);
      } catch (e) {
        statusEl.textContent = "Erreur réseau lors de l'envoi.";
        runBtn.disabled = false;
      }
    });

    function pollResult(commandId) {
      var attempts = 0;
      pollTimer = setInterval(async function () {
        attempts++;
        try {
          var resp = await fetch("/api/v1/commands/" + commandId, { headers: { Accept: "application/json" } });
          if (!resp.ok) throw new Error("HTTP " + resp.status);
          var data = await resp.json();
          statusEl.textContent = "Statut : " + statusLabel(data.status);
          if (["done", "error", "timeout"].indexOf(data.status) !== -1) {
            clearInterval(pollTimer); pollTimer = null;
            runBtn.disabled = false;
            showResult(data);
          }
        } catch (e) {
          // On continue le polling malgré une erreur ponctuelle.
        }
        // Arrêt de sécurité après ~5 minutes de polling.
        if (attempts > 150) { clearInterval(pollTimer); pollTimer = null; runBtn.disabled = false; }
      }, 2000);
    }

    function statusLabel(s) {
      return {
        pending: "en attente",
        dispatched: "transmise à l'agent",
        running: "en cours",
        done: "terminée",
        error: "erreur",
        timeout: "délai dépassé",
      }[s] || s;
    }

    function showResult(data) {
      var r = data.result || {};
      var lines = [];
      lines.push("Code de sortie : " + (r.exit_code != null ? r.exit_code : "—"));
      if (r.duration_seconds != null) lines.push("Durée : " + r.duration_seconds + " s");
      lines.push("");
      if (r.stdout) { lines.push("--- STDOUT ---"); lines.push(r.stdout); }
      if (r.stderr) { lines.push(""); lines.push("--- STDERR ---"); lines.push(r.stderr); }
      outputEl.textContent = lines.join("\n");
      outputEl.classList.remove("hidden");
    }
  }

  // Si la page est ouverte avec #console, on défile vers la console.
  function scrollToAnchor() {
    var hash = window.location.hash;
    if (hash === "#console" || hash === "#remote") {
      var el = document.querySelector(hash);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  // --- Initialisation ---
  loadDetail();
  loadMetrics();
  loadSoftware();
  if (IS_ADMIN) setupConsole();
  scrollToAnchor();

  setInterval(loadDetail, DETAIL_REFRESH_MS);
  setInterval(loadMetrics, 60000);
})();
