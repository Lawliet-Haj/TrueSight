// TrueSight — page Réglages : changement de mot de passe + activation/désactivation MFA.
// API : POST /api/v1/settings/password, GET /api/v1/settings/mfa,
//        POST /api/v1/settings/mfa/{setup,enable,disable}.
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function setMsg(el, text, ok) {
    if (!el) return;
    el.textContent = text;
    el.className = "form-msg " + (ok ? "ok" : "err");
  }

  function clearMsg(el) {
    if (!el) return;
    el.textContent = "";
    el.className = "form-msg";
  }

  async function jsonOf(resp) {
    try { return await resp.json(); } catch (_) { return {}; }
  }

  // ---- Changement de mot de passe ----
  var pwForm = $("pw-form");
  if (pwForm) {
    pwForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var m = $("pw-msg");
      clearMsg(m);
      var cur = $("pw-current").value;
      var nw = $("pw-new").value;
      var cf = $("pw-confirm").value;

      if (nw.length < 8) { setMsg(m, "Le nouveau mot de passe doit faire au moins 8 caractères.", false); return; }
      if (nw !== cf) { setMsg(m, "La confirmation ne correspond pas.", false); return; }

      try {
        var r = await postJSON("/api/v1/settings/password", { current_password: cur, new_password: nw });
        if (r.status === 401) { setMsg(m, "Mot de passe actuel incorrect.", false); return; }
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Échec du changement.", false); return; }
        setMsg(m, "Mot de passe mis à jour.", true);
        pwForm.reset();
      } catch (_) {
        setMsg(m, "Erreur réseau.", false);
      }
    });
  }

  // ---- MFA ----
  var pill = $("mfa-pill");
  var pillText = $("mfa-pill-text");
  var tag = $("mfa-tag");
  var help = $("mfa-help");
  var disabledBlock = $("mfa-disabled-block");
  var enabledBlock = $("mfa-enabled-block");
  var setup = $("mfa-setup");

  function renderState(enabled) {
    if (enabled) {
      pill.className = "pill on";
      pillText.textContent = "Activé";
      if (tag) tag.textContent = "actif";
      help.textContent = "La double authentification est active : un code sera demandé à chaque connexion.";
      enabledBlock.classList.remove("hidden");
      disabledBlock.classList.add("hidden");
    } else {
      pill.className = "pill off";
      pillText.textContent = "Désactivé";
      if (tag) tag.textContent = "inactif";
      help.textContent = "Ajoutez une étape de vérification via une application d'authentification (Google Authenticator, Authy, Microsoft Authenticator…).";
      disabledBlock.classList.remove("hidden");
      enabledBlock.classList.add("hidden");
      setup.classList.add("hidden");
    }
  }

  async function loadStatus() {
    try {
      var r = await fetch("/api/v1/settings/mfa", { headers: { Accept: "application/json" } });
      if (r.status === 401) { window.location.href = "/login"; return; }
      var d = await jsonOf(r);
      renderState(!!d.enabled);
    } catch (_) {
      // En cas d'échec réseau, on propose au moins l'activation.
      renderState(false);
    }
  }

  var startBtn = $("mfa-start");
  if (startBtn) {
    startBtn.addEventListener("click", async function () {
      var m = $("mfa-enable-msg");
      clearMsg(m);
      try {
        var r = await postJSON("/api/v1/settings/mfa/setup", {});
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Échec de l'initialisation.", false); return; }
        $("mfa-qr-img").src = d.qr_png_base64;
        $("mfa-secret").textContent = d.secret;
        setup.classList.remove("hidden");
        $("mfa-code").focus();
      } catch (_) {
        setMsg(m, "Erreur réseau.", false);
      }
    });
  }

  var enableForm = $("mfa-enable-form");
  if (enableForm) {
    enableForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var m = $("mfa-enable-msg");
      clearMsg(m);
      var code = ($("mfa-code").value || "").replace(/\s/g, "");
      try {
        var r = await postJSON("/api/v1/settings/mfa/enable", { code: code });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Code invalide.", false); return; }
        enableForm.reset();
        renderState(true);
      } catch (_) {
        setMsg(m, "Erreur réseau.", false);
      }
    });
  }

  var disableForm = $("mfa-disable-form");
  if (disableForm) {
    disableForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var m = $("mfa-disable-msg");
      clearMsg(m);
      var pw = $("mfa-pw").value;
      try {
        var r = await postJSON("/api/v1/settings/mfa/disable", { password: pw });
        if (r.status === 401) { setMsg(m, "Mot de passe incorrect.", false); return; }
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Échec.", false); return; }
        disableForm.reset();
        renderState(false);
      } catch (_) {
        setMsg(m, "Erreur réseau.", false);
      }
    });
  }

  loadStatus();

  // ---- Ordre des onglets de la fiche poste (admin) ----
  function setupTabOrder() {
    var list = $("tab-order-list");
    if (!list) return;  // section absente (viewer)
    var msg = $("tab-order-msg");
    var catalog = [];  // [{key, label}] depuis le serveur

    function rowHtml(key, label) {
      return '<li class="tab-order-row" data-key="' + key + '" draggable="true">' +
        '<span class="to-handle" aria-hidden="true" title="Glisser pour réordonner">⠿</span>' +
        '<span class="to-label">' + label + "</span>" +
        '<span class="grow"></span>' +
        '<button type="button" class="btn xs to-move" data-dir="up" title="Monter" aria-label="Monter">↑</button>' +
        '<button type="button" class="btn xs to-move" data-dir="down" title="Descendre" aria-label="Descendre">↓</button>' +
        "</li>";
    }

    function render(order) {
      var byKey = {};
      catalog.forEach(function (t) { byKey[t.key] = t.label; });
      var keys = [];
      (order || []).forEach(function (k) { if (byKey[k] && keys.indexOf(k) === -1) keys.push(k); });
      catalog.forEach(function (t) { if (keys.indexOf(t.key) === -1) keys.push(t.key); });
      list.innerHTML = keys.map(function (k) { return rowHtml(k, byKey[k]); }).join("");
    }

    function currentOrder() {
      return Array.prototype.map.call(list.querySelectorAll(".tab-order-row"), function (li) {
        return li.getAttribute("data-key");
      });
    }

    // Réordonnancement par les flèches ↑/↓ (accessible / repli tactile).
    list.addEventListener("click", function (e) {
      var btn = e.target.closest(".to-move");
      if (!btn) return;
      var row = btn.closest(".tab-order-row");
      if (!row) return;
      if (btn.getAttribute("data-dir") === "up") {
        if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
      } else if (row.nextElementSibling) {
        row.parentNode.insertBefore(row.nextElementSibling, row);
      }
    });

    // Réordonnancement par glisser-déposer (HTML5 drag & drop).
    var dragRow = null;
    function rowAfterPoint(y) {
      var rows = Array.prototype.slice.call(list.querySelectorAll(".tab-order-row:not(.dragging)"));
      var closest = null, closestOffset = Number.NEGATIVE_INFINITY;
      rows.forEach(function (row) {
        var box = row.getBoundingClientRect();
        var offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closestOffset) { closestOffset = offset; closest = row; }
      });
      return closest;
    }
    list.addEventListener("dragstart", function (e) {
      var row = e.target.closest(".tab-order-row");
      if (!row) return;
      dragRow = row;
      row.classList.add("dragging");
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", row.getAttribute("data-key")); } catch (_) {}
      }
    });
    list.addEventListener("dragend", function () {
      if (dragRow) dragRow.classList.remove("dragging");
      dragRow = null;
    });
    list.addEventListener("dragover", function (e) {
      if (!dragRow) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      var after = rowAfterPoint(e.clientY);
      if (after == null) list.appendChild(dragRow);
      else if (after !== dragRow) list.insertBefore(dragRow, after);
    });
    list.addEventListener("drop", function (e) { e.preventDefault(); });

    var saveBtn = $("tab-order-save");
    if (saveBtn) saveBtn.addEventListener("click", async function () {
      clearMsg(msg);
      try {
        var r = await postJSON("/api/v1/settings/tab-order", { order: currentOrder() });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(msg, d.error || "Échec.", false); return; }
        setMsg(msg, "Ordre enregistré. Il s'applique à l'ouverture d'une fiche poste.", true);
      } catch (_) { setMsg(msg, "Erreur réseau.", false); }
    });

    var resetBtn = $("tab-order-reset");
    if (resetBtn) resetBtn.addEventListener("click", async function () {
      clearMsg(msg);
      try {
        var r = await postJSON("/api/v1/settings/tab-order", { order: [] });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(msg, d.error || "Échec.", false); return; }
        render([]);  // ordre par défaut (catalogue)
        setMsg(msg, "Ordre réinitialisé (par défaut).", true);
      } catch (_) { setMsg(msg, "Erreur réseau.", false); }
    });

    fetch("/api/v1/settings/preferences", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
      .then(function (d) {
        catalog = Array.isArray(d.tabs) ? d.tabs : [];
        render(d.tab_order || []);
      })
      .catch(function () { list.innerHTML = '<li class="dl-loading err-cell">Erreur de chargement.</li>'; });
  }

  setupTabOrder();

  // ---- Services supervisés (admin) ----
  function setupServiceWatches() {
    var body = $("sw-body");
    if (!body) return;
    var form = $("sw-form");
    var msg = $("sw-msg");
    var scopeSel = $("sw-scope");
    var scopeValField = $("sw-scopeval-field");

    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    }
    function scopeLabel(s, v) {
      if (s === "site") return "Emplacement : " + (v || "?");
      if (s === "tag") return "Étiquette : " + (v || "?");
      return "Tous les postes";
    }
    function render(rows) {
      if (!rows.length) { body.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun service surveillé.</td></tr>'; return; }
      body.innerHTML = rows.map(function (w) {
        return "<tr>" +
          '<td class="mono">' + esc(w.service_name) + "</td>" +
          "<td>" + esc(scopeLabel(w.scope, w.scope_value)) + "</td>" +
          '<td><button type="button" class="btn xs sw-auto' + (w.auto_restart ? " go" : "") + '" data-id="' + w.id + '" data-on="' + (w.auto_restart ? "1" : "0") + '">' + (w.auto_restart ? "Activé" : "Désactivé") + "</button></td>" +
          "<td>" + (w.is_active ? '<span class="badge ok">actif</span>' : '<span class="badge off">inactif</span>') + "</td>" +
          '<td class="num"><button type="button" class="btn xs danger sw-del" data-id="' + w.id + '"><svg><use href="#i-x"/></svg>Suppr.</button></td>' +
          "</tr>";
      }).join("");
    }
    async function load() {
      try {
        var r = await fetch("/api/v1/service-watches", { headers: { Accept: "application/json" } });
        if (r.status === 401) { window.location.href = "/login"; return; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        render(await r.json());
      } catch (_) {
        body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">Erreur de chargement.</td></tr>';
      }
    }

    if (scopeSel) scopeSel.addEventListener("change", function () {
      scopeValField.classList.toggle("hidden", scopeSel.value === "global");
    });
    if (form) form.addEventListener("submit", async function (e) {
      e.preventDefault();
      clearMsg(msg);
      var name = $("sw-name").value.trim();
      if (!name) { setMsg(msg, "Nom de service requis.", false); return; }
      var payload = { service_name: name, scope: scopeSel.value, auto_restart: $("sw-auto").checked };
      if (scopeSel.value !== "global") payload.scope_value = $("sw-scopeval").value.trim();
      try {
        var r = await postJSON("/api/v1/service-watches", payload);
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(msg, d.error || "Échec.", false); return; }
        form.reset();
        scopeValField.classList.add("hidden");
        setMsg(msg, "Service ajouté à la supervision.", true);
        load();
      } catch (_) { setMsg(msg, "Erreur réseau.", false); }
    });
    body.addEventListener("click", async function (e) {
      var auto = e.target.closest(".sw-auto");
      var del = e.target.closest(".sw-del");
      if (auto) {
        var on = auto.getAttribute("data-on") === "1";
        try {
          await fetch("/api/v1/service-watches/" + auto.getAttribute("data-id"), {
            method: "PATCH", headers: { "Content-Type": "application/json", Accept: "application/json" },
            body: JSON.stringify({ auto_restart: !on }),
          });
        } catch (_) { /* ignore */ }
        load();
      } else if (del) {
        var ask = await TS.confirm({
          title: "Retirer ce service de la supervision ?",
          body: "Plus d'alerte ni de redémarrage automatique pour ce service.",
          danger: true, confirmLabel: "Retirer",
        });
        if (!ask.confirmed) return;
        try {
          await fetch("/api/v1/service-watches/" + del.getAttribute("data-id"),
                      { method: "DELETE", headers: { Accept: "application/json" } });
        } catch (_) { /* ignore */ }
        load();
      }
    });
    load();
  }
  setupServiceWatches();
})();
