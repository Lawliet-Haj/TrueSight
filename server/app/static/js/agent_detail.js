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
        if (!window.confirm(labels[action] + " ?\n\nCette action sera exécutée sur le poste.")) return;
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
      if (!window.confirm("Tuer le processus « " + name + " » (PID " + pidNum + ") sur le poste ?")) return;
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

    // Activation par ancre (#terminal / #console / #remote) à l'ouverture.
    var hash = window.location.hash;
    if (hash === "#console") activate("command");
    else if (hash === "#terminal") activate("terminal");
    else activate("remote");
  }

  // --- Initialisation ---
  loadDetail();
  loadMetrics();
  loadSoftware();
  if (IS_ADMIN) {
    setupConsole();
    setupQuickActions();
    setupScriptLibrary();
    setupProcesses();
    setupActivity();
    setupTags();
    setupTabs();
  }

  setInterval(loadDetail, DETAIL_REFRESH_MS);
  setInterval(loadMetrics, 60000);
})();
