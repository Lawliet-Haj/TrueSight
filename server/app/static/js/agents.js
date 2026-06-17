// TrueSight — page Parc : KPIs instrumentés + tableau du parc, rafraîchi toutes les 10 s.
// Données : GET /api/v1/agents → [{id,hostname,os_version,status,last_seen_at,cpu_pct,ram_used_pct,tags,is_active}]
// Admin : sélection multiple + actions groupées (POST /api/v1/agents/bulk).
(function () {
  "use strict";

  var REFRESH_MS = 10000;
  var CPU_ALERT = 85;
  var RAM_ALERT = 85;
  var lastData = [];

  var pd = document.getElementById("parc-data");
  var IS_ADMIN = !!(pd && pd.getAttribute("data-is-admin") === "1");
  var COLSPAN = IS_ADMIN ? 9 : 8;
  var selected = {};  // { agent_id: true } — sélection persistée entre rafraîchissements
  var statusFilter = "all";  // all | online | offline | alert
  var sitesById = {};  // { site_id: {name,color} } — pour la résolution / les libellés

  var HEALTH_LABEL = { healthy: "Sain", warning: "Attention", critical: "Défectueux", unknown: "Inconnu" };

  // Filtres issus de l'URL (clic depuis la Vue d'ensemble) : ?site= / ?health= / ?status=.
  var qp = new URLSearchParams(window.location.search);
  var siteParam = qp.get("site") || "";
  var healthParam = (qp.get("health") || "").toLowerCase();
  var securityParam = (qp.get("security") || "").toLowerCase();
  if ((qp.get("status") || "").toLowerCase() === "offline") statusFilter = "offline";

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtDate(iso) {
    if (!iso) return "jamais";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    return d.toLocaleString("fr-FR");
  }

  function fmtRelative(iso) {
    if (!iso) return "jamais vu";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var secs = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (secs < 60) return "il y a " + secs + " s";
    if (secs < 3600) return "il y a " + Math.floor(secs / 60) + " min";
    if (secs < 86400) return "il y a " + Math.floor(secs / 3600) + " h";
    return "il y a " + Math.floor(secs / 86400) + " j";
  }

  function gaugeColor(v) {
    if (v >= 85) return "var(--danger)";
    if (v >= 60) return "var(--warn)";
    return "var(--ok)";
  }

  function gauge(pct, off) {
    if (off || pct === null || pct === undefined) return '<span class="seen">—</span>';
    var v = Math.max(0, Math.min(100, pct));
    return (
      '<div class="gauge">' +
      '<div class="track"><div class="fill" style="width:' + v + "%;background:" + gaugeColor(v) + '"></div></div>' +
      '<span class="pc">' + v.toFixed(0) + "%</span>" +
      "</div>"
    );
  }

  function rowState(r) {
    if (r.status !== "online") return "off";
    var cpu = r.cpu_pct, ram = r.ram_used_pct;
    if ((cpu != null && cpu >= CPU_ALERT) || (ram != null && ram >= RAM_ALERT)) return "alert";
    return "on";
  }

  function setText(id, txt) {
    var el = document.getElementById(id);
    if (el) el.textContent = txt;
  }

  function renderKpis(rows) {
    var online = 0, offline = 0, alert = 0;
    rows.forEach(function (r) {
      var st = rowState(r);
      if (st === "off") offline++;
      else { online++; if (st === "alert") alert++; }
    });
    var total = rows.length;
    setText("count-online", online);
    setText("count-offline", offline);
    setText("count-alert", alert);

    var availEl = document.getElementById("count-avail");
    if (availEl) {
      if (total === 0) availEl.innerHTML = "—<small>%</small>";
      else availEl.innerHTML = ((online / total) * 100).toFixed(1).replace(".", ",") + '<small>%</small>';
    }

    setText("kpi-online-sub", online === 1 ? "poste joignable" : "postes joignables");
    setText("kpi-offline-sub", "sans heartbeat récent");
    setText("kpi-alert-sub", alert === 0 ? "aucune surcharge" : (alert === 1 ? "1 poste en surcharge" : alert + " postes en surcharge"));
    setText("kpi-avail-sub", total + (total === 1 ? " poste supervisé" : " postes supervisés"));
    setText("fleet-count", total + (total === 1 ? " poste" : " postes"));

    var sbAgents = document.getElementById("sb-agents");
    if (sbAgents) sbAgents.innerHTML = "agents&nbsp;<b>" + online + "/" + total + "</b>&nbsp;reportent";
  }

  function render(rows) {
    var body = document.getElementById("agents-body");
    if (!body) return;
    var filterEl = document.getElementById("filter");
    var filter = ((filterEl && filterEl.value) || "").toLowerCase();

    renderKpis(rows);

    // Purge la sélection des postes disparus.
    var present = {};
    rows.forEach(function (r) { present[r.id] = true; });
    Object.keys(selected).forEach(function (id) { if (!present[id]) delete selected[id]; });

    var filtered = rows.filter(function (r) {
      if (statusFilter !== "all") {
        var st = rowState(r);
        if (statusFilter === "online" && st === "off") return false;
        if (statusFilter === "offline" && st !== "off") return false;
        if (statusFilter === "alert" && st !== "alert") return false;
      }
      if (!filter) return true;
      var hay = [r.hostname, r.os_version, (r.tags || []).join(" ")].join(" ").toLowerCase();
      return hay.indexOf(filter) !== -1;
    });

    if (!filtered.length) {
      body.innerHTML = '<tr><td colspan="' + COLSPAN + '" class="empty-cell">Aucun poste</td></tr>';
      updateBulkBar();
      return;
    }

    body.innerHTML = filtered
      .map(function (r) {
        var st = rowState(r);
        var off = st === "off";
        var hk = r.health || (off ? "unknown" : "healthy");
        var hlabel = HEALTH_LABEL[hk] || "Inconnu";
        var reasons = (r.health_reasons || []).join(", ");
        var sub = hlabel + (reasons && hk !== "healthy" ? " · " + reasons : "");

        var tags = (r.tags || [])
          .map(function (t) { return '<span class="chip tag tag-click" data-tag="' + esc(t) + '">' + esc(t) + "</span>"; })
          .join("");
        var revoked = r.is_active === false ? '<span class="revoked">révoqué</span>' : "";
        var remoteDisabled = off ? "disabled" : "";

        var site = r.site_name
          ? '<span class="site-chip"><i style="background:' + esc(r.site_color || "#5A6773") + '"></i>' + esc(r.site_name) + "</span>"
          : '<span class="muted">—</span>';

        var cb = IS_ADMIN
          ? '<td class="cb-col"><input type="checkbox" class="row-cb" data-id="' + esc(r.id) + '"' +
            (selected[r.id] ? " checked" : "") + "></td>"
          : "";

        var adminActs = IS_ADMIN
          ? '<div class="btn-ico" data-act="rename" data-id="' + esc(r.id) + '" data-name="' + esc(r.display_name || "") + '" title="Renommer"><svg><use href="#i-cog"/></svg></div>' +
            '<div class="btn-ico danger" data-act="del" data-id="' + esc(r.id) + '" data-name="' + esc(r.name || r.hostname || r.id) + '" title="Supprimer du parc"><svg><use href="#i-x"/></svg></div>'
          : "";

        return (
          '<tr class="clickable" data-href="/agents/' + esc(r.id) + '">' +
          cb +
          '<td><div class="host"><span class="dot ' + esc(hk) + '" title="' + esc(hlabel) + '"></span>' +
            '<div><div class="nm">' + esc(r.name || r.hostname || r.id) + revoked + "</div>" +
            '<div class="us hs-' + esc(hk) + '">' + esc(sub) + "</div></div></div></td>" +
          '<td class="text-dim">' + esc(r.os_version || "—") + "</td>" +
          "<td>" + site + "</td>" +
          "<td>" + gauge(r.cpu_pct, off) + "</td>" +
          "<td>" + gauge(r.ram_used_pct, off) + "</td>" +
          "<td>" + (tags || '<span class="muted">—</span>') + "</td>" +
          '<td><span class="seen" title="' + esc(fmtDate(r.last_seen_at)) + '">' + esc(fmtRelative(r.last_seen_at)) + "</span></td>" +
          '<td><div class="act">' +
            '<div class="btn-ico ' + remoteDisabled + '" data-act="remote" data-id="' + esc(r.id) + '" title="Bureau à distance"><svg><use href="#i-screen"/></svg></div>' +
            '<div class="btn-ico ' + remoteDisabled + '" data-act="cmd" data-id="' + esc(r.id) + '" title="Commande à distance"><svg><use href="#i-terminal"/></svg></div>' +
            adminActs +
          "</div></td>" +
          "</tr>"
        );
      })
      .join("");

    // Ligne cliquable → fiche poste (sauf clic sur action / case / tag).
    Array.prototype.forEach.call(body.querySelectorAll("tr.clickable"), function (tr) {
      tr.addEventListener("click", function () { window.location.href = tr.getAttribute("data-href"); });
    });

    // Boutons d'action ligne.
    Array.prototype.forEach.call(body.querySelectorAll(".btn-ico"), function (b) {
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (b.classList.contains("disabled")) return;
        var id = b.getAttribute("data-id");
        var act = b.getAttribute("data-act");
        if (act === "rename") { renameAgent(id, b.getAttribute("data-name") || ""); return; }
        if (act === "del") { deleteAgent(id, b.getAttribute("data-name") || ""); return; }
        window.location.href = "/agents/" + id + (act === "remote" ? "#remote" : "#console");
      });
    });

    // Tags cliquables → filtre la liste.
    Array.prototype.forEach.call(body.querySelectorAll(".tag-click"), function (c) {
      c.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var t = c.getAttribute("data-tag");
        var gs = document.getElementById("global-search");
        if (gs) gs.value = t;
        if (filterEl) filterEl.value = t;
        render(lastData);
      });
    });

    // Cases à cocher (admin).
    Array.prototype.forEach.call(body.querySelectorAll(".row-cb"), function (cb) {
      cb.addEventListener("click", function (ev) { ev.stopPropagation(); });
      cb.addEventListener("change", function () {
        var id = cb.getAttribute("data-id");
        if (cb.checked) selected[id] = true; else delete selected[id];
        updateBulkBar();
      });
    });

    updateBulkBar();
  }

  // --- Actions groupées (admin) ---
  function selectedIds() { return Object.keys(selected); }

  function updateBulkBar() {
    var bar = document.getElementById("bulkbar");
    if (!bar) return;
    var ids = selectedIds();
    var countEl = document.getElementById("bulk-count");
    if (countEl) countEl.textContent = ids.length + (ids.length === 1 ? " poste sélectionné" : " postes sélectionnés");
    bar.classList.toggle("hidden", ids.length === 0);
    var all = document.getElementById("cb-all");
    if (all) {
      var visible = Array.prototype.slice.call(document.querySelectorAll(".row-cb"));
      var checkedVisible = visible.filter(function (c) { return c.checked; }).length;
      all.checked = visible.length > 0 && checkedVisible === visible.length;
      all.indeterminate = checkedVisible > 0 && checkedVisible < visible.length;
    }
  }

  var BULK_LABELS = { lock: "Verrouiller", restart: "Redémarrer" };

  async function applyBulk(kind, payload, confirmMsg) {
    var ids = selectedIds();
    if (!ids.length) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    var body = Object.assign({ agent_ids: ids, kind: kind }, payload);
    try {
      var resp = await fetch("/api/v1/agents/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) { window.alert("Échec : " + (data.error || resp.status)); return; }
      window.alert("Action envoyée à " + data.count + " poste(s).");
      selected = {};
      render(lastData);
    } catch (e) {
      window.alert("Erreur réseau lors de l'envoi groupé.");
    }
  }

  function setupBulk() {
    var bar = document.getElementById("bulkbar");
    if (!bar) return;

    Array.prototype.forEach.call(bar.querySelectorAll("[data-bulk]"), function (b) {
      b.addEventListener("click", function () {
        var kind = b.getAttribute("data-bulk");
        var n = selectedIds().length;
        if (kind === "lock" || kind === "restart") {
          applyBulk("quick", { action: kind }, BULK_LABELS[kind] + " " + n + " poste(s) ?");
        } else if (kind === "message") {
          var msg = window.prompt("Message à afficher sur les " + n + " poste(s) :");
          if (msg && msg.trim()) applyBulk("quick", { action: "message", text: msg.trim() });
        } else if (kind === "command") {
          var cmd = window.prompt("Commande PowerShell à exécuter sur les " + n + " poste(s) :");
          if (cmd && cmd.trim()) {
            applyBulk("command", { shell: "powershell", command_text: cmd.trim(), timeout_seconds: 120 },
              "Exécuter cette commande sur " + n + " poste(s) ?\n\n" + cmd.trim());
          }
        }
      });
    });

    var clear = document.getElementById("bulk-clear");
    if (clear) clear.addEventListener("click", function () { selected = {}; render(lastData); });

    var all = document.getElementById("cb-all");
    if (all) {
      all.addEventListener("change", function () {
        var visible = Array.prototype.slice.call(document.querySelectorAll(".row-cb"));
        visible.forEach(function (c) {
          c.checked = all.checked;
          var id = c.getAttribute("data-id");
          if (all.checked) selected[id] = true; else delete selected[id];
        });
        updateBulkBar();
      });
    }
  }

  // --- Renommer / supprimer un poste (admin) ---
  async function renameAgent(id, current) {
    var name = window.prompt("Nom convivial du poste (laisser vide pour utiliser le nom d'hôte) :", current);
    if (name === null) return;
    try {
      var r = await fetch("/api/v1/agents/" + id + "/name", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ name: name.trim() }),
      });
      if (!r.ok) { var d = await r.json().catch(function () { return {}; }); window.alert(d.error || "Échec du renommage."); }
    } catch (e) { window.alert("Erreur réseau."); }
    load();
  }

  async function deleteAgent(id, name) {
    if (!window.confirm("Supprimer définitivement le poste « " + name + " » du parc ?\n" +
        "Toutes ses données (inventaire, métriques, historique) seront effacées.\n" +
        "À utiliser après désinstallation de l'agent sur le poste.")) return;
    try {
      var r = await fetch("/api/v1/agents/" + id, { method: "DELETE", headers: { Accept: "application/json" } });
      if (!r.ok) { var d = await r.json().catch(function () { return {}; }); window.alert(d.error || "Échec de la suppression."); }
    } catch (e) { window.alert("Erreur réseau."); }
    delete selected[id];
    load();
  }

  // --- Emplacements : remplit le filtre + le sélecteur d'affectation groupée ---
  async function loadSites() {
    try {
      var r = await fetch("/api/v1/sites", { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      var sites = await r.json();
      sitesById = {};
      var realSites = sites.filter(function (s) { return s.id; });
      realSites.forEach(function (s) { sitesById[s.id] = { name: s.name, color: s.color }; });

      var opts = realSites.map(function (s) {
        return '<option value="' + esc(s.id) + '">' + esc(s.name) + "</option>";
      }).join("");

      var filterSel = document.getElementById("site-filter");
      if (filterSel) {
        filterSel.innerHTML = '<option value="">Tous les emplacements</option>' + opts +
          '<option value="none">Non assigné</option>';
        filterSel.value = siteParam || "";
      }
      var bulkSel = document.getElementById("bulk-site");
      if (bulkSel) {
        bulkSel.innerHTML = '<option value="">Emplacement…</option>' + opts +
          '<option value="none">— Désaffecter</option>';
      }
      applyBanner();
    } catch (e) { /* non bloquant */ }
  }

  function applyBanner() {
    var banner = document.getElementById("filter-banner");
    var txt = document.getElementById("filter-banner-text");
    if (!banner || !txt) return;
    var parts = [];
    if (siteParam) {
      var nm = siteParam === "none" ? "Non assigné"
        : (sitesById[siteParam] ? sitesById[siteParam].name : "emplacement");
      parts.push("Emplacement : " + nm);
    }
    if (healthParam && HEALTH_LABEL[healthParam]) parts.push("Santé : " + HEALTH_LABEL[healthParam]);
    if (securityParam === "updates") parts.push("MAJ Windows en attente");
    else if (securityParam === "defender") parts.push("Antivirus désactivé");
    if (!parts.length) { banner.classList.add("hidden"); return; }
    txt.textContent = parts.join("  ·  ");
    banner.classList.remove("hidden");
  }

  function setupSitesUI() {
    // Le bouton CSV reprend les filtres courants de l'URL (site/santé/sécurité).
    var exp = document.getElementById("export-csv");
    if (exp) exp.href = "/api/v1/agents/export.csv" + window.location.search;

    var filterSel = document.getElementById("site-filter");
    if (filterSel) {
      filterSel.addEventListener("change", function () {
        var v = filterSel.value;
        window.location.href = v ? "/agents?site=" + encodeURIComponent(v) : "/agents";
      });
    }
    var apply = document.getElementById("bulk-site-apply");
    if (apply) {
      apply.addEventListener("click", async function () {
        var sel = document.getElementById("bulk-site");
        var v = sel ? sel.value : "";
        var ids = selectedIds();
        if (!ids.length) return;
        if (v === "") { window.alert("Choisissez un emplacement à affecter."); return; }
        try {
          var r = await fetch("/api/v1/agents/bulk-site", {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify({ agent_ids: ids, site_id: v === "none" ? null : v }),
          });
          var d = await r.json().catch(function () { return {}; });
          if (!r.ok) { window.alert(d.error || "Échec de l'affectation."); return; }
          selected = {};
          load();
        } catch (e) { window.alert("Erreur réseau."); }
      });
    }
  }

  async function load() {
    try {
      var qs = [];
      if (siteParam) qs.push("site=" + encodeURIComponent(siteParam));
      if (healthParam) qs.push("health=" + encodeURIComponent(healthParam));
      if (securityParam) qs.push("security=" + encodeURIComponent(securityParam));
      var url = "/api/v1/agents" + (qs.length ? "?" + qs.join("&") : "");
      var resp = await fetch(url, { headers: { Accept: "application/json" } });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      lastData = await resp.json();
      render(lastData);
    } catch (e) {
      console.error("Échec du chargement des agents :", e);
    }
  }

  // Recherche : la barre globale (topbar) pilote le filtre caché #filter.
  var hiddenFilter = document.getElementById("filter");
  var globalSearch = document.getElementById("global-search");
  if (globalSearch && hiddenFilter) {
    globalSearch.addEventListener("input", function () {
      hiddenFilter.value = globalSearch.value;
      render(lastData);
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== globalSearch &&
        !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "")) {
      if (globalSearch) { e.preventDefault(); globalSearch.focus(); }
    }
  });

  // Filtre d'état (Tous / En ligne / Hors ligne / Alerte) — pour tous les rôles.
  var fleetFilter = document.getElementById("fleet-filter");
  if (fleetFilter) {
    Array.prototype.forEach.call(fleetFilter.querySelectorAll(".seg-btn"), function (btn) {
      btn.addEventListener("click", function () {
        statusFilter = btn.getAttribute("data-st") || "all";
        Array.prototype.forEach.call(fleetFilter.querySelectorAll(".seg-btn"), function (b) {
          b.classList.toggle("on", b === btn);
        });
        render(lastData);
      });
    });
  }

  if (IS_ADMIN) setupBulk();
  setupSitesUI();
  loadSites();
  load();
  setInterval(load, REFRESH_MS);
})();
