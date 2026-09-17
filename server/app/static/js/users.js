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

  // Compte rendu d'une invitation. Le point important est le REPLI : si l'e-mail
  // ne part pas, le compte existe quand même et l'administrateur doit repartir
  // avec le lien — sinon la personne reste bloquée sans que personne ne le sache.
  function annonceInvitation(el, inv, qui) {
    if (!inv) { setMsg(el, "Invitation envoyée.", true); return; }
    if (inv.ok) {
      setMsg(el, "Invitation envoyée à " + qui + " — lien valable jusqu'au " +
                 (inv.expire_le || "…") + ".", true);
      return;
    }
    setMsg(el, "Compte prêt, mais l'e-mail n'est pas parti (" + (inv.erreur || "raison inconnue") +
               "). Transmettez ce lien à " + qui + " :", false);
    if (!inv.lien) return;
    var zone = document.createElement("div");
    zone.className = "invite-link";
    var champ = document.createElement("input");
    champ.type = "text";
    champ.className = "input mono";
    champ.readOnly = true;
    champ.value = inv.lien;
    champ.style.flex = "1";
    var copie = document.createElement("button");
    copie.type = "button";
    copie.className = "btn xs";
    copie.textContent = "Copier";
    copie.addEventListener("click", function () {
      champ.select();
      try { document.execCommand("copy"); } catch (_) { /* rien */ }
      if (navigator.clipboard) { navigator.clipboard.writeText(inv.lien).catch(function () {}); }
      copie.textContent = "Copié";
    });
    zone.appendChild(champ);
    zone.appendChild(copie);
    var ancien = el.parentNode.querySelector(".invite-link");
    if (ancien) ancien.remove();
    el.parentNode.appendChild(zone);
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
      body.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucun compte.</td></tr>';
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

      // Un compte « invité » existe mais personne ne s'y est encore connecté :
      // le dire évite de croire qu'il est opérationnel.
      var invite = u.invited
        ? ' <span class="chip tag" title="Invitation envoyée, mot de passe pas encore choisi">invité</span>'
        : "";
      var pwBtn =
        '<button type="button" class="btn xs" data-action="invite" data-id="' + esc(u.id) + '" ' +
        'title="Envoie un lien pour (re)choisir le mot de passe ; le lien precedent est invalide">' +
        (u.invited ? "Relancer" : "Réinitialiser") + "</button>";
      var delBtn =
        '<button type="button" class="btn xs danger" data-action="delete" data-id="' + esc(u.id) + '"' +
        (self ? ' disabled title="Compte courant"' : "") + ">Suppr.</button>";

      var main =
        "<tr>" +
        '<td><div class="host"><span class="dot ' + (u.is_active ? "on" : "off") + '"></span>' +
          '<div class="nm">' + esc(u.email) + selfTag + invite + "</div></div></td>" +
        "<td>" + esc(u.name || "—") + "</td>" +
        "<td>" + roleSel + "</td>" +
        "<td>" + mfa + "</td>" +
        "<td>" + stateBtn + "</td>" +
        '<td><div class="act">' + pwBtn + delBtn + "</div></td>" +
        "</tr>";

      return main;
    }).join("");
  }

  async function load() {
    try {
      var r = await fetch("/api/v1/users", { headers: { Accept: "application/json" } });
      if (r.status === 401) { window.location.href = "/login"; return; }
      if (r.status === 403) {
        body.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Accès réservé au super-administrateur.</td></tr>';
        return;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      render(await r.json());
    } catch (_) {
      body.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Erreur de chargement.</td></tr>';
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
      var name = document.getElementById("nu-name").value.trim();
      var role = document.getElementById("nu-role").value;
      if (!name) { setMsg(m, "Indiquez le nom de la personne.", false); return; }
      try {
        var r = await postJSON("/api/v1/users", { email: email, name: name, role: role });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Échec de l'invitation.", false); return; }
        createForm.reset();
        annonceInvitation(m, d.invitation, name + " (" + email + ")");
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

    if (action === "invite") {
      var ask = await TS.confirm({
        title: "Envoyer un lien d'accès ?",
        body: "La personne recevra un e-mail pour choisir son mot de passe. " +
              "Le mot de passe actuel et les sessions ouvertes sont invalidés.",
        confirmLabel: "Envoyer",
      });
      if (!ask.confirmed) return;
      try {
        var rp = await postJSON("/api/v1/users/" + id + "/reset-password", {});
        var dp = await jsonOf(rp);
        if (!rp.ok) { TS.toast(dp.error || "Échec.", "error"); return; }
        annonceInvitation(document.getElementById("uc-msg"), dp.invitation, "ce compte");
      } catch (_) {
        TS.toast("Erreur réseau.", "error");
      }
      load();
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

  });

  // Délégation du changement de rôle (select).
  body.addEventListener("change", async function (e) {
    var sel = e.target.closest('select[data-action="role"]');
    if (!sel) return;
    await act("/api/v1/users/" + sel.getAttribute("data-id") + "/role", { role: sel.value });
  });

  load();
})();
