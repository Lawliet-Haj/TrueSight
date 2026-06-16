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
})();
