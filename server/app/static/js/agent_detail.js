// TrueSight — fiche poste : inventaire, graphiques CPU/RAM (24 h), console de commande.
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
      renderTags(d.tags);
    } catch (e) {
      console.error("Échec du chargement du détail :", e);
    }
  }

  var HEALTH_BADGE = {
    healthy: ["Sain", "ok"], warning: ["Attention", "warn"],
    critical: ["Défectueux", "danger"], unknown: ["Hors ligne", "off"],
  };

  function renderHeader(d) {
    document.getElementById("title-hostname").textContent = d.name || d.hostname || d.id;
    // Sous-titre : nom d'hôte (si renommé) + emplacement.
    var label = document.querySelector(".page-head .label");
    if (label) {
      var bits = ["Fiche poste"];
      if (d.display_name && d.hostname && d.display_name !== d.hostname) bits.push(d.hostname);
      if (d.site_name) bits.push(d.site_name);
      label.textContent = bits.join("  ·  ");
    }
    var badge = document.getElementById("status-badge");
    if (d.status !== "online") {
      badge.textContent = "Hors ligne";
      badge.className = "badge off";
    } else {
      var h = HEALTH_BADGE[d.health] || HEALTH_BADGE.healthy;
      badge.textContent = h[0];
      badge.className = "badge " + h[1];
      if (d.health_reasons && d.health_reasons.length) badge.title = d.health_reasons.join(", ");
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
    html += kv("Emplacement", d.site_name || "Non assigné");
    var hlabel = (HEALTH_BADGE[d.health] || ["—"])[0];
    if (d.health_reasons && d.health_reasons.length && d.health !== "healthy") {
      hlabel += " · " + d.health_reasons.join(", ");
    }
    html += kv("Santé", hlabel);
    var s = d.security;
    if (s) {
      var av = s.defender_enabled === true ? "Defender actif"
        : (s.defender_enabled === false ? "Defender désactivé" : "—");
      if (s.defender_enabled === true && s.defender_realtime === false) av += " (temps réel off)";
      html += kv("Antivirus", av);
      if (s.pending_updates != null) {
        html += kv("MAJ Windows en attente",
          s.pending_updates + (s.pending_critical ? " (dont " + s.pending_critical + " critiques)" : ""));
      }
    }
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
        '<tr><td colspan="' + (IS_ADMIN ? 5 : 4) + '" class="empty-cell err-cell">Erreur de chargement</td></tr>';
    }
  }

  function renderSoftware() {
    var body = document.getElementById("software-body");
    var filterEl = document.getElementById("sw-filter");
    var filter = (filterEl ? filterEl.value : "").toLowerCase();
    var span = IS_ADMIN ? 5 : 4;
    var rows = softwareCache.filter(function (s) {
      if (!filter) return true;
      return [s.name, s.publisher, s.version].join(" ").toLowerCase().indexOf(filter) !== -1;
    });
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="' + span + '" class="empty-cell">Aucun logiciel</td></tr>';
      return;
    }
    body.innerHTML = rows.map(function (s) {
      var action = "";
      if (IS_ADMIN) {
        action = '<td class="num"><button type="button" class="btn xs danger sw-uninstall" ' +
          'data-name="' + esc(s.name || "") + '"' + (s.name ? "" : " disabled") +
          '><svg><use href="#i-x"/></svg>Désinstaller</button></td>';
      }
      return "<tr>" +
        '<td>' + esc(s.name || "—") + "</td>" +
        '<td class="mono text-dim">' + esc(s.version || "—") + "</td>" +
        '<td class="text-dim">' + esc(s.publisher || "—") + "</td>" +
        '<td class="mono text-faint">' + esc(s.install_date || "—") + "</td>" +
        action +
        "</tr>";
    }).join("");
    if (IS_ADMIN) bindUninstallButtons();
  }

  var swFilter = document.getElementById("sw-filter");
  if (swFilter) swFilter.addEventListener("input", renderSoftware);

  // --- Déploiement logiciel (install / uninstall silencieux — admin) ---
  // Réutilise pollCommand / renderCmdResult (définis plus bas, hoistés).
  function runSoftwareCommand(kind, payload, label) {
    var statusEl = document.getElementById("sw-status");
    var outputEl = document.getElementById("sw-output");
    if (statusEl) statusEl.textContent = label + " — envoi…";
    if (outputEl) outputEl.classList.add("hidden");
    fetch("/api/v1/agents/" + AGENT_ID + "/software/" + kind, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, data: data }; });
    }).then(function (res) {
      if (!res.ok) {
        if (statusEl) statusEl.textContent = "Erreur : " + (res.data.error || "envoi impossible");
        return;
      }
      if (statusEl) statusEl.textContent = label + " — en file, exécution sur le poste…";
      pollCommand(res.data.command_id, statusEl, outputEl);
    }).catch(function () {
      if (statusEl) statusEl.textContent = "Erreur réseau lors de l'envoi.";
    });
  }

  function bindUninstallButtons() {
    Array.prototype.forEach.call(document.querySelectorAll(".sw-uninstall"), function (b) {
      b.addEventListener("click", function () {
        var name = b.getAttribute("data-name");
        if (!name) return;
        TS.confirm({
          title: "Désinstaller « " + name + " » ?",
          body: "Désinstallation silencieuse sur ce poste (QuietUninstallString / MSI). Indisponible pour certains EXE.",
          danger: true, confirmLabel: "Désinstaller",
        }).then(function (r) {
          if (!r.confirmed) return;
          runSoftwareCommand("uninstall", { source: "registry", name: name }, "Désinstallation de " + name);
        });
      });
    });
  }

  function setupSoftwareDeploy() {
    var toggle = document.getElementById("sw-install-toggle");
    var box = document.getElementById("sw-install");
    if (!toggle || !box) return;

    var srcSel = document.getElementById("sw-src");
    var catSel = document.getElementById("sw-catalog");
    var widInput = document.getElementById("sw-winget-id");
    var urlInput = document.getElementById("sw-url");
    var argsInput = document.getElementById("sw-args");
    var runBtn = document.getElementById("sw-install-run");
    var catalogLoaded = false;

    function loadCatalog() {
      if (catalogLoaded) return;
      catalogLoaded = true;
      fetch("/api/v1/software/catalog", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (items) {
          if (!Array.isArray(items) || !items.length) {
            catSel.innerHTML = '<option value="">Catalogue indisponible</option>';
            return;
          }
          var groups = {};
          items.forEach(function (it) { (groups[it.category] = groups[it.category] || []).push(it); });
          var html = "";
          Object.keys(groups).forEach(function (cat) {
            html += '<optgroup label="' + esc(cat) + '">';
            groups[cat].forEach(function (it) {
              html += '<option value="' + esc(it.key) + '">' + esc(it.label) + "</option>";
            });
            html += "</optgroup>";
          });
          catSel.innerHTML = html;
        })
        .catch(function () { catalogLoaded = false; catSel.innerHTML = '<option value="">Erreur catalogue</option>'; });
    }

    function syncFields() {
      var src = srcSel.value;
      catSel.classList.toggle("hidden", src !== "catalog");
      widInput.classList.toggle("hidden", src !== "winget");
      urlInput.classList.toggle("hidden", src !== "url");
      argsInput.classList.toggle("hidden", src !== "url");
    }

    toggle.addEventListener("click", function () {
      box.classList.toggle("hidden");
      if (!box.classList.contains("hidden")) loadCatalog();
    });
    srcSel.addEventListener("change", syncFields);
    syncFields();

    runBtn.addEventListener("click", function () {
      var src = srcSel.value;
      var statusEl = document.getElementById("sw-status");
      var payload = { source: src };
      var label;
      if (src === "catalog") {
        if (!catSel.value) { if (statusEl) statusEl.textContent = "Choisissez une application."; return; }
        payload.key = catSel.value;
        var opt = catSel.options[catSel.selectedIndex];
        label = "Installation de " + (opt ? opt.text : catSel.value);
      } else if (src === "winget") {
        var wid = widInput.value.trim();
        if (!wid) { widInput.focus(); return; }
        payload.winget_id = wid;
        label = "Installation de " + wid;
      } else {
        var url = urlInput.value.trim();
        if (!url) { urlInput.focus(); return; }
        payload.url = url;
        if (argsInput.value.trim()) payload.exe_args = argsInput.value.trim();
        label = "Installation depuis URL";
      }
      TS.confirm({
        title: label + " ?",
        body: "Installation silencieuse sous le compte SYSTEM du poste.",
        confirmLabel: "Installer",
      }).then(function (r) {
        if (!r.confirmed) return;
        runSoftwareCommand("install", payload, label);
      });
    });
  }

  // --- Affichage / polling d'une commande (partagé console + actions rapides) ---
  function cmdStatusLabel(s) {
    return {
      pending: "en attente",
      dispatched: "transmise à l'agent",
      running: "en cours",
      done: "terminée",
      error: "erreur",
      timeout: "délai dépassé",
    }[s] || s;
  }

  function renderCmdResult(outputEl, data) {
    var r = (data && data.result) || {};
    var lines = [];
    lines.push("Code de sortie : " + (r.exit_code != null ? r.exit_code : "—"));
    if (r.duration_seconds != null) lines.push("Durée : " + r.duration_seconds + " s");
    lines.push("");
    if (r.stdout) { lines.push("--- STDOUT ---"); lines.push(r.stdout); }
    if (r.stderr) { lines.push(""); lines.push("--- STDERR ---"); lines.push(r.stderr); }
    outputEl.textContent = lines.join("\n");
    outputEl.classList.remove("hidden");
  }

  // Sonde un command_id jusqu'à son état final. statusEl / outputEl / onDone optionnels.
  function pollCommand(commandId, statusEl, outputEl, onDone) {
    var attempts = 0;
    var timer = setInterval(async function () {
      attempts++;
      try {
        var resp = await fetch("/api/v1/commands/" + commandId, { headers: { Accept: "application/json" } });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        var data = await resp.json();
        if (statusEl) statusEl.textContent = "Statut : " + cmdStatusLabel(data.status);
        if (["done", "error", "timeout"].indexOf(data.status) !== -1) {
          clearInterval(timer);
          if (outputEl) renderCmdResult(outputEl, data);
          if (onDone) onDone(data);
        }
      } catch (e) {
        // On continue le polling malgré une erreur ponctuelle.
      }
      // Arrêt de sécurité après ~5 minutes de polling.
      if (attempts > 150) { clearInterval(timer); if (onDone) onDone(null); }
    }, 2000);
    return timer;
  }

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

      var ask = await TS.confirm({ title: "Exécuter cette commande ?", body: text, confirmLabel: "Exécuter" });
      if (!ask.confirmed) return;

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
        pollTimer = pollCommand(data.command_id, statusEl, outputEl, function () { runBtn.disabled = false; });
      } catch (e) {
        statusEl.textContent = "Erreur réseau lors de l'envoi.";
        runBtn.disabled = false;
      }
    });
  }

  // --- Actions rapides (admin uniquement) ---
  function setupQuickActions() {
    var buttons = document.querySelectorAll(".quick-actions .btn.quick");
    if (!buttons.length) return;
    var statusEl = document.getElementById("cmd-status");
    var outputEl = document.getElementById("cmd-output");
    var msgBox = document.getElementById("qa-message-box");
    var msgInput = document.getElementById("qa-message-text");
    var msgSend = document.getElementById("qa-message-send");

    var labels = {
      lock: "Verrouiller la session",
      restart: "Redémarrer le poste",
      logoff: "Déconnecter la session",
      message: "Envoyer un message",
    };

    async function runQuickAction(action, text) {
      if (action !== "message") {
        var ask = await TS.confirm({
          title: labels[action] + " ?",
          body: "Cette action sera exécutée sur le poste.",
          danger: (action === "restart" || action === "logoff"),
          confirmLabel: labels[action],
        });
        if (!ask.confirmed) return;
      }
      if (statusEl) statusEl.textContent = "Envoi de l'action…";
      if (outputEl) outputEl.classList.add("hidden");
      var body = { action: action };
      if (text) body.text = text;
      try {
        var resp = await fetch("/api/v1/agents/" + AGENT_ID + "/quick-action", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(body),
        });
        var data = await resp.json();
        if (!resp.ok) {
          if (statusEl) statusEl.textContent = "Erreur : " + (data.error || resp.status);
          return;
        }
        if (statusEl) statusEl.textContent = "Action mise en file (en attente d'exécution)…";
        if (data.command_id) {
          pollCommand(data.command_id, statusEl, outputEl);
        } else if (data.result || data.status) {
          // Réponse synchrone éventuelle.
          if (statusEl) statusEl.textContent = "Statut : " + cmdStatusLabel(data.status || "done");
          if (outputEl && data.result) renderCmdResult(outputEl, data);
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = "Erreur réseau lors de l'envoi.";
      }
    }

    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        var action = b.getAttribute("data-action");
        if (action === "message") {
          if (msgBox) msgBox.classList.toggle("hidden");
          if (msgInput && msgBox && !msgBox.classList.contains("hidden")) msgInput.focus();
          return;
        }
        runQuickAction(action);
      });
    });

    if (msgSend) {
      msgSend.addEventListener("click", function () {
        var text = (msgInput ? msgInput.value : "").trim();
        if (!text) { if (msgInput) msgInput.focus(); return; }
        runQuickAction("message", text);
        if (msgInput) msgInput.value = "";
        if (msgBox) msgBox.classList.add("hidden");
      });
    }
    if (msgInput) {
      msgInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { ev.preventDefault(); if (msgSend) msgSend.click(); }
      });
    }
  }

  // --- Étiquettes du poste (admin) ---
  var currentTags = [];
  function renderTags(tags) {
    var box = document.getElementById("tag-chips");
    if (!box) return;
    currentTags = Array.isArray(tags) ? tags.slice() : [];
    if (!currentTags.length) {
      box.innerHTML = '<span class="muted" style="font-size:12px">aucune</span>';
      return;
    }
    box.innerHTML = currentTags.map(function (t) {
      return '<span class="chip tag tag-removable">' + esc(t) +
        '<button type="button" class="tag-x" data-tag="' + esc(t) + '" title="Retirer">×</button></span>';
    }).join("");
    Array.prototype.forEach.call(box.querySelectorAll(".tag-x"), function (b) {
      b.addEventListener("click", function () {
        var t = b.getAttribute("data-tag");
        saveTags(currentTags.filter(function (x) { return x !== t; }));
      });
    });
  }
  function saveTags(tags) {
    fetch("/api/v1/agents/" + AGENT_ID + "/tags", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ tags: tags }),
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) renderTags(d.tags); })
      .catch(function () {});
  }
  function setupTags() {
    var input = document.getElementById("tag-input");
    if (!input) return;
    input.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      var v = input.value.trim();
      input.value = "";
      if (!v) return;
      var lower = currentTags.map(function (x) { return x.toLowerCase(); });
      if (lower.indexOf(v.toLowerCase()) === -1) saveTags(currentTags.concat([v]));
    });
  }

  // --- Bibliothèque de scripts 1-clic (admin) ---
  function setupScriptLibrary() {
    var container = document.getElementById("script-groups");
    if (!container) return;
    fetch("/api/v1/scripts", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (scripts) {
        if (!scripts.length) { container.innerHTML = '<div class="dl-empty">Aucun script.</div>'; return; }
        var byKey = {};
        var groups = {};
        scripts.forEach(function (s) {
          byKey[s.key] = s;
          (groups[s.category] = groups[s.category] || []).push(s);
        });
        container.innerHTML = Object.keys(groups).map(function (cat) {
          var btns = groups[cat].map(function (s) {
            return '<button type="button" class="btn quick script-btn' + (s.danger ? " danger" : "") +
              '" data-key="' + esc(s.key) + '">' + esc(s.label) + "</button>";
          }).join("");
          return '<div class="script-cat"><span class="script-cat-label">' + esc(cat) +
            '</span><div class="quick-row">' + btns + "</div></div>";
        }).join("");

        Array.prototype.forEach.call(container.querySelectorAll(".script-btn"), function (b) {
          b.addEventListener("click", function () {
            var s = byKey[b.getAttribute("data-key")];
            if (!s) return;
            // Pré-remplit la console (transparence) puis déclenche l'exécution
            // via le bouton existant (qui gère la confirmation + le polling + la sortie).
            document.getElementById("cmd-shell").value = s.shell;
            document.getElementById("cmd-text").value = s.command_text;
            document.getElementById("cmd-timeout").value = s.timeout;
            document.getElementById("cmd-run").click();
          });
        });
      })
      .catch(function () {
        container.innerHTML = '<div class="dl-empty err-cell">Erreur de chargement des scripts.</div>';
      });
  }

  // --- Processus (top CPU/RAM + arrêt), via le pipeline de commandes (admin) ---
  function setupProcesses() {
    var loadBtn = document.getElementById("proc-load");
    if (!loadBtn) return;
    var statusEl = document.getElementById("proc-status");
    var body = document.getElementById("proc-body");
    var PROC_CMD =
      "Get-Process | Sort-Object CPU -Descending | Select-Object -First 20 " +
      "Name,Id,@{n='CPU';e={[math]::Round($_.CPU,1)}}," +
      "@{n='RAM_MB';e={[math]::Round($_.WorkingSet64/1MB,0)}} | ConvertTo-Json -Compress";

    // Soumet une commande PowerShell et résout avec son résultat final.
    function runForResult(text, timeout) {
      return fetch("/api/v1/agents/" + AGENT_ID + "/commands", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ shell: "powershell", command_text: text, timeout_seconds: timeout }),
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
          return new Promise(function (resolve) { pollCommand(d.command_id, null, null, resolve); });
        });
      });
    }

    function render(procs) {
      if (!procs.length) { body.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun processus.</td></tr>'; return; }
      body.innerHTML = procs.map(function (p) {
        var name = p.Name || "—";
        var pid = p.Id;
        var cpu = p.CPU != null ? p.CPU : "—";
        var ram = p.RAM_MB != null ? p.RAM_MB : "—";
        return "<tr>" +
          '<td class="mono">' + esc(name) + "</td>" +
          '<td class="num mono text-dim">' + esc(pid) + "</td>" +
          '<td class="num mono">' + esc(cpu) + "</td>" +
          '<td class="num mono">' + esc(ram) + "</td>" +
          '<td class="num"><button class="btn xs danger proc-kill" data-pid="' + esc(pid) +
            '" data-name="' + esc(name) + '">Tuer</button></td>' +
          "</tr>";
      }).join("");
      Array.prototype.forEach.call(body.querySelectorAll(".proc-kill"), function (b) {
        b.addEventListener("click", function () { killProc(b.getAttribute("data-pid"), b.getAttribute("data-name")); });
      });
    }

    function load() {
      statusEl.textContent = "Chargement des processus…";
      loadBtn.disabled = true;
      body.innerHTML = '<tr><td colspan="5" class="empty-cell">Récupération…</td></tr>';
      runForResult(PROC_CMD, 30).then(function (data) {
        loadBtn.disabled = false;
        var r = (data && data.result) || {};
        if (!data || data.status !== "done") {
          statusEl.textContent = "Échec : " + cmdStatusLabel(data ? data.status : "?");
          body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">' + esc((r.stderr || "Pas de résultat").split("\n")[0]) + "</td></tr>";
          return;
        }
        var procs;
        try { procs = JSON.parse(r.stdout || "[]"); } catch (e) {
          body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">Réponse illisible.</td></tr>';
          return;
        }
        if (!Array.isArray(procs)) procs = [procs];
        statusEl.textContent = procs.length + " processus (triés par temps CPU)";
        render(procs);
      }).catch(function (e) {
        loadBtn.disabled = false;
        statusEl.textContent = "Erreur : " + e.message;
      });
    }

    function killProc(pid, name) {
      var pidNum = parseInt(pid, 10);
      if (isNaN(pidNum)) return;  // garde-fou (le PID doit être numérique)
      TS.confirm({
        title: "Arrêter « " + name + " » ?",
        body: "Le processus (PID " + pidNum + ") sera forcé à s'arrêter sur le poste.",
        danger: true, confirmLabel: "Arrêter",
      }).then(function (ask) {
      if (!ask.confirmed) return;
      statusEl.textContent = "Arrêt du processus " + pidNum + "…";
      runForResult("Stop-Process -Id " + pidNum + " -Force; 'OK'", 30).then(function (data) {
        var r = (data && data.result) || {};
        if (data && data.status === "done" && !(r.stderr && r.stderr.trim())) {
          statusEl.textContent = "Processus " + pidNum + " arrêté.";
        } else {
          statusEl.textContent = "Échec de l'arrêt de " + pidNum +
            (r.stderr ? " : " + r.stderr.split("\n")[0] : "");
        }
        setTimeout(load, 1200);
      }).catch(function (e) { statusEl.textContent = "Erreur : " + e.message; });
      });
    }

    loadBtn.addEventListener("click", load);
  }

  // --- Activité du poste : frise d'événements intéressants (1 appel consolidé) ---
  function fmtRel(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var secs = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (secs < 60) return "il y a " + secs + " s";
    if (secs < 3600) return "il y a " + Math.floor(secs / 60) + " min";
    if (secs < 86400) return "il y a " + Math.floor(secs / 3600) + " h";
    return "il y a " + Math.floor(secs / 86400) + " j";
  }

  // PowerShell consolidé → JSON {user, apps[], events[{time,kind,title,detail}]}.
  // (Sortie structurée — bien plus lisible qu'un dump texte.)
  var ACTIVITY_CMD = [
    "$ErrorActionPreference='SilentlyContinue'",
    "$u = \"$env:USERDOMAIN\\$env:USERNAME\"",
    "$apps = @(Get-Process | Where-Object {$_.MainWindowTitle} | Select-Object -ExpandProperty Name -Unique | Sort-Object)",
    "$events = New-Object System.Collections.ArrayList",
    "function Add-Ev($t,$k,$ti,$d){ [void]$events.Add([pscustomobject]@{time=$t.ToString('o');kind=$k;title=$ti;detail=$d}) }",
    "Get-WinEvent -FilterHashtable @{LogName='Security';Id=4624,4625;StartTime=(Get-Date).AddDays(-2)} -MaxEvents 40 -ErrorAction SilentlyContinue | ForEach-Object { $a=$_.Properties[5].Value; if($_.Id -eq 4624){ Add-Ev $_.TimeCreated 'logon' 'Ouverture de session' $a } else { Add-Ev $_.TimeCreated 'logon_fail' 'Échec de connexion' $a } }",
    "Get-WinEvent -FilterHashtable @{LogName='System';Id=6005,6006,1074,6008;StartTime=(Get-Date).AddDays(-7)} -MaxEvents 25 -ErrorAction SilentlyContinue | ForEach-Object { $ti=switch($_.Id){6005{'Démarrage du système'}6006{'Arrêt du système'}6008{'Arrêt inattendu'}1074{'Arrêt / redémarrage demandé'}default{'Événement système'}}; Add-Ev $_.TimeCreated 'power' $ti (($_.Message.Split([char]10))[0]) }",
    "Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2,3;StartTime=(Get-Date).AddDays(-1)} -MaxEvents 30 -ErrorAction SilentlyContinue | ForEach-Object { $k=if($_.Level -le 2){'error'}else{'warning'}; Add-Ev $_.TimeCreated $k $_.ProviderName (($_.Message.Split([char]10))[0]) }",
    "$sorted = @($events | Sort-Object {[datetime]$_.time} -Descending | Select-Object -First 60)",
    "[pscustomobject]@{ user=$u; apps=$apps; events=$sorted } | ConvertTo-Json -Depth 4 -Compress",
  ].join("\n");

  var KIND = {
    logon: { label: "Connexion", badge: "info", group: "logon" },
    logon_fail: { label: "Échec", badge: "danger", group: "logon" },
    power: { label: "Système", badge: "", group: "power" },
    error: { label: "Erreur", badge: "danger", group: "issue" },
    warning: { label: "Alerte", badge: "warn", group: "issue" },
  };

  var activityEvents = [];
  var activityFilter = "all";

  function setupActivity() {
    var panel = document.getElementById("panel-activity");
    if (!panel) return;
    var feed = document.getElementById("act-feed");
    var summary = document.getElementById("act-summary");
    var statusEl = document.getElementById("act-status");

    function submit(text, timeout) {
      return fetch("/api/v1/agents/" + AGENT_ID + "/commands", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ shell: "powershell", command_text: text, timeout_seconds: timeout }),
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
          return new Promise(function (resolve) { pollCommand(d.command_id, null, null, resolve); });
        });
      });
    }

    function renderSummary(user, apps) {
      apps = Array.isArray(apps) ? apps : (apps ? [apps] : []);
      var appChips = apps.slice(0, 12).map(function (a) { return '<span class="chip">' + esc(a) + "</span>"; }).join("");
      if (apps.length > 12) appChips += '<span class="chip muted">+' + (apps.length - 12) + "</span>";
      summary.innerHTML =
        '<div class="s-item"><span class="s-label">Utilisateur</span><span class="s-val mono">' + esc(user || "—") + "</span></div>" +
        '<div class="s-item s-apps"><span class="s-label">Applications ouvertes (' + apps.length + ")</span>" +
        '<span class="s-chips">' + (appChips || '<span class="muted">aucune</span>') + "</span></div>";
    }

    function renderFeed() {
      var rows = activityEvents.filter(function (e) {
        if (activityFilter === "all") return true;
        var g = (KIND[e.kind] || {}).group;
        return g === activityFilter;
      });
      if (!rows.length) {
        feed.innerHTML = '<div class="feed-empty">Aucun événement pour ce filtre.</div>';
        return;
      }
      feed.innerHTML = rows.map(function (e) {
        var k = KIND[e.kind] || { label: e.kind || "—", badge: "" };
        var abs = new Date(e.time);
        var absTxt = isNaN(abs.getTime()) ? "" : abs.toLocaleString("fr-FR");
        return (
          '<div class="feed-item b-' + esc(e.kind || "") + '">' +
          '<div class="feed-meta">' +
            '<span class="badge ' + k.badge + '">' + esc(k.label) + "</span>" +
            '<span class="feed-time" title="' + esc(absTxt) + '">' + esc(fmtRel(e.time)) + "</span>" +
          "</div>" +
          '<div class="feed-body"><div class="feed-title">' + esc(e.title || "—") + "</div>" +
            (e.detail ? '<div class="feed-detail">' + esc(e.detail) + "</div>" : "") +
          "</div></div>"
        );
      }).join("");
    }

    function load() {
      statusEl.textContent = "Analyse de l'activité…";
      feed.innerHTML = '<div class="dl-loading">Chargement…</div>';
      submit(ACTIVITY_CMD, 60).then(function (data) {
        var r = (data && data.result) || {};
        if (!data || data.status !== "done") {
          statusEl.textContent = "Échec : " + cmdStatusLabel(data ? data.status : "?");
          feed.innerHTML = '<div class="feed-empty err-cell">' + esc((r.stderr || "Pas de résultat").split("\n")[0]) + "</div>";
          return;
        }
        var parsed;
        try { parsed = JSON.parse(r.stdout || "{}"); } catch (e) {
          feed.innerHTML = '<div class="feed-empty err-cell">Réponse illisible.</div>'; return;
        }
        activityEvents = Array.isArray(parsed.events) ? parsed.events : (parsed.events ? [parsed.events] : []);
        renderSummary(parsed.user, parsed.apps);
        renderFeed();
        statusEl.textContent = activityEvents.length + " événement(s)";
      }).catch(function (e) { statusEl.textContent = "Erreur : " + e.message; });
    }

    // Filtres (segmented control).
    Array.prototype.forEach.call(panel.querySelectorAll("#act-filter .seg-btn"), function (btn) {
      btn.addEventListener("click", function () {
        activityFilter = btn.getAttribute("data-kind");
        Array.prototype.forEach.call(panel.querySelectorAll("#act-filter .seg-btn"), function (b) {
          b.classList.toggle("on", b === btn);
        });
        renderFeed();
      });
    });
    var refresh = document.getElementById("act-refresh");
    if (refresh) refresh.addEventListener("click", load);

    // Auto-charge à la première ouverture de l'onglet Activité.
    var loadedOnce = false;
    document.addEventListener("ts:tab-activated", function (ev) {
      if (ev.detail && ev.detail.tab === "activity" && !loadedOnce) { loadedOnce = true; load(); }
    });
  }

  // --- Mode agrandi : la zone de travail remplit la fenêtre (zéro scroll) ---
  function setupFocus() {
    var btn = document.getElementById("workzone-focus");
    if (!btn) return;
    function activeTab() {
      var t = document.querySelector(".workzone .tab[aria-selected='true']");
      return t ? t.getAttribute("data-tab") : "remote";
    }
    function apply(on) {
      document.body.classList.toggle("wz-focus", on);
      btn.classList.toggle("on", on);
      btn.title = on ? "Réduire (Échap)" : "Agrandir — remplir la fenêtre (Échap pour réduire)";
      try { localStorage.setItem("ts-wz-focus", on ? "1" : "0"); } catch (e) { /* ignore */ }
      // Laisse le layout s'appliquer puis prévient les composants (terminal → re-fit).
      setTimeout(function () {
        document.dispatchEvent(new CustomEvent("ts:tab-activated", { detail: { tab: activeTab() } }));
      }, 60);
    }
    btn.addEventListener("click", function () { apply(!document.body.classList.contains("wz-focus")); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("wz-focus")) apply(false);
    });
    // « Remplir la fenêtre » est actif par défaut (les infos matériel/métriques
    // sont dans l'onglet « Matériel », plus rien sous la zone de travail).
    // Reste désactivable (bouton / Échap) et le choix est mémorisé.
    try {
      var savedFocus = localStorage.getItem("ts-wz-focus");
      apply(savedFocus === null ? true : savedFocus === "1");
    } catch (e) { apply(true); }
  }

  // Regroupe Matériel + Métriques + Logiciels dans l'onglet « Matériel »
  // (déplace les sections existantes pour ne rien laisser sous la zone de travail).
  function groupInfoIntoTab() {
    var panel = document.getElementById("panel-hardware");
    if (!panel) return;
    var grid = document.querySelector(".grid-3");
    var swBody = document.getElementById("software-body");
    var swPanel = swBody ? swBody.closest(".panel") : null;
    if (grid) panel.appendChild(grid);
    if (swPanel) panel.appendChild(swPanel);
    // Les graphes Chart.js créés dans un onglet masqué peuvent rendre à 0 px :
    // on force un resize quand l'onglet « Matériel » devient visible.
    document.addEventListener("ts:tab-activated", function (ev) {
      if (ev.detail && ev.detail.tab === "hardware") {
        if (cpuChart) cpuChart.resize();
        if (ramChart) ramChart.resize();
      }
    });
  }

  // --- Onglets de la zone de travail (bureau / terminal / commande) ---
  function setupTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll(".workzone .tab"));
    if (!tabs.length) return;

    function activate(name) {
      tabs.forEach(function (t) {
        var on = t.getAttribute("data-tab") === name;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        t.classList.toggle("active", on);
        var panel = document.getElementById("panel-" + t.getAttribute("data-tab"));
        if (panel) panel.classList.toggle("hidden", !on);
      });
      // Prévient les composants actifs du changement d'onglet (ex. terminal → re-fit).
      document.dispatchEvent(new CustomEvent("ts:tab-activated", { detail: { tab: name } }));
    }

    tabs.forEach(function (t) {
      t.addEventListener("click", function () { activate(t.getAttribute("data-tab")); });
      t.addEventListener("keydown", function (ev) {
        var idx = tabs.indexOf(t);
        if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
          ev.preventDefault();
          var next = ev.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
          tabs[next].focus();
          activate(tabs[next].getAttribute("data-tab"));
        }
      });
    });

    // Activation à l'ouverture : une ancre explicite (#terminal / #console /
    // #remote) prime ; sinon le PREMIER onglet dans l'ordre de l'utilisateur
    // (= premier dans le DOM, après applyTabOrder).
    var hash = window.location.hash;
    if (hash === "#console") activate("command");
    else if (hash === "#terminal") activate("terminal");
    else if (hash === "#remote") activate("remote");
    else activate(tabs[0] ? tabs[0].getAttribute("data-tab") : "remote");
  }

  // --- Comptes utilisateurs locaux (admin) ---
  function setupAccounts() {
    var loadBtn = document.getElementById("acct-load");
    if (!loadBtn) return;
    var statusEl = document.getElementById("acct-status");
    var body = document.getElementById("acct-body");
    var createBox = document.getElementById("acct-create");
    var createOut = document.getElementById("acct-create-out");

    // POST vers un endpoint comptes dédié, puis sonde la commande jusqu'au résultat.
    function run(suffix, payload, sEl, oEl) {
      return fetch("/api/v1/agents/" + AGENT_ID + "/accounts/" + suffix, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload || {}),
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
          return new Promise(function (resolve) { pollCommand(d.command_id, sEl || null, oEl || null, resolve); });
        });
      });
    }

    function render(users) {
      if (!users.length) { body.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun compte.</td></tr>'; return; }
      body.innerHTML = users.map(function (u) {
        var en = u.enabled
          ? '<span class="pill ok">activé</span>'
          : '<span class="pill off">désactivé</span>';
        var role = u.admin ? '<span class="pill admin">admin</span>' : '<span class="text-faint">standard</span>';
        return "<tr>" +
          '<td class="mono">' + esc(u.name || "—") + "</td>" +
          "<td>" + en + "</td>" +
          "<td>" + role + "</td>" +
          '<td class="text-dim">' + esc(u.description || "—") + "</td>" +
          '<td class="num"><button class="btn xs danger acct-del" data-name="' + esc(u.name || "") + '">Supprimer</button></td>' +
          "</tr>";
      }).join("");
      Array.prototype.forEach.call(body.querySelectorAll(".acct-del"), function (b) {
        b.addEventListener("click", function () { delUser(b.getAttribute("data-name")); });
      });
    }

    function load() {
      statusEl.textContent = "Chargement des comptes…";
      loadBtn.disabled = true;
      body.innerHTML = '<tr><td colspan="5" class="empty-cell">Récupération…</td></tr>';
      run("list", {}, null, null).then(function (data) {
        loadBtn.disabled = false;
        var r = (data && data.result) || {};
        if (!data || data.status !== "done") {
          statusEl.textContent = "Échec : " + cmdStatusLabel(data ? data.status : "?");
          body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">' + esc((r.stderr || "Pas de résultat").split("\n")[0]) + "</td></tr>";
          return;
        }
        var users;
        try { users = JSON.parse(r.stdout || "[]"); } catch (e) {
          body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">Réponse illisible.</td></tr>';
          return;
        }
        if (!Array.isArray(users)) users = [users];
        statusEl.textContent = users.length + " compte(s)";
        render(users);
      }).catch(function (e) {
        loadBtn.disabled = false;
        statusEl.textContent = "Erreur : " + e.message;
      });
    }

    function delUser(name) {
      if (!name) return;
      TS.confirm({
        title: "Supprimer le compte « " + name + " » ?",
        body: "Le compte local sera supprimé sur ce poste.",
        danger: true, confirmLabel: "Supprimer",
        checkbox: "Supprimer aussi les fichiers du profil (C:\\Users\\" + name + ")",
      }).then(function (r) {
        if (!r.confirmed) return;
        statusEl.textContent = "Suppression de " + name + "…";
        run("delete", { username: name, remove_profile: r.checked }, statusEl, null)
          .then(function () { load(); })
          .catch(function (e) { statusEl.textContent = "Erreur : " + e.message; });
      });
    }

    loadBtn.addEventListener("click", load);
    document.getElementById("acct-new-toggle").addEventListener("click", function () {
      createBox.classList.toggle("hidden");
      if (!createBox.classList.contains("hidden")) document.getElementById("acct-username").focus();
    });

    document.getElementById("acct-create-run").addEventListener("click", function () {
      var username = document.getElementById("acct-username").value.trim();
      var fullname = document.getElementById("acct-fullname").value.trim();
      var password = document.getElementById("acct-password").value;
      var admin = document.getElementById("acct-admin").checked;
      if (!username) { statusEl.textContent = "Nom d'utilisateur requis."; return; }
      if (!password) { statusEl.textContent = "Mot de passe requis."; return; }

      function doCreate() {
        statusEl.textContent = "Création du compte…";
        createOut.classList.add("hidden");
        run("create", { username: username, full_name: fullname, password: password, administrator: admin }, statusEl, createOut)
          .then(function (data) {
            document.getElementById("acct-password").value = "";
            if (data && data.status === "done") { statusEl.textContent = "Compte créé."; TS.toast("Compte « " + username + " » créé.", "success"); }
            load();
          })
          .catch(function (e) { statusEl.textContent = "Erreur : " + e.message; });
      }

      if (admin) {
        TS.confirm({
          title: "Créer un compte ADMINISTRATEUR ?",
          body: "« " + username + " » disposera des droits d'administrateur sur le poste.",
          danger: true, confirmLabel: "Créer (admin)",
        }).then(function (r) { if (r.confirmed) doCreate(); });
      } else {
        doCreate();
      }
    });
  }

  // --- Correctifs Windows (admin) ---
  function sevBadgeClass(sev) {
    if (sev === "Critical" || sev === "Important") return "danger";
    if (sev === "Moderate") return "warn";
    if (sev === "Low") return "info";
    return "off";
  }

  function setupPatches() {
    var loadBtn = document.getElementById("patch-load");
    if (!loadBtn) return;
    var statusEl = document.getElementById("patch-status");
    var outputEl = document.getElementById("patch-output");
    var metaEl = document.getElementById("patch-meta");
    var body = document.getElementById("patch-body");
    var cbAll = document.getElementById("patch-cb-all");
    var rebootBadge = document.getElementById("patch-reboot-badge");
    var rebootBtn = document.getElementById("patch-reboot");
    var updatesCache = [];

    function render(data) {
      var pc = data.pending_count;
      var crit = data.pending_critical;
      var parts = [];
      if (pc != null) parts.push(pc + (pc === 1 ? " correctif en attente" : " correctifs en attente"));
      if (crit != null) parts.push(crit + " critique(s)/important(s)");
      if (data.last_search_at) parts.push("dernière analyse : " + fmtDate(data.last_search_at));
      else if (data.collected_at) parts.push("relevé : " + fmtDate(data.collected_at));
      metaEl.textContent = parts.join(" · ") || "Aucune donnée de correctifs (poste non encore inventorié ou agent ancien).";

      var pending = !!data.reboot_pending;
      rebootBadge.classList.toggle("hidden", !pending);
      rebootBtn.classList.toggle("hidden", !pending);

      updatesCache = Array.isArray(data.updates) ? data.updates : [];
      if (!updatesCache.length) {
        body.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucun correctif détaillé. '
          + (pc ? "Le poste rapporte " + pc + " MAJ — lancez « Rescanner » pour le détail." : "Poste à jour.")
          + "</td></tr>";
        if (cbAll) cbAll.checked = false;
        return;
      }
      body.innerHTML = patchRowsHtml(updatesCache);
      if (cbAll) cbAll.checked = false;
    }

    function patchRowsHtml(list) {
      return list.map(function (u) {
        var kb = u.kb || "—";
        var sev = u.severity || "Unknown";
        var size = (u.size_mb != null) ? u.size_mb : "—";
        var reboot = u.reboot_required ? '<span class="badge warn">oui</span>' : '<span class="text-faint">—</span>';
        var cbCell = (kb && kb !== "unknown")
          ? '<td class="cb-col"><input type="checkbox" class="patch-cb" data-kb="' + esc(kb) + '"></td>'
          : '<td class="cb-col"></td>';
        return "<tr>" +
          cbCell +
          '<td class="mono">' + esc(kb) + "</td>" +
          "<td>" + esc(u.title || "—") + "</td>" +
          '<td><span class="badge ' + sevBadgeClass(sev) + '">' + esc(sev) + "</span></td>" +
          '<td class="num mono">' + esc(String(size)) + "</td>" +
          "<td>" + reboot + "</td>" +
          "</tr>";
      }).join("");
    }

    // Le rescan renvoie, après un marqueur, un JSON de la liste des correctifs.
    function parseRescanUpdates(stdout) {
      var marker = "===PATCHES_JSON===";
      var i = (stdout || "").indexOf(marker);
      if (i === -1) return null;
      try {
        var arr = JSON.parse(stdout.slice(i + marker.length).trim());
        return Array.isArray(arr) ? arr : null;
      } catch (e) { return null; }
    }

    function loadPatches() {
      statusEl.textContent = "Chargement…";
      fetch("/api/v1/agents/" + AGENT_ID + "/patch", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
        .then(function (data) { statusEl.textContent = ""; render(data); })
        .catch(function () {
          statusEl.textContent = "Erreur de chargement.";
          body.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Erreur de chargement</td></tr>';
        });
    }

    function selectedKbs() {
      return Array.prototype.map.call(
        document.querySelectorAll(".patch-cb:checked"),
        function (c) { return c.getAttribute("data-kb"); }
      );
    }

    function doInstall(mode, kbList, label) {
      var body2 = "Des correctifs Windows vont être installés sur ce poste sous le compte SYSTEM. "
        + "Le poste peut nécessiter un redémarrage en fin d'installation — un utilisateur "
        + "peut être en session. Aucun redémarrage ne sera déclenché automatiquement.";
      TS.confirm({ title: label + " ?", body: body2, danger: true, confirmLabel: "Installer" })
        .then(function (r) {
          if (!r.confirmed) return;
          statusEl.textContent = label + " — envoi…";
          if (outputEl) outputEl.classList.add("hidden");
          var payload = { mode: mode };
          if (mode === "selected") payload.kb_list = kbList;
          fetch("/api/v1/agents/" + AGENT_ID + "/patch/install", {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify(payload),
          }).then(function (resp) {
            return resp.json().then(function (d) { return { ok: resp.ok, data: d }; });
          }).then(function (res) {
            if (!res.ok) { statusEl.textContent = "Erreur : " + (res.data.error || "envoi impossible"); return; }
            statusEl.textContent = label + " — en file, exécution sur le poste (peut être long)…";
            pollCommand(res.data.command_id, statusEl, outputEl, function () { loadPatches(); });
          }).catch(function () { statusEl.textContent = "Erreur réseau lors de l'envoi."; });
        });
    }

    loadBtn.addEventListener("click", loadPatches);

    document.getElementById("patch-rescan").addEventListener("click", function () {
      statusEl.textContent = "Analyse du poste…";
      if (outputEl) outputEl.classList.add("hidden");
      fetch("/api/v1/agents/" + AGENT_ID + "/patch/rescan", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: "{}",
      }).then(function (resp) {
        return resp.json().then(function (d) { return { ok: resp.ok, data: d }; });
      }).then(function (res) {
        if (!res.ok) { statusEl.textContent = "Erreur : " + (res.data.error || "envoi impossible"); return; }
        statusEl.textContent = "Analyse en cours sur le poste…";
        pollCommand(res.data.command_id, statusEl, null, function (data) {
          if (!data) { statusEl.textContent = "Analyse interrompue."; return; }
          if (["error", "timeout"].indexOf(data.status) !== -1) {
            statusEl.textContent = "Échec de l'analyse.";
            if (outputEl) renderCmdResult(outputEl, data);
            return;
          }
          var parsed = parseRescanUpdates(data.result && data.result.stdout);
          if (parsed === null) {
            // Repli : sortie non reconnue → on affiche le texte brut.
            statusEl.textContent = "Analyse terminée (format non reconnu).";
            if (outputEl) renderCmdResult(outputEl, data);
            return;
          }
          updatesCache = parsed;
          body.innerHTML = parsed.length
            ? patchRowsHtml(parsed)
            : '<tr><td colspan="6" class="empty-cell">Aucun correctif en attente. Poste à jour.</td></tr>';
          if (cbAll) cbAll.checked = false;
          metaEl.textContent = parsed.length
            + (parsed.length === 1 ? " correctif détecté" : " correctifs détectés")
            + " — analyse en direct. Cochez puis « Installer la sélection ».";
          statusEl.textContent = "Analyse terminée.";
        });
      }).catch(function () { statusEl.textContent = "Erreur réseau lors de l'envoi."; });
    });

    document.getElementById("patch-install-critical").addEventListener("click", function () {
      doInstall("critical", null, "Installation des correctifs critiques");
    });
    document.getElementById("patch-install-all").addEventListener("click", function () {
      doInstall("all", null, "Installation de tous les correctifs");
    });
    document.getElementById("patch-install-selected").addEventListener("click", function () {
      var kbs = selectedKbs();
      if (!kbs.length) { statusEl.textContent = "Sélectionnez au moins un correctif (KB)."; return; }
      doInstall("selected", kbs, "Installation de " + kbs.length + " correctif(s)");
    });

    if (cbAll) {
      cbAll.addEventListener("change", function () {
        Array.prototype.forEach.call(document.querySelectorAll(".patch-cb"), function (c) { c.checked = cbAll.checked; });
      });
    }

    rebootBtn.addEventListener("click", function () {
      TS.confirm({
        title: "Redémarrer le poste ?",
        body: "Le poste va redémarrer pour finaliser les correctifs. Un utilisateur peut être en session.",
        danger: true, confirmLabel: "Redémarrer",
      }).then(function (r) {
        if (!r.confirmed) return;
        statusEl.textContent = "Envoi du redémarrage…";
        fetch("/api/v1/agents/" + AGENT_ID + "/quick-action", {
          method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ action: "restart" }),
        }).then(function (resp) {
          return resp.json().then(function (d) { return { ok: resp.ok, data: d }; });
        }).then(function (res) {
          if (!res.ok) { statusEl.textContent = "Erreur : " + (res.data.error || "envoi impossible"); return; }
          statusEl.textContent = "Redémarrage demandé.";
          pollCommand(res.data.command_id, statusEl, null);
        }).catch(function () { statusEl.textContent = "Erreur réseau lors de l'envoi."; });
      });
    });

    // Chargement paresseux à la première ouverture de l'onglet.
    var loadedOnce = false;
    document.addEventListener("ts:tab-activated", function (ev) {
      if (ev.detail && ev.detail.tab === "patches" && !loadedOnce) { loadedOnce = true; loadPatches(); }
    });
  }

  // Applique l'ordre des onglets personnalisé dans les Réglages (data-tab-order).
  // À exécuter AVANT setupTabs pour que la navigation clavier suive le nouvel ordre.
  function applyTabOrder() {
    var tablist = document.querySelector(".workzone .tablist");
    if (!tablist) return;
    var raw = pageData.getAttribute("data-tab-order");
    if (!raw) return;
    var order;
    try { order = JSON.parse(raw); } catch (e) { return; }
    if (!Array.isArray(order) || !order.length) return;
    var spacer = tablist.querySelector(".tablist-spacer");
    var tabs = Array.prototype.slice.call(tablist.querySelectorAll(".tab"));
    var byKey = {};
    tabs.forEach(function (t) { byKey[t.getAttribute("data-tab")] = t; });
    var placed = {};
    // 1) Onglets de l'ordre voulu (s'ils existent), réinsérés avant le spacer.
    order.forEach(function (key) {
      var btn = byKey[key];
      if (btn) { tablist.insertBefore(btn, spacer); placed[key] = true; }
    });
    // 2) Onglets non listés : conservés dans leur ordre d'origine, avant le spacer.
    tabs.forEach(function (t) {
      if (!placed[t.getAttribute("data-tab")]) tablist.insertBefore(t, spacer);
    });
  }

  // --- Initialisation ---
  // Regroupe matériel/métriques/logiciels dans l'onglet AVANT de créer les
  // graphes (les canvases doivent être à leur place définitive).
  if (IS_ADMIN) groupInfoIntoTab();
  loadDetail();
  loadMetrics();
  loadSoftware();
  if (IS_ADMIN) {
    setupConsole();
    setupQuickActions();
    setupScriptLibrary();
    setupProcesses();
    setupActivity();
    setupAccounts();
    setupPatches();
    setupTags();
    setupFocus();
    applyTabOrder();
    setupTabs();
    setupSoftwareDeploy();
  }

  setInterval(loadDetail, DETAIL_REFRESH_MS);
  setInterval(loadMetrics, 60000);
})();
