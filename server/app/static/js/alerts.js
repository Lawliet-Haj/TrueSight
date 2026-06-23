// TrueSight — page Alertes : déclenchements de seuils sur le parc.
// Données : GET /api/v1/alerts?status=active|all →
//   [{id,agent_id,hostname,type,threshold,triggered_at,resolved_at,notified,active}]
// Le sélecteur segmenté (Actives / Toutes) pilote le paramètre `status`.
(function () {
  "use strict";

  var REFRESH_MS = 15000;
  var status = "active"; // état courant du filtre (cohérent avec le bouton .on initial)
  var timer = null;

  // Libellés + couleur de badge par type de règle (cf. alerts.py).
  var TYPE_LABEL = {
    offline: "Hors ligne",
    cpu_high: "CPU élevé",
    ram_high: "RAM élevée",
    disk_low: "Disque faible",
    service_down: "Service arrêté",
  };
  var TYPE_BADGE = {
    offline: "danger",
    cpu_high: "warn",
    ram_high: "warn",
    disk_low: "warn",
    service_down: "danger",
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

  function fmtRelative(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var secs = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (secs < 60) return "il y a " + secs + " s";
    if (secs < 3600) return "il y a " + Math.floor(secs / 60) + " min";
    if (secs < 86400) return "il y a " + Math.floor(secs / 3600) + " h";
    return "il y a " + Math.floor(secs / 86400) + " j";
  }

  // Formate le seuil de la règle selon son type (% ou durée).
  function fmtThreshold(type, thr) {
    if (thr === null || thr === undefined) return "—";
    if (type === "offline") {
      return thr >= 60 ? "> " + Math.round(thr / 60) + " min" : "> " + thr + " s";
    }
    if (type === "disk_low") return "≤ " + thr + " % libre";
    if (type === "cpu_high" || type === "ram_high") return "≥ " + thr + " %";
    return String(thr);
  }

  function render(rows) {
    var body = document.getElementById("alerts-body");
    if (!body) return;

    var countEl = document.getElementById("alerts-count");
    if (countEl) {
      var n = rows.length;
      countEl.textContent = n + (n === 1 ? " alerte" : " alertes") + (status === "active" ? " actives" : "");
    }

    if (!rows.length) {
      var none = status === "active" ? "Aucune alerte active — tout va bien." : "Aucune alerte enregistrée.";
      body.innerHTML = '<tr><td colspan="6" class="empty-cell">' + none + "</td></tr>";
      return;
    }

    body.innerHTML = rows
      .map(function (r) {
        var active = r.active === true;
        var label = TYPE_LABEL[r.type] || r.type || "—";
        var badge = TYPE_BADGE[r.type] || "warn";
        var clickable = r.agent_id
          ? ' class="clickable" data-href="/agents/' + esc(r.agent_id) + '"'
          : "";

        var resolvedCell = active
          ? '<span class="badge warn">active</span>'
          : '<span class="seen" title="' + esc(fmtDate(r.resolved_at)) + '">' + esc(fmtRelative(r.resolved_at)) + "</span>";

        var notifiedCell = r.notified
          ? '<span class="badge ok">n8n ✓</span>'
          : '<span class="badge off">non</span>';

        // Pour service_down : afficher les services concernés à la place du seuil.
        var detailCell;
        if (r.type === "service_down" && r.context && Array.isArray(r.context.services) && r.context.services.length) {
          detailCell = '<td class="mono text-dim">' + esc(r.context.services.join(", ")) + "</td>";
        } else {
          detailCell = '<td class="mono text-dim">' + esc(fmtThreshold(r.type, r.threshold)) + "</td>";
        }

        return (
          "<tr" + clickable + ">" +
          '<td><div class="host"><span class="dot ' + (active ? "alert" : "off") + '"></span>' +
            '<div class="nm">' + esc(r.hostname || "—") + "</div></div></td>" +
          '<td><span class="badge ' + badge + '">' + esc(label) + "</span></td>" +
          detailCell +
          '<td><span class="seen" title="' + esc(fmtDate(r.triggered_at)) + '">' + esc(fmtRelative(r.triggered_at)) + "</span></td>" +
          "<td>" + resolvedCell + "</td>" +
          "<td>" + notifiedCell + "</td>" +
          "</tr>"
        );
      })
      .join("");

    // Ligne cliquable → fiche du poste concerné.
    Array.prototype.forEach.call(body.querySelectorAll("tr.clickable"), function (tr) {
      tr.addEventListener("click", function () {
        window.location.href = tr.getAttribute("data-href");
      });
    });
  }

  async function load() {
    var body = document.getElementById("alerts-body");
    try {
      var resp = await fetch("/api/v1/alerts?status=" + encodeURIComponent(status), {
        headers: { Accept: "application/json" },
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      render(await resp.json());
    } catch (e) {
      if (body) body.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Erreur de chargement</td></tr>';
    }
  }

  // Sélecteur segmenté Actives / Toutes.
  Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (btn) {
    btn.addEventListener("click", function () {
      var next = btn.getAttribute("data-status");
      if (next === status) return;
      status = next === "all" ? "all" : "active";
      Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (b) {
        var on = b === btn;
        b.classList.toggle("on", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
      load();
    });
  });

  load();
  timer = setInterval(load, REFRESH_MS);
})();
