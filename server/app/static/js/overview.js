// TrueSight - Vue d'ensemble : santé du parc, problèmes, emplacements.
// API : GET /api/v1/overview ; gestion des sites (admin) : POST/PATCH/DELETE /api/v1/sites.
(function () {
  "use strict";

  var REFRESH_MS = 20000;
  var ovd = document.getElementById("ov-data");
  var IS_ADMIN = !!(ovd && ovd.getAttribute("data-is-admin") === "1");

  // Ordre + libellés + couleurs des états de santé.
  var HEALTH = [
    { key: "healthy",  label: "Sain",       color: "var(--ok)" },
    { key: "warning",  label: "Attention",  color: "var(--warn)" },
    { key: "critical", label: "Défectueux", color: "var(--danger)" },
    { key: "unknown",  label: "Inconnu",    color: "#5A6773" },
  ];

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }
  async function jsonOf(r) { try { return await r.json(); } catch (_) { return {}; } }

  function renderHealth(data) {
    var h = data.health || {};
    var total = data.total || 0;
    setText("ov-healthy-pct", total ? (data.healthy_pct != null ? data.healthy_pct + " % en bon état" : "—") : "aucun poste");

    var bar = document.getElementById("ov-healthbar");
    var legend = document.getElementById("ov-legend");
    if (!bar || !legend) return;

    if (!total) {
      bar.innerHTML = '<div class="hseg" style="width:100%;background:var(--bg-3)"></div>';
      legend.innerHTML = '<span class="text-faint">Aucun poste supervisé pour l\'instant.</span>';
      return;
    }
    bar.innerHTML = HEALTH.map(function (s) {
      var n = h[s.key] || 0;
      if (!n) return "";
      var w = (n / total) * 100;
      return '<div class="hseg" title="' + s.label + ' : ' + n + '" style="width:' + w + "%;background:" + s.color + '"></div>';
    }).join("");
    legend.innerHTML = HEALTH.map(function (s) {
      var n = h[s.key] || 0;
      return '<span class="hleg"><i style="background:' + s.color + '"></i>' + s.label +
             ' <b>' + n + "</b></span>";
    }).join("");
  }

  function renderKpis(data) {
    setText("ov-total", data.total || 0);
    setText("ov-online", data.online || 0);
    setText("ov-online-sub", (data.offline || 0) + " hors ligne");
    setText("ov-alerts", data.active_alerts || 0);
    setText("ov-updates", data.updates_available || 0);
    setText("ov-updates-sub", data.current_agent_version ? ("vers v" + data.current_agent_version) : "aucune publiée");
    var d = new Date();
    setText("ov-updated", "actualisé à " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }));
  }

  var PROBLEM_ICON = {
    offline: "i-unplug", disk_low: "i-hdd", cpu_high: "i-cpu", ram_high: "i-cpu",
    updates: "i-download", defender: "i-shield",
  };
  var PROBLEM_HREF = {
    offline: "/agents?status=offline",
    updates: "/agents?security=updates",
    defender: "/agents?security=defender",
  };

  function renderProblems(problems) {
    var box = document.getElementById("ov-problems");
    if (!box) return;
    var rows = (problems || []).filter(function (p) { return p.count > 0; });
    if (!rows.length) {
      box.innerHTML = '<div class="prob-ok"><svg><use href="#i-check"/></svg> Aucun problème détecté sur le parc.</div>';
      return;
    }
    box.innerHTML = rows.map(function (p) {
      // Drill-down : Parc filtré (offline/updates/defender) ; sinon page Alertes.
      var href = PROBLEM_HREF[p.type] || "/alerts";
      var sev = (p.type === "offline" || p.type === "disk_low") ? "crit" : "warn";
      return '<a class="prob-row" href="' + href + '">' +
        '<span class="prob-ic ' + sev + '"><svg><use href="#' + (PROBLEM_ICON[p.type] || "i-alert") + '"/></svg></span>' +
        '<span class="prob-lb">' + esc(p.label) + "</span>" +
        '<span class="prob-ct">' + p.count + "</span></a>";
    }).join("");
  }

  function healthDots(health) {
    return HEALTH.map(function (s) {
      var n = (health && health[s.key]) || 0;
      if (!n) return "";
      return '<span class="hdot" title="' + s.label + ' : ' + n + '"><i style="background:' + s.color + '"></i>' + n + "</span>";
    }).join("");
  }

  function renderSites(sites) {
    var box = document.getElementById("ov-sites");
    if (!box) return;
    setText("ov-sites-count", (sites || []).length + (sites.length === 1 ? " emplacement" : " emplacements"));
    if (!sites.length) {
      box.innerHTML = '<div class="empty-cell">Aucun emplacement.' +
        (IS_ADMIN ? " Créez-en un ci-dessus." : "") + "</div>";
      return;
    }
    box.innerHTML = sites.map(function (s) {
      var color = s.color || "#5A6773";
      var href = "/agents?site=" + (s.id ? encodeURIComponent(s.id) : "none");
      var admin = (IS_ADMIN && s.id)
        ? '<span class="site-actions">' +
            '<button type="button" class="btn-ico sm" data-act="rename" data-id="' + esc(s.id) + '" data-name="' + esc(s.name) + '" title="Renommer"><svg><use href="#i-cog"/></svg></button>' +
            '<button type="button" class="btn-ico sm" data-act="del" data-id="' + esc(s.id) + '" data-name="' + esc(s.name) + '" title="Supprimer"><svg><use href="#i-x"/></svg></button>' +
          "</span>"
        : "";
      return '<div class="site-card" data-href="' + href + '">' +
        '<div class="site-top"><span class="site-pin" style="background:' + esc(color) + '"></span>' +
          '<span class="site-name">' + esc(s.name) + "</span>" + admin + "</div>" +
        '<div class="site-meta"><span class="site-n">' + s.total + (s.total === 1 ? " poste" : " postes") + "</span>" +
          '<span class="site-on">' + s.online + " en ligne</span></div>" +
        '<div class="site-dots">' + (healthDots(s.health) || '<span class="text-faint">—</span>') + "</div>" +
        "</div>";
    }).join("");

    // Navigation (clic carte) sauf clic sur une action admin.
    Array.prototype.forEach.call(box.querySelectorAll(".site-card"), function (card) {
      card.addEventListener("click", function () { window.location.href = card.getAttribute("data-href"); });
    });
    Array.prototype.forEach.call(box.querySelectorAll(".site-actions .btn-ico"), function (b) {
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var id = b.getAttribute("data-id");
        var name = b.getAttribute("data-name");
        if (b.getAttribute("data-act") === "rename") renameSite(id, name);
        else deleteSite(id, name);
      });
    });
  }

  async function renameSite(id, current) {
    var name = window.prompt("Nouveau nom de l'emplacement :", current);
    if (name === null) return;
    name = name.trim();
    if (!name) return;
    var r = await fetch("/api/v1/sites/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ name: name }),
    });
    if (!r.ok) { var d = await jsonOf(r); window.alert(d.error || "Échec du renommage."); }
    load();
  }

  async function deleteSite(id, name) {
    if (!window.confirm("Supprimer l'emplacement « " + name + " » ?\nLes postes associés deviendront « non assignés ».")) return;
    var r = await fetch("/api/v1/sites/" + id, { method: "DELETE", headers: { Accept: "application/json" } });
    if (!r.ok) { var d = await jsonOf(r); window.alert(d.error || "Échec de la suppression."); }
    load();
  }

  // Création d'un emplacement (admin).
  var createForm = document.getElementById("site-create");
  if (createForm) {
    createForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var msg = document.getElementById("ns-msg");
      if (msg) { msg.textContent = ""; msg.className = "form-msg"; }
      var name = document.getElementById("ns-name").value.trim();
      var color = document.getElementById("ns-color").value;
      if (!name) return;
      var r = await fetch("/api/v1/sites", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ name: name, color: color }),
      });
      var d = await jsonOf(r);
      if (!r.ok) { if (msg) { msg.textContent = d.error || "Échec."; msg.className = "form-msg err"; } return; }
      createForm.reset();
      document.getElementById("ns-color").value = "#34e2b0";
      load();
    });
  }

  async function load() {
    try {
      var r = await fetch("/api/v1/overview", { headers: { Accept: "application/json" } });
      if (r.status === 401) { window.location.href = "/login"; return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      var data = await r.json();
      renderHealth(data);
      renderKpis(data);
      renderProblems(data.problems);
      renderSites(data.sites);
    } catch (e) {
      console.error("Vue d'ensemble : échec du chargement", e);
    }
  }

  load();
  setInterval(load, REFRESH_MS);
})();
