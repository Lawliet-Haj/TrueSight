// TrueSight — Copilote IA (fiche poste). Chargé uniquement pour les admins quand
// une clé API est configurée. Le Copilote LIT la télémétrie et PROPOSE des actions ;
// rien n'est exécuté sans confirmation explicite. Les propositions sont rendues en
// texte brut (textContent — jamais innerHTML) : la sortie du modèle est non fiable.
(function () {
  "use strict";

  var pageData = document.getElementById("page-data");
  if (!pageData) return;
  if (pageData.getAttribute("data-ai-enabled") !== "1") return;
  var AGENT_ID = pageData.getAttribute("data-agent-id");

  var feed = document.getElementById("cop-feed");
  var input = document.getElementById("cop-input");
  var sendBtn = document.getElementById("cop-send");
  if (!feed || !input || !sendBtn) return;

  var history = [];
  var busy = false;

  var KIND_LABEL = {
    run_script: "Exécuter un script",
    install_software: "Installer une application",
    uninstall_software: "Désinstaller une application",
    quick_action: "Action rapide",
    run_command: "Exécuter une commande",
  };
  var STATUS_LABEL = {
    pending: "en attente", dispatched: "transmise", running: "en cours",
    done: "terminée", error: "erreur", timeout: "délai dépassé",
  };

  function clearIntro() {
    var intro = feed.querySelector(".cop-intro");
    if (intro) intro.remove();
  }

  function addBubble(role, text) {
    clearIntro();
    var b = document.createElement("div");
    b.className = "cop-msg cop-" + role;
    b.textContent = text;
    feed.appendChild(b);
    feed.scrollTop = feed.scrollHeight;
    return b;
  }

  function addProposal(p) {
    var card = document.createElement("div");
    card.className = "cop-prop" + (p.danger ? " danger" : "");

    var head = document.createElement("div");
    head.className = "cop-prop-head";
    head.textContent = KIND_LABEL[p.kind] || p.kind;
    if (p.danger) {
      var warn = document.createElement("span");
      warn.className = "cop-prop-flag";
      warn.textContent = "à risque";
      head.appendChild(warn);
    }
    card.appendChild(head);

    if (p.rationale) {
      var why = document.createElement("div");
      why.className = "cop-prop-why";
      why.textContent = p.rationale;
      card.appendChild(why);
    }

    var pv = p.preview || {};
    var pre = document.createElement("pre");
    pre.className = "cop-prop-cmd";
    pre.textContent = (pv.shell ? pv.shell + " : " : "") + (pv.command_text || "");
    card.appendChild(pre);

    var actions = document.createElement("div");
    actions.className = "cop-prop-actions";
    var ok = document.createElement("button");
    ok.type = "button"; ok.className = "btn xs go"; ok.textContent = "Confirmer";
    var no = document.createElement("button");
    no.type = "button"; no.className = "btn xs"; no.textContent = "Ignorer";
    var status = document.createElement("span");
    status.className = "cmd-status";
    actions.appendChild(ok); actions.appendChild(no); actions.appendChild(status);
    card.appendChild(actions);

    var output = document.createElement("pre");
    output.className = "cmd-output hidden";
    card.appendChild(output);

    feed.appendChild(card);
    feed.scrollTop = feed.scrollHeight;

    no.addEventListener("click", function () {
      ok.disabled = true; no.disabled = true;
      card.classList.add("dismissed");
      status.textContent = "ignorée";
    });
    ok.addEventListener("click", function () {
      if (ok.disabled) return;
      function go() { ok.disabled = true; no.disabled = true; runConfirm(p, status, output); }
      if (p.danger) {
        TS.confirm({
          title: (KIND_LABEL[p.kind] || "Action") + " ?",
          body: pre.textContent, danger: true, confirmLabel: "Confirmer",
        }).then(function (r) { if (r.confirmed) go(); });
      } else {
        go();
      }
    });
  }

  function runConfirm(p, statusEl, outputEl) {
    var c = p.confirm || {};
    if (!c.endpoint) { statusEl.textContent = "proposition invalide"; return; }
    statusEl.textContent = "envoi…";
    fetch(c.endpoint, {
      method: c.method || "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(c.body || {}),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { statusEl.textContent = "erreur : " + (res.d.error || "échec"); return; }
        if (res.d.command_id) { statusEl.textContent = "en file…"; pollCmd(res.d.command_id, statusEl, outputEl); }
        else { statusEl.textContent = "envoyée"; }
      })
      .catch(function () { statusEl.textContent = "erreur réseau"; });
  }

  function pollCmd(commandId, statusEl, outputEl) {
    var attempts = 0;
    var timer = setInterval(function () {
      attempts++;
      fetch("/api/v1/commands/" + commandId, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          statusEl.textContent = "statut : " + (STATUS_LABEL[d.status] || d.status);
          if (["done", "error", "timeout"].indexOf(d.status) !== -1) {
            clearInterval(timer);
            var res = d.result || {};
            var lines = ["Code de sortie : " + (res.exit_code != null ? res.exit_code : "—")];
            if (res.stdout) { lines.push("", "--- STDOUT ---", res.stdout); }
            if (res.stderr) { lines.push("", "--- STDERR ---", res.stderr); }
            outputEl.textContent = lines.join("\n");
            outputEl.classList.remove("hidden");
          }
        })
        .catch(function () {});
      if (attempts > 150) clearInterval(timer);
    }, 2000);
  }

  function send() {
    var msg = input.value.trim();
    if (!msg || busy) return;
    busy = true; sendBtn.disabled = true;
    addBubble("user", msg);
    input.value = "";
    var thinking = addBubble("assistant", "…");
    thinking.classList.add("thinking");

    fetch("/api/v1/ai/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ message: msg, agent_id: AGENT_ID, history: history }),
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        thinking.remove();
        if (!res.ok) { addBubble("assistant", "Erreur : " + (res.d.error || "réponse invalide")); return; }
        var d = res.d;
        addBubble("assistant", d.reply || "(pas de réponse)");
        (d.proposals || []).forEach(addProposal);
        if (Array.isArray(d.history)) history = d.history;
      })
      .catch(function () { thinking.remove(); addBubble("assistant", "Erreur réseau."); })
      .then(function () { busy = false; sendBtn.disabled = false; input.focus(); });
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); send(); }
  });
})();
