// TrueSight — Terminal interactif (admin). Onglet « Terminal » de la fiche poste.
//
// Flux :
//   1. Clic « Ouvrir le terminal » → POST /api/v1/agents/<id>/remote-session
//      avec body {"kind":"terminal","shell":"powershell"|"cmd"} → {token, ws_url, ...}.
//   2. new WebSocket(ws_url) ; un xterm.js (FitAddon) est monté dans #terminal-screen.
//   3. PROTOCOLE (JSON texte, deux sens) :
//        viewer→agent : {"t":"resize","cols","rows"} | {"t":"input","data"} | {"t":"ping"}
//        agent→viewer : {"t":"output","data"} | {"t":"exit"[,"code"]} | {"t":"pong"}
//
// xterm.js et l'addon FitAddon sont chargés via CDN dans base.html.
(function () {
  "use strict";

  var pageData = document.getElementById("page-data");
  if (!pageData) return;
  var AGENT_ID = pageData.getAttribute("data-agent-id");
  var IS_ADMIN = pageData.getAttribute("data-is-admin") === "1";
  if (!IS_ADMIN || !AGENT_ID) return;

  // --- Éléments DOM ---
  var elOpen = document.getElementById("terminal-open");
  var elClose = document.getElementById("terminal-close");
  var elShell = document.getElementById("terminal-shell");
  var elScreen = document.getElementById("terminal-screen");
  var elPlaceholder = document.getElementById("terminal-placeholder");
  var elBar = document.getElementById("terminal-bar");
  var elStateLabel = document.getElementById("terminal-state-label");
  var elLatency = document.getElementById("terminal-latency");
  var elError = document.getElementById("terminal-error");
  if (!elOpen || !elScreen) return;

  // xterm.js disponible ?
  var XTerm = window.Terminal;
  var FitAddonNS = window.FitAddon;
  if (!XTerm) {
    elOpen.disabled = true;
    showError("xterm.js n'a pas pu être chargé (vérifiez la connexion CDN).");
    return;
  }

  // --- État de session ---
  var ws = null;
  var term = null;
  var fitAddon = null;
  var pingTimer = null;
  var lastPingSentAt = 0;
  var open = false;

  // ---------------------------------------------------------------------------
  // Utilitaires UI
  // ---------------------------------------------------------------------------
  function showError(msg) {
    if (!elError) return;
    elError.textContent = msg;
    elError.classList.add("show");
  }
  function clearError() {
    if (!elError) return;
    elError.textContent = "";
    elError.classList.remove("show");
  }

  function setState(state) {
    // state : "off" | "connecting" | "live"
    if (!elBar) return;
    if (state === "live") {
      elBar.classList.add("is-live");
      if (elStateLabel) elStateLabel.textContent = "OUVERT";
    } else if (state === "connecting") {
      elBar.classList.add("is-live");
      if (elStateLabel) elStateLabel.textContent = "CONNEXION…";
    } else {
      elBar.classList.remove("is-live");
      if (elStateLabel) elStateLabel.textContent = "HORS LIGNE";
    }
  }

  function setButtonsForActive(active) {
    if (active) {
      elOpen.classList.add("hidden");
      elClose.classList.remove("hidden");
      if (elShell) elShell.disabled = true;
    } else {
      elOpen.classList.remove("hidden");
      elOpen.disabled = false;
      elOpen.innerHTML = '<svg><use href="#i-play"/></svg>Ouvrir le terminal';
      elClose.classList.add("hidden");
      if (elShell) elShell.disabled = false;
    }
  }

  // ---------------------------------------------------------------------------
  // xterm.js
  // ---------------------------------------------------------------------------
  function buildTerminal() {
    // Conteneur d'accueil du terminal (placeholder masqué pendant la session).
    if (elPlaceholder) elPlaceholder.classList.add("hidden");

    var mount = document.createElement("div");
    mount.className = "term-mount";
    mount.id = "term-mount";
    elScreen.appendChild(mount);

    term = new XTerm({
      cursorBlink: true,
      convertEol: false,
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: 13,
      lineHeight: 1.2,
      scrollback: 5000,
      theme: {
        background: "#05080a",
        foreground: "#cfe7df",
        cursor: "#2BE3C6",
        cursorAccent: "#05080a",
        selectionBackground: "rgba(43,227,198,.25)",
        black: "#0C1116",
        brightBlack: "#5A6773",
        red: "#FF5D63",
        green: "#34E2B0",
        yellow: "#F5B83D",
        blue: "#5AB6FF",
        magenta: "#b48ef0",
        cyan: "#2BE3C6",
        white: "#E8EEF4",
        brightWhite: "#ffffff",
      },
    });

    if (FitAddonNS && FitAddonNS.FitAddon) {
      fitAddon = new FitAddonNS.FitAddon();
      term.loadAddon(fitAddon);
    }
    term.open(mount);
    fit();
    term.focus();

    term.onData(function (d) {
      sendJson({ t: "input", data: d });
    });
  }

  function fit() {
    if (!fitAddon || !term) return;
    try { fitAddon.fit(); } catch (e) { /* conteneur masqué : ignoré */ }
  }

  function sendResize() {
    if (!term) return;
    sendJson({ t: "resize", cols: term.cols, rows: term.rows });
  }

  function disposeTerminal() {
    if (term) {
      try { term.dispose(); } catch (e) { /* ignore */ }
      term = null;
    }
    fitAddon = null;
    var mount = document.getElementById("term-mount");
    if (mount && mount.parentNode) mount.parentNode.removeChild(mount);
    if (elPlaceholder) elPlaceholder.classList.remove("hidden");
  }

  // ---------------------------------------------------------------------------
  // Session WebSocket
  // ---------------------------------------------------------------------------
  async function openTerminal() {
    clearError();
    elOpen.disabled = true;
    elOpen.innerHTML = '<span class="spin"></span>Ouverture…';
    setState("connecting");

    var shell = (elShell && elShell.value) || "powershell";

    var data;
    try {
      var resp = await fetch("/api/v1/agents/" + AGENT_ID + "/remote-session", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ kind: "terminal", shell: shell }),
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (resp.status === 403) { throw new Error("Action réservée aux administrateurs."); }
      data = await resp.json();
      if (!resp.ok) throw new Error(data && data.error ? data.error : "HTTP " + resp.status);
    } catch (e) {
      showError("Impossible d'ouvrir le terminal : " + (e.message || e));
      setState("off");
      setButtonsForActive(false);
      return;
    }

    var wsUrl = data.ws_url;
    if (!wsUrl) {
      showError("Réponse serveur incomplète (ws_url manquant).");
      setState("off");
      setButtonsForActive(false);
      return;
    }

    openSocket(wsUrl);
  }

  function openSocket(wsUrl) {
    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      showError("WebSocket invalide : " + (e.message || e));
      setState("off");
      setButtonsForActive(false);
      return;
    }

    ws.onopen = function () {
      open = true;
      setButtonsForActive(true);
      buildTerminal();
      setState("live");
      sendResize();   // taille initiale
      startPing();
    };

    ws.onmessage = function (ev) {
      if (typeof ev.data !== "string") return; // protocole texte uniquement
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (!msg || !msg.t) return;

      if (msg.t === "output") {
        if (term && typeof msg.data === "string") term.write(msg.data);
      } else if (msg.t === "exit") {
        var code = (msg.code != null) ? (" (code " + msg.code + ")") : "";
        if (term) term.write("\r\n\x1b[90m— session terminée" + code + " —\x1b[0m\r\n");
        finishSession(false);
      } else if (msg.t === "pong") {
        var rtt = Math.round(performance.now() - lastPingSentAt);
        if (elLatency) elLatency.textContent = rtt;
      }
    };

    ws.onerror = function () {
      showError("Erreur de connexion au relais.");
    };

    ws.onclose = function (ev) {
      if (open && ev && ev.code !== 1000 && ev.code !== 1005) {
        showError("Session terminée (code " + ev.code + (ev.reason ? " — " + ev.reason : "") + ").");
      }
      finishSession(false);
    };
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(function () {
      if (ws && ws.readyState === WebSocket.OPEN) {
        lastPingSentAt = performance.now();
        sendJson({ t: "ping" });
      }
    }, 15000);
  }
  function stopPing() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  function sendJson(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(obj)); } catch (e) { /* ignore */ }
    }
  }

  // Fermeture demandée par l'utilisateur.
  function closeTerminal() {
    if (ws) {
      try { ws.close(1000, "viewer_end"); } catch (e) { /* ignore */ }
    } else {
      finishSession(true);
    }
  }

  // Nettoyage commun (fermeture serveur ou utilisateur).
  function finishSession(keepTerminal) {
    open = false;
    stopPing();
    ws = null;
    setState("off");
    setButtonsForActive(false);
    if (elLatency) elLatency.textContent = "—";
    if (!keepTerminal) disposeTerminal();
  }

  // ---------------------------------------------------------------------------
  // Redimensionnement
  // ---------------------------------------------------------------------------
  function onResize() {
    if (!term) return;
    fit();
    sendResize();
  }
  window.addEventListener("resize", onResize);

  // L'onglet « Terminal » redevient visible : le conteneur a enfin une taille → re-fit.
  document.addEventListener("ts:tab-activated", function (ev) {
    if (ev.detail && ev.detail.tab === "terminal" && term) {
      // léger délai pour laisser le panneau s'afficher avant la mesure
      setTimeout(onResize, 30);
    }
  });

  // Ferme proprement la session si l'onglet du navigateur est quitté.
  window.addEventListener("beforeunload", function () {
    if (ws) { try { ws.close(1000, "page_unload"); } catch (e) { /* ignore */ } }
  });

  // ---------------------------------------------------------------------------
  // Branchements boutons
  // ---------------------------------------------------------------------------
  elOpen.addEventListener("click", openTerminal);
  elClose.addEventListener("click", closeTerminal);

  // État initial.
  setState("off");
  setButtonsForActive(false);
})();
