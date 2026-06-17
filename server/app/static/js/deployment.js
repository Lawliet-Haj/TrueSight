// TrueSight — Réglages › Déploiement & mises à jour.
// Liens d'installation (admin + superadmin) + versions de l'agent (superadmin).
// API : GET/POST /api/v1/install-tokens, DELETE /api/v1/install-tokens/<id>,
//        GET/POST /api/v1/agent-releases, POST .../current, DELETE .../<id>.
(function () {
  "use strict";

  var linksBody = document.getElementById("links-body");
  if (!linksBody) return; // panneau absent (rôle insuffisant)

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function setMsg(el, text, ok) {
    if (!el) return;
    el.textContent = text;
    el.className = "form-msg " + (ok ? "ok" : "err");
  }
  function clearMsg(el) { if (el) { el.textContent = ""; el.className = "form-msg"; } }
  async function jsonOf(r) { try { return await r.json(); } catch (_) { return {}; } }

  function fmtDate(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleDateString("fr-FR") + " " + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  }
  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return "—";
    var mb = bytes / (1024 * 1024);
    return (mb >= 1 ? mb.toFixed(1) + " Mo" : Math.round(bytes / 1024) + " Ko");
  }

  // ---------------------------------------------------------------- Liens
  var linkForm = document.getElementById("link-form");
  var linkResult = document.getElementById("link-result");
  var linkOneliner = document.getElementById("link-oneliner");

  function renderLinks(tokens) {
    if (!tokens.length) {
      linksBody.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucun lien généré.</td></tr>';
      return;
    }
    linksBody.innerHTML = tokens.map(function (t) {
      var state = t.revoked
        ? '<span class="badge off">révoqué</span>'
        : (t.active ? '<span class="badge ok">actif</span>' : '<span class="badge off">expiré</span>');
      var revoke = (!t.revoked && t.active)
        ? '<button type="button" class="btn xs danger" data-action="revoke-link" data-id="' + esc(t.id) + '">Révoquer</button>'
        : "";
      return "<tr>" +
        "<td>" + esc(t.label || "Lien d'installation") + "</td>" +
        "<td>" + fmtDate(t.created_at) + "</td>" +
        "<td>" + (t.expires_at ? fmtDate(t.expires_at) : "sans limite") + "</td>" +
        '<td class="num mono">' + (t.use_count || 0) + "</td>" +
        "<td>" + state + "</td>" +
        '<td><div class="act">' + revoke + "</div></td>" +
        "</tr>";
    }).join("");
  }

  async function loadLinks() {
    try {
      var r = await fetch("/api/v1/install-tokens", { headers: { Accept: "application/json" } });
      if (r.status === 401) { window.location.href = "/login"; return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      renderLinks(await r.json());
    } catch (_) {
      linksBody.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Erreur de chargement.</td></tr>';
    }
  }

  if (linkForm) {
    linkForm.addEventListener("submit", async function (e) {
      e.preventDefault();
      var m = document.getElementById("link-msg");
      clearMsg(m);
      var label = document.getElementById("link-label").value.trim();
      var ttl = parseInt(document.getElementById("link-ttl").value, 10);
      if (isNaN(ttl)) ttl = 7;
      try {
        var r = await fetch("/api/v1/install-tokens", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ label: label, ttl_days: ttl }),
        });
        var d = await jsonOf(r);
        if (!r.ok) { setMsg(m, d.error || "Échec.", false); return; }
        linkOneliner.textContent = d.one_liner || "";
        document.getElementById("link-expire").textContent =
          d.expires_at ? ("Ce lien expire le " + fmtDate(d.expires_at) + ". À usage de déploiement uniquement.")
                       : "Ce lien n'expire pas — pensez à le révoquer après le déploiement.";
        linkResult.classList.remove("hidden");
        linkForm.reset();
        document.getElementById("link-ttl").value = "7";
        setMsg(m, "Lien généré.", true);
        loadLinks();
      } catch (_) { setMsg(m, "Erreur réseau.", false); }
    });
  }

  var linkCopy = document.getElementById("link-copy");
  if (linkCopy) {
    linkCopy.addEventListener("click", function () {
      var txt = linkOneliner.textContent || "";
      if (!txt) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(function () {
          linkCopy.classList.add("ok-flash");
          setTimeout(function () { linkCopy.classList.remove("ok-flash"); }, 900);
        });
      }
    });
  }

  linksBody.addEventListener("click", async function (e) {
    var btn = e.target.closest('[data-action="revoke-link"]');
    if (!btn) return;
    if (!window.confirm("Révoquer ce lien d'installation ? Les postes déjà enrôlés ne sont pas affectés.")) return;
    try {
      var r = await fetch("/api/v1/install-tokens/" + btn.getAttribute("data-id"), {
        method: "DELETE", headers: { Accept: "application/json" },
      });
      var d = await jsonOf(r);
      if (!r.ok) window.alert(d.error || "Action refusée.");
    } catch (_) { window.alert("Erreur réseau."); }
    loadLinks();
  });

  // ------------------------------------------------------------- Versions
  var relsBody = document.getElementById("rels-body");
  var relForm = document.getElementById("rel-form");
  var deployTag = document.getElementById("deploy-current");

  function renderReleases(rels) {
    var current = rels.filter(function (r) { return r.is_current; })[0];
    if (deployTag) deployTag.textContent = current ? ("courante : v" + current.version) : "aucune version";
    if (!relsBody) return;

    if (!rels.length) {
      relsBody.innerHTML = '<tr><td colspan="6" class="empty-cell">Aucun paquet publié. Téléversez le .zip produit par build.ps1.</td></tr>';
      return;
    }
    relsBody.innerHTML = rels.map(function (r) {
      var state = r.is_current
        ? '<span class="badge ok">courante</span>'
        : '<button type="button" class="btn xs" data-action="set-current" data-id="' + esc(r.id) + '">Activer</button>';
      var del = r.is_current
        ? ""
        : '<button type="button" class="btn xs danger" data-action="del-release" data-id="' + esc(r.id) + '">Suppr.</button>';
      return "<tr>" +
        '<td><div class="nm mono">v' + esc(r.version) + "</div>" +
          (r.notes ? '<div class="sub">' + esc(r.notes) + "</div>" : "") + "</td>" +
        "<td>" + fmtSize(r.size) + "</td>" +
        "<td>" + fmtDate(r.published_at) + "</td>" +
        "<td>" + esc(r.published_by || "—") + "</td>" +
        "<td>" + state + "</td>" +
        '<td><div class="act">' + del + "</div></td>" +
        "</tr>";
    }).join("");
  }

  async function loadReleases() {
    try {
      var r = await fetch("/api/v1/agent-releases", { headers: { Accept: "application/json" } });
      if (r.status === 401) { window.location.href = "/login"; return; }
      if (r.status === 403) { if (deployTag) deployTag.textContent = "—"; return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      renderReleases(await r.json());
    } catch (_) {
      if (relsBody) relsBody.innerHTML = '<tr><td colspan="6" class="empty-cell err-cell">Erreur de chargement.</td></tr>';
    }
  }

  if (relForm) {
    relForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var m = document.getElementById("rel-msg");
      clearMsg(m);
      var fileInput = document.getElementById("rel-file");
      if (!fileInput.files || !fileInput.files[0]) { setMsg(m, "Sélectionnez un fichier .zip.", false); return; }

      var fd = new FormData();
      fd.append("file", fileInput.files[0]);
      fd.append("notes", document.getElementById("rel-notes").value.trim());
      fd.append("make_current", document.getElementById("rel-current").checked ? "true" : "false");

      // XHR pour la barre de progression du téléversement (paquet volumineux).
      var prog = document.getElementById("rel-progress");
      var bar = document.getElementById("rel-bar");
      var pct = document.getElementById("rel-pct");
      var submit = document.getElementById("rel-submit");
      prog.classList.remove("hidden");
      bar.value = 0; pct.textContent = "0 %"; submit.disabled = true;

      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/v1/agent-releases");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.onprogress = function (ev) {
        if (ev.lengthComputable) {
          var p = Math.round((ev.loaded / ev.total) * 100);
          bar.value = p; pct.textContent = p + " %";
        }
      };
      xhr.onload = function () {
        submit.disabled = false;
        prog.classList.add("hidden");
        var d = {};
        try { d = JSON.parse(xhr.responseText); } catch (_) {}
        if (xhr.status >= 200 && xhr.status < 300) {
          setMsg(m, "Version v" + (d.version || "?") + " publiée.", true);
          relForm.reset();
          document.getElementById("rel-current").checked = true;
          loadReleases();
        } else {
          setMsg(m, d.error || ("Échec (HTTP " + xhr.status + ")."), false);
        }
      };
      xhr.onerror = function () { submit.disabled = false; prog.classList.add("hidden"); setMsg(m, "Erreur réseau.", false); };
      xhr.send(fd);
    });
  }

  if (relsBody) {
    relsBody.addEventListener("click", async function (e) {
      var btn = e.target.closest("[data-action]");
      if (!btn) return;
      var action = btn.getAttribute("data-action");
      var id = btn.getAttribute("data-id");
      if (action === "set-current") {
        try {
          var r = await fetch("/api/v1/agent-releases/" + id + "/current", { method: "POST", headers: { Accept: "application/json" } });
          var d = await jsonOf(r);
          if (!r.ok) window.alert(d.error || "Action refusée.");
        } catch (_) { window.alert("Erreur réseau."); }
        loadReleases();
      } else if (action === "del-release") {
        if (!window.confirm("Supprimer ce paquet ? (irréversible)")) return;
        try {
          var r2 = await fetch("/api/v1/agent-releases/" + id, { method: "DELETE", headers: { Accept: "application/json" } });
          var d2 = await jsonOf(r2);
          if (!r2.ok) window.alert(d2.error || "Action refusée.");
        } catch (_) { window.alert("Erreur réseau."); }
        loadReleases();
      }
    });
  }

  loadLinks();
  loadReleases();
})();
