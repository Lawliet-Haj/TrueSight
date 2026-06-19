// TrueSight — Réglages › Gestion des accès (super-administrateur uniquement).
// API : GET/POST /api/v1/users, POST /users/<id>/{role,active,reset-password},
//        DELETE /users/<id>. Les garde-fous anti-verrouillage sont côté serveur ;
// l'UI désactive simplement les actions destructrices sur le compte courant.
(function () {
  "use strict";

  var body = document.getElementById("users-body");
  if (!body) return;
  var createForm = document.getElementById("user-create");

  var ROLE_LABEL = { viewer: "Lecture seule", admin: "Administrateur", superadmin: "Super-administrateur" };

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function setMsg(el, text, ok) {
    if (!el) return;
    el.textContent = text;
    el.className = (el.className.indexOf("pw-msg") !== -1 ? "form-msg pw-msg " : "form-msg ") + (ok ? "ok" : "err");
  }
  function clearMsg(el) {
    if (!el) return;
    el.textContent = "";
    el.className = el.className.indexOf("pw-msg") !== -1 ? "form-msg pw-msg" : "form-msg";
  }

  function postJSON(url, b) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(b || {}),
    });
  }
  async function jsonOf(resp) {
    try { return await resp.json(); } catch (_) { return {}; }
  }

  function roleOptions(selected) {
    return Object.keys(ROLE_LABEL).map(function (r) {
      return '<option value="' + r + '"' + (r === selected ? " selected" : "") + ">" + ROLE_LABEL[r] + "</option>";
    }).join("");
  }

  function render(users) {
    var count = document.getElementById("users-count");
    if (count) count.textContent = users.length + (users.length === 1 ? " accès" : " accès");

    if (!users.length) {
      body.innerHTML = '<tr><td colspan="5" class="empty-cell">Aucun compte.</td></tr>';
      return;
    }

    body.innerHTML = users.map(function (u) {
      var self = u.is_self === true;
      var selfTag = self ? ' <span class="chip tag">vous</span>' : "";
      var mfa = u.mfa_enabled
        ? '<span class="badge ok">activé</span>'
        : '<span class="badge off">non</span>';

      var stateBtn =
        '<button type="button" class="btn xs ' + (u.is_active ? "" : "danger") + '" data-action="active" ' +
        'data-id="' + esc(u.id) + '" data-active="' + (u.is_active ? "0" : "1") + '"' +
        (self && u.is_active ? ' disabled title="Vous ne pouvez pas désactiver votre propre compte"' : "") +
        ">" + (u.is_active ? "Actif" : "Inactif") + "</button>";

      var roleSel =
        '<select class="input role-sel" data-action="role" data-id="' + esc(u.id) + '">' +
        roleOptions(u.role) + "</select>";

      var pwBtn = '<button type="button" class="btn xs" data-action="pw" data-id="' + esc(u.id) + '">MdP</button>';
      var delBtn =
        '<button type="button" class="btn xs danger" data-action="delete" data-id="' + esc(u.id) + '"' +
        (self ? ' disabled title="Compte courant"' : "") + ">Suppr.</button>";

      var main =
        "<tr>" +
        '<td><div class="host"><span class="dot ' + (u.is_active ? "on" : "off") + '"></span>' +
          '<div class="nm">' + esc(u.email) + selfTag + "</div></div></td>" +
        "<td>" + roleSel + "</td>" +
        "<td>" + mfa + "</td>" +
        "<td>" + stateBtn + "</td>" +
        '<td><div class="act">' + pwBtn + delBtn + "</div></td>" +
        "</tr>";

      var pwRow =
        '<tr class="pw-row hidden" data-pwrow="' + esc(u.id) + '"><td colspan="5">' +
        '<div class="pw-inline">' +
          '<span class="field-label">Nouveau mot de passe — ' + esc(u.email) + "</span>" +
          '<input type="password" class="input pw-new" minlength="8" autocomplete="new-password" placeholder="≥ 8 caractères">' +
          '<button type="button" class="btn go xs" data-action="pw-set" data-id="' + esc(u.id) + '">Définir</button>' +
          '<span class="form-msg pw-msg"></span>' +
        "</div></td></tr>";

      return main + pwRow;
    }).join("");
  }

  async function load() {
    try {
      var r = await fetch("/api/v1/users", { headers: { Accept: "application/json" } });
      if (r.status === 401) { window.location.href = "/login"; return; }
      if (r.status === 403) {
        body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">Accès réservé au super-administrateur.</td></tr>';
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      render(await r.json());
    } catch (_) {
      body.innerHTML = '<tr><td colspan="5" class="empty-cell err-cell">Erreur de chargement.</td></tr>';
    }
  }

  // Action générique POST/DELETE puis rechargement ; alerte en cas de refus serveur.
  async function act(url, payload, method) {
    try {
      var r = method === "DELETE"
        ? await fetch(url, { method: "DELETE", headers: { Accept: "application/json" } })
        : await postJSON(url, payload || {});
      if (r.status === 401) { window.location.href = "/login"; return; }
      var d = await jsonOf(r);
      if (!r.ok) TS.toast(d.error || "Action refusée.", "error");
    } catch (_) {
      TS.toast("Erreur réseau.", "error");
    }
    load();
  }

  // Création d'un accès.
  if (createForm) {
    createForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var m = document.getElementById("uc-msg");
      clearMsg(m);
      var email = document.getElementById("nu-email").value.trim();
      var pw = document.getElementById("nu-pw").value;
      var role = document.getElementById("nu-role").value;
      if (pw.length < 8) { setMsg(m, "Mot de passe ≥ 8 caractères.", false); return; }
      try {
        var r = await postJSON("/api/v1/users", { email: email, password: pw, role: role });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Échec de la création.", false); return; }
        createForm.reset();
        setMsg(m, "Accès « " + email + " » créé.", true);
        load();
      } catch (_) {
        setMsg(m, "Erreur réseau.", false);
      }
    });
  }

  // Délégation des clics (boutons d'action de chaque ligne).
  body.addEventListener("click", async function (e) {
    var btn = e.target.closest("[data-action]");
    if (!btn || btn.tagName === "SELECT") return;
    var action = btn.getAttribute("data-action");
    var id = btn.getAttribute("data-id");

    if (action === "pw") {
      var row = body.querySelector('[data-pwrow="' + id + '"]');
      if (row) row.classList.toggle("hidden");
      return;
    }
    if (action === "active") {
      await act("/api/v1/users/" + id + "/active", { active: btn.getAttribute("data-active") === "1" });
      return;
    }
    if (action === "delete") {
      var ask = await TS.confirm({
        title: "Supprimer ce compte d'accès ?",
        body: "Le compte ne pourra plus se connecter au dashboard.",
        danger: true, confirmLabel: "Supprimer",
      });
      if (!ask.confirmed) return;
      await act("/api/v1/users/" + id, null, "DELETE");
      return;
    }
    if (action === "pw-set") {
      var tr = btn.closest("tr");
      var input = tr.querySelector(".pw-new");
      var msg = tr.querySelector(".pw-msg");
      clearMsg(msg);
      var val = input.value;
      if (val.length < 8) { setMsg(msg, "≥ 8 caractères.", false); return; }
      try {
        var r = await postJSON("/api/v1/users/" + id + "/reset-password", { new_password: val });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(msg, d.error || "Échec.", false); return; }
        setMsg(msg, "Mot de passe défini.", true);
        input.value = "";
      } catch (_) {
        setMsg(msg, "Erreur réseau.", false);
      }
    }
  });

  // Délégation du changement de rôle (select).
  body.addEventListener("change", async function (e) {
    var sel = e.target.closest('select[data-action="role"]');
    if (!sel) return;
    await act("/api/v1/users/" + sel.getAttribute("data-id") + "/role", { role: sel.value });
  });

  load();
})();
