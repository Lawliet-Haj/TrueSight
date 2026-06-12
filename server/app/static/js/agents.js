// TrueSight — page Parc : KPIs instrumentés + tableau du parc, rafraîchi toutes les 10 s.
// Données : GET /api/v1/agents → [{id,hostname,os_version,status,last_seen_at,cpu_pct,ram_used_pct,tags,is_active}]
(function () {
  "use strict";

  var REFRESH_MS = 10000;
  // Seuils d'alerte (cohérents avec la charte : ambre 60–84, rouge ≥85).
  var CPU_ALERT = 85;
  var RAM_ALERT = 85;
  var lastData = [];

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Formate une date ISO UTC en heure locale lisible.
  function fmtDate(iso) {
    if (!iso) return "jamais";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return esc(iso);
    return d.toLocaleString("fr-FR");
  }

  // Temps relatif court ("il y a 2 min").
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

  // Couleur d'une valeur selon le seuil (vert <60, ambre 60–84, rouge ≥85).
  function gaugeColor(v) {
    if (v >= 85) return "var(--danger)";
    if (v >= 60) return "var(--warn)";
    return "var(--ok)";
  }

  // Jauge CPU/RAM : track + remplissage coloré + valeur mono. "off" → tiret.
  function gauge(pct, off) {
    if (off) return '<span class="seen">—</span>';
    if (pct === null || pct === undefined) return '<span class="seen">—</span>';
    var v = Math.max(0, Math.min(100, pct));
    return (
      '<div class="gauge">' +
      '<div class="track"><div class="fill" style="width:' + v + "%;background:" + gaugeColor(v) + '"></div></div>' +
      '<span class="pc">' + v.toFixed(0) + "%</span>" +
      "</div>"
    );
  }

  // Détermine l'état d'affichage : online / alert (online mais surchargé) / offline.
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

  // Met à jour les 4 KPIs en haut de page.
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
      if (total === 0) {
        availEl.innerHTML = "—<small>%</small>";
      } else {
        var pct = (online / total) * 100;
        availEl.innerHTML = pct.toFixed(1).replace(".", ",") + '<small>%</small>';
      }
    }

    setText("kpi-online-sub", online === 1 ? "poste joignable" : "postes joignables");
    setText("kpi-offline-sub", "sans heartbeat récent");
    setText("kpi-alert-sub", alert === 0 ? "aucune surcharge" : (alert === 1 ? "1 poste en surcharge" : alert + " postes en surcharge"));
    setText("kpi-avail-sub", total + (total === 1 ? " poste supervisé" : " postes supervisés"));
    setText("fleet-count", total + (total === 1 ? " poste" : " postes"));

    // Télémétrie de la barre d'état (si présente).
    var sbAgents = document.getElementById("sb-agents");
    if (sbAgents) sbAgents.innerHTML = "agents&nbsp;<b>" + online + "/" + total + "</b>&nbsp;reportent";
  }

  function render(rows) {
    var body = document.getElementById("agents-body");
    if (!body) return;
    var filterEl = document.getElementById("filter");
    var filter = ((filterEl && filterEl.value) || "").toLowerCase();

    renderKpis(rows);

    var filtered = rows.filter(function (r) {
      if (!filter) return true;
      var hay = [r.hostname, r.os_version, (r.tags || []).join(" ")].join(" ").toLowerCase();
      return hay.indexOf(filter) !== -1;
    });

    if (!filtered.length) {
      body.innerHTML = '<tr><td colspan="7" class="empty-cell">Aucun poste</td></tr>';
      return;
    }

    body.innerHTML = filtered
      .map(function (r) {
        var st = rowState(r);
        var off = st === "off";
        var stateTitle = st === "on" ? "En ligne" : st === "alert" ? "En alerte" : "Hors ligne";

        var tags = (r.tags || [])
          .map(function (t) { return '<span class="chip tag">' + esc(t) + "</span>"; })
          .join("");
        var revoked = r.is_active === false ? '<span class="revoked">révoqué</span>' : "";

        var remoteDisabled = off ? "disabled" : "";

        return (
          '<tr class="clickable" data-href="/agents/' + esc(r.id) + '">' +
          '<td><div class="host"><span class="dot ' + st + '" title="' + stateTitle + '"></span>' +
            '<div><div class="nm">' + esc(r.hostname || r.id) + revoked + "</div>" +
            '<div class="us">' + stateTitle + "</div></div></div></td>" +
          '<td class="text-dim">' + esc(r.os_version || "—") + "</td>" +
          "<td>" + gauge(r.cpu_pct, off) + "</td>" +
          "<td>" + gauge(r.ram_used_pct, off) + "</td>" +
          "<td>" + (tags || '<span class="muted">—</span>') + "</td>" +
          '<td><span class="seen" title="' + esc(fmtDate(r.last_seen_at)) + '">' + esc(fmtRelative(r.last_seen_at)) + "</span></td>" +
          '<td><div class="act">' +
            '<div class="btn-ico ' + remoteDisabled + '" data-act="remote" data-id="' + esc(r.id) + '" title="Bureau à distance"><svg><use href="#i-screen"/></svg></div>' +
            '<div class="btn-ico ' + remoteDisabled + '" data-act="cmd" data-id="' + esc(r.id) + '" title="Commande à distance"><svg><use href="#i-terminal"/></svg></div>' +
          "</div></td>" +
          "</tr>"
        );
      })
      .join("");

    // Lignes cliquables → fiche poste (sauf clic sur un bouton d'action).
    Array.prototype.forEach.call(body.querySelectorAll("tr.clickable"), function (tr) {
      tr.addEventListener("click", function () {
        window.location.href = tr.getAttribute("data-href");
      });
    });

    // Boutons d'action : ouvrent la fiche (où vivent la console + le bureau à distance).
    Array.prototype.forEach.call(body.querySelectorAll(".btn-ico"), function (b) {
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (b.classList.contains("disabled")) return;
        var id = b.getAttribute("data-id");
        var act = b.getAttribute("data-act");
        // Ancres : #remote ouvre directement la fenêtre bureau à distance, #console la commande.
        window.location.href = "/agents/" + id + (act === "remote" ? "#remote" : "#console");
      });
    });
  }

  async function load() {
    try {
      var resp = await fetch("/api/v1/agents", { headers: { Accept: "application/json" } });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      lastData = await resp.json();
      render(lastData);
    } catch (e) {
      // Erreur réseau ponctuelle : on conserve l'affichage précédent.
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

  // Raccourci "/" pour focaliser la recherche.
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== globalSearch &&
        !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "")) {
      if (globalSearch) { e.preventDefault(); globalSearch.focus(); }
    }
  });

  load();
  setInterval(load, REFRESH_MS);
})();
