// TrueSight — page Inventaire logiciel PAR POSTE.
// On choisit un poste (sélecteur), on liste SES logiciels (GET /agents/<id>/software),
// et (admin) on peut désinstaller en 1 clic via l'endpoint existant déjà audité.
// La barre de recherche globale filtre la liste affichée côté client.
(function () {
  "use strict";

  var data = document.getElementById("inv-data");
  var IS_ADMIN = !!data && data.getAttribute("data-is-admin") === "1";
  var SPAN = IS_ADMIN ? 5 : 4;

  var sel = document.getElementById("inv-agent");
  var body = document.getElementById("inventory-body");
  var countEl = document.getElementById("inventory-count");
  var openLink = document.getElementById("inv-open");
  var search = document.getElementById("global-search");
  var refresh = document.getElementById("refresh");
  if (!sel || !body) return;

  var software = [];
  var currentAgent = "";

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function setEmpty(msg, err) {
    body.innerHTML = '<tr><td colspan="' + SPAN + '" class="empty-cell' + (err ? " err-cell" : "") + '">' + esc(msg) + "</td></tr>";
  }

  function render() {
    var filter = ((search && search.value) || "").toLowerCase();
    var rows = software.filter(function (s) {
      if (!filter) return true;
      return [s.name, s.publisher, s.version].join(" ").toLowerCase().indexOf(filter) !== -1;
    });
    if (countEl) countEl.textContent = rows.length + (rows.length === 1 ? " logiciel" : " logiciels");
    if (!rows.length) { setEmpty(currentAgent ? "Aucun logiciel." : "Choisissez un poste ci-dessus."); return; }
    body.innerHTML = rows.map(function (s) {
      var action = "";
      if (IS_ADMIN) {
        action = '<td class="num"><button class="btn xs danger inv-uninstall" data-name="' +
          esc(s.name || "") + '"' + (s.name ? "" : " disabled") + ">Désinstaller</button></td>";
      }
      return "<tr>" +
        '<td class="mono">' + esc(s.name || "—") + "</td>" +
        '<td class="mono text-dim">' + esc(s.version || "—") + "</td>" +
        '<td class="text-dim">' + esc(s.publisher || "—") + "</td>" +
        '<td class="mono text-faint">' + esc(s.install_date || "—") + "</td>" +
        action +
        "</tr>";
    }).join("");
    if (IS_ADMIN) bindUninstall();
  }

  function selectAgent(agentId) {
    currentAgent = agentId || "";
    if (openLink) {
      if (currentAgent) { openLink.href = "/agents/" + currentAgent; openLink.classList.remove("hidden"); }
      else openLink.classList.add("hidden");
    }
    if (!currentAgent) { software = []; render(); return; }
    setEmpty("Chargement…");
    fetch("/api/v1/agents/" + currentAgent + "/software", { headers: { Accept: "application/json" } })
      .then(function (r) { if (r.status === 401) { window.location.href = "/login"; return null; } return r.ok ? r.json() : []; })
      .then(function (list) { if (list === null) return; software = Array.isArray(list) ? list : []; render(); })
      .catch(function () { setEmpty("Erreur de chargement", true); });
  }

  function bindUninstall() {
    Array.prototype.forEach.call(body.querySelectorAll(".inv-uninstall"), function (b) {
      b.addEventListener("click", function () {
        var name = b.getAttribute("data-name");
        if (!name || !currentAgent) return;
        TS.confirm({
          title: "Désinstaller « " + name + " » ?",
          body: "Désinstallation silencieuse sur ce poste (QuietUninstallString / MSI). Indisponible pour certains EXE.",
          danger: true, confirmLabel: "Désinstaller",
        }).then(function (ask) {
          if (!ask.confirmed) return;
          var prev = b.textContent;
          b.disabled = true; b.textContent = "envoi…";
          fetch("/api/v1/agents/" + currentAgent + "/software/uninstall", {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify({ source: "registry", name: name }),
          })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
              if (!res.ok) { b.disabled = false; b.textContent = prev; TS.toast(res.d.error || "Échec de la désinstallation.", "error"); return; }
              pollUninstall(res.d.command_id, b);
            })
            .catch(function () { b.disabled = false; b.textContent = prev; });
        });
      });
    });
  }

  function pollUninstall(commandId, btn) {
    var attempts = 0;
    btn.textContent = "en cours…";
    var timer = setInterval(function () {
      attempts++;
      fetch("/api/v1/commands/" + commandId, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          if (["done", "error", "timeout"].indexOf(d.status) !== -1) {
            clearInterval(timer);
            var code = d.result ? d.result.exit_code : null;
            // 3010 / 1641 = succès, redémarrage requis/initié (codes MSI usuels).
            var reboot = code === 3010 || code === 1641;
            var ok = code === 0 || reboot;
            btn.textContent = ok ? (reboot ? "désinstallé (redémarrage requis)" : "désinstallé") : "échec";
            if (ok) setTimeout(function () { selectAgent(currentAgent); }, 900);
          }
        })
        .catch(function () {});
      if (attempts > 150) clearInterval(timer);
    }, 2000);
  }

  function loadAgents() {
    fetch("/api/v1/agents", { headers: { Accept: "application/json" } })
      .then(function (r) { if (r.status === 401) { window.location.href = "/login"; return null; } return r.ok ? r.json() : []; })
      .then(function (list) {
        if (!list) return;
        list.sort(function (a, b) {
          return (a.name || a.hostname || "").localeCompare(b.name || b.hostname || "");
        });
        var html = '<option value="">Choisir un poste…</option>';
        list.forEach(function (a) {
          var label = (a.name || a.hostname || a.id) + (a.site_name ? " · " + a.site_name : "");
          html += '<option value="' + esc(a.id) + '">' + esc(label) + "</option>";
        });
        sel.innerHTML = html;
        // Pré-sélection via ?agent=<id> (lien direct / rafraîchissement).
        var pre = new URLSearchParams(window.location.search).get("agent");
        if (pre) { sel.value = pre; if (sel.value) selectAgent(sel.value); }
      })
      .catch(function () {});
  }

  sel.addEventListener("change", function () {
    var id = sel.value;
    var params = new URLSearchParams(window.location.search);
    if (id) params.set("agent", id); else params.delete("agent");
    var qs = params.toString();
    history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : ""));
    selectAgent(id);
  });

  if (search) {
    search.setAttribute("placeholder", "Filtrer les logiciels…");
    search.addEventListener("input", render);
  }
  if (refresh) refresh.addEventListener("click", function () {
    if (currentAgent) selectAgent(currentAgent); else loadAgents();
  });

  loadAgents();
})();
