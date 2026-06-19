// TrueSight — primitives d'interface partagées (window.TS).
// Remplace les boîtes natives window.confirm / window.alert par des composants
// au thème de l'app : modale de confirmation (promesse) + toasts.
// Chargé par base.html AVANT les scripts de page → window.TS dispo partout.
(function () {
  "use strict";
  if (window.TS) return;

  // ---- Toasts -------------------------------------------------------------
  function container() {
    var c = document.getElementById("ts-toasts");
    if (!c) {
      c = document.createElement("div");
      c.id = "ts-toasts";
      c.className = "ts-toasts";
      c.setAttribute("aria-live", "polite");
      document.body.appendChild(c);
    }
    return c;
  }

  // type : "success" | "error" | "info" (défaut info)
  function toast(message, type) {
    var el = document.createElement("div");
    el.className = "ts-toast " + (type === "success" ? "ok" : type === "error" ? "err" : "info");
    el.setAttribute("role", "status");
    el.textContent = String(message == null ? "" : message);
    container().appendChild(el);
    requestAnimationFrame(function () { el.classList.add("show"); });
    var ttl = type === "error" ? 6000 : 3600;
    var timer = setTimeout(close, ttl);
    function close() {
      clearTimeout(timer);
      el.classList.remove("show");
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }
    el.addEventListener("click", close);
    return el;
  }

  // ---- Modale de confirmation --------------------------------------------
  // opts : {title, body, confirmLabel, cancelLabel, danger, checkbox}
  // Résout TOUJOURS un objet {confirmed:bool, checked:bool}.
  function confirm(opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var lastFocus = document.activeElement;

      var overlay = document.createElement("div");
      overlay.className = "ts-modal-overlay";

      var modal = document.createElement("div");
      modal.className = "ts-modal" + (opts.danger ? " danger" : "");
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");

      var title = document.createElement("div");
      title.className = "ts-modal-title";
      title.textContent = opts.title || "Confirmer";
      modal.appendChild(title);

      if (opts.body) {
        var body = document.createElement("div");
        body.className = "ts-modal-body";
        body.textContent = opts.body;  // textContent : pas d'injection HTML
        modal.appendChild(body);
      }

      var checkInput = null;
      if (opts.checkbox) {
        var lab = document.createElement("label");
        lab.className = "ts-modal-check";
        checkInput = document.createElement("input");
        checkInput.type = "checkbox";
        var span = document.createElement("span");
        span.textContent = opts.checkbox;
        lab.appendChild(checkInput);
        lab.appendChild(span);
        modal.appendChild(lab);
      }

      var actions = document.createElement("div");
      actions.className = "ts-modal-actions";
      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn";
      cancel.textContent = opts.cancelLabel || "Annuler";
      var ok = document.createElement("button");
      ok.type = "button";
      ok.className = "btn " + (opts.danger ? "danger-solid" : "go");
      ok.textContent = opts.confirmLabel || "Confirmer";
      actions.appendChild(cancel);
      actions.appendChild(ok);
      modal.appendChild(actions);

      overlay.appendChild(modal);
      document.body.appendChild(overlay);
      requestAnimationFrame(function () { overlay.classList.add("show"); });
      ok.focus();

      function done(confirmed) {
        document.removeEventListener("keydown", onKey, true);
        overlay.classList.remove("show");
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 180);
        if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
        resolve({ confirmed: !!confirmed, checked: !!(checkInput && checkInput.checked) });
      }
      function onKey(e) {
        if (e.key === "Escape") { e.preventDefault(); done(false); }
        else if (e.key === "Enter") { e.preventDefault(); done(true); }
      }
      cancel.addEventListener("click", function () { done(false); });
      ok.addEventListener("click", function () { done(true); });
      overlay.addEventListener("mousedown", function (e) { if (e.target === overlay) done(false); });
      document.addEventListener("keydown", onKey, true);
    });
  }

  window.TS = { toast: toast, confirm: confirm };
})();
