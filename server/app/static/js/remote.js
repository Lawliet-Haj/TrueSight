// TrueSight — Bureau à distance (VIEWER). Conforme à REMOTE.md (itération R1+R2).
//
// Flux :
//   1. Clic « Prendre la main » → POST /api/v1/agents/<id>/remote-session (admin)
//      → réponse 201 {session_id, token, ws_url:"wss://<host>/ws/remote/viewer?token=..."}
//   2. WebSocket(ws_url) en binaryType="arraybuffer".
//   3. Message BINAIRE agent→viewer : en-tête 8 octets
//        [0x01][0x00][width u16 LE][height u16 LE][monitor u8][flags u8] + octets JPEG (pleine trame).
//      → createImageBitmap(Blob JPEG) → dessin sur <canvas id="pv-remote-canvas">.
//   4. Capture souris/clavier sur le canvas → messages TEXTE JSON viewer→agent (coords 0..1).
//
// La logique remote est isolée ici (pas dans agent_detail.js).
(function () {
  "use strict";

  var pageData = document.getElementById("page-data");
  if (!pageData) return;
  var AGENT_ID = pageData.getAttribute("data-agent-id");
  var IS_ADMIN = pageData.getAttribute("data-is-admin") === "1";
  if (!IS_ADMIN || !AGENT_ID) return;

  // --- Éléments DOM ---
  var elStart = document.getElementById("remote-start");
  var elStop = document.getElementById("remote-stop");
  var elControl = document.getElementById("remote-control");
  var elFull = document.getElementById("remote-fullscreen");
  var elScreen = document.getElementById("remote-screen");
  var elCanvas = document.getElementById("pv-remote-canvas");
  var elBar = document.getElementById("remote-bar");
  var elRecLabel = document.getElementById("remote-rec-label");
  var elUser = document.getElementById("remote-user");
  var elFps = document.getElementById("remote-fps");
  var elLatency = document.getElementById("remote-latency");
  var elMonitor = document.getElementById("remote-monitor");
  var elMonitors = document.getElementById("remote-monitors");
  var elError = document.getElementById("remote-error");
  var elMode = document.getElementById("remote-mode");
  if (!elStart || !elCanvas) return;

  var ctx = elCanvas.getContext("2d", { alpha: false });

  // --- État de session ---
  var ws = null;
  var sessionId = null;
  var controlling = false;     // le viewer envoie-t-il les entrées ?
  var currentMonitor = 0;
  var canvasW = elCanvas.width;
  var canvasH = elCanvas.height;

  // --- Compteurs fps / latence ---
  var frameCount = 0;
  var fpsTimer = null;
  var lastFrameAt = 0;
  var pingTimer = null;
  var lastPingSentAt = 0;

  // --- Fluidité : presets de flux + mode Auto adaptatif (selon la latence) ---
  // q = qualité JPEG, fps = cadence cible, w = largeur max (0 = pleine résolution).
  var PRESETS = {
    fluid:    { q: 45, fps: 24, w: 1280 },
    balanced: { q: 65, fps: 18, w: 1600 },
    sharp:    { q: 85, fps: 14, w: 0 },
  };
  var currentPreset = "balanced";
  var adaptiveOn = false;
  var autoApplied = null;
  var lastRtt = null;

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

  function setLiveState(state) {
    // state : "off" | "connecting" | "live"
    if (state === "live") {
      elBar.classList.add("is-live");
      elRecLabel.textContent = "LIVE";
    } else if (state === "connecting") {
      elBar.classList.add("is-live");
      elRecLabel.textContent = "CONNEXION…";
    } else {
      elBar.classList.remove("is-live");
      elRecLabel.textContent = "HORS LIGNE";
    }
  }

  function setButtonsForActive(active) {
    if (active) {
      elStart.classList.add("hidden");
      elStop.classList.remove("hidden");
      elFull.disabled = false;
      elControl.disabled = false;
    } else {
      elStart.classList.remove("hidden");
      elStart.disabled = false;
      elStart.innerHTML = '<svg><use href="#i-play"/></svg>Prendre la main';
      elStop.classList.add("hidden");
      elFull.disabled = true;
      elControl.disabled = true;
      setControlling(false);
    }
  }

  function setControlling(on) {
    controlling = on;
    if (on) {
      elCanvas.classList.add("controlling");
      elControl.classList.add("go");
      elControl.innerHTML = '<svg><use href="#i-hand"/></svg>Contrôle actif';
      elCanvas.focus();
    } else {
      elCanvas.classList.remove("controlling");
      elControl.classList.remove("go");
      elControl.innerHTML = '<svg><use href="#i-hand"/></svg>Prendre le contrôle';
    }
  }

  // ---------------------------------------------------------------------------
  // Démarrage de session (signalisation)
  // ---------------------------------------------------------------------------
  async function startSession() {
    clearError();
    elStart.disabled = true;
    elStart.innerHTML = '<span class="spin"></span>Ouverture…';
    setLiveState("connecting");

    var data;
    try {
      var resp = await fetch("/api/v1/agents/" + AGENT_ID + "/remote-session", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({}),
      });
      if (resp.status === 401) { window.location.href = "/login"; return; }
      if (resp.status === 403) { throw new Error("Action réservée aux administrateurs."); }
      data = await resp.json();
      if (!resp.ok) {
        throw new Error(data && data.error ? data.error : "HTTP " + resp.status);
      }
    } catch (e) {
      showError("Impossible d'ouvrir la session : " + (e.message || e));
      setLiveState("off");
      setButtonsForActive(false);
      return;
    }

    sessionId = data.session_id || null;
    var wsUrl = data.ws_url;
    if (!wsUrl) {
      showError("Réponse serveur incomplète (ws_url manquant).");
      setLiveState("off");
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
      setLiveState("off");
      setButtonsForActive(false);
      return;
    }
    ws.binaryType = "arraybuffer";

    ws.onopen = function () {
      setButtonsForActive(true);
      setLiveState("connecting"); // passe à "live" à la première trame reçue
      bindInputs();
      startFpsCounter();
      startPing();
      // Applique le mode de fluidité choisi (qualité + cadence + largeur) puis
      // demande une keyframe pleine trame.
      applyPreset(currentPreset);
    };

    ws.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        handleTextMessage(ev.data);
      } else {
        handleBinaryFrame(ev.data);
      }
    };

    ws.onerror = function () {
      showError("Erreur de connexion au relais.");
    };

    ws.onclose = function (ev) {
      teardown();
      if (ev && ev.code !== 1000 && ev.code !== 1005) {
        showError("Session terminée (code " + ev.code + (ev.reason ? " — " + ev.reason : "") + ").");
      }
    };
  }

  // ---------------------------------------------------------------------------
  // Réception : trames binaires
  // ---------------------------------------------------------------------------
  function handleBinaryFrame(buffer) {
    if (!buffer || buffer.byteLength < 8) return;
    var dv = new DataView(buffer);
    // En-tête 8 octets : [version u8][type u8][width u16 LE][height u16 LE][monitor u8][flags u8]
    var version = dv.getUint8(0);
    var frameType = dv.getUint8(1);
    if (version !== 0x01) return;   // version de protocole gérée.
    if (frameType !== 0x00) return; // R1+R2 : seul le type 0x00 (pleine trame) est géré.
    var width = dv.getUint16(2, true);
    var height = dv.getUint16(4, true);
    var monitor = dv.getUint8(6);
    // var flags = dv.getUint8(7); // réservé

    currentMonitor = monitor;
    if (elMonitor) elMonitor.textContent = (monitor + 1);

    // Ajuste la taille du canvas à la résolution annoncée.
    if (width && height && (canvasW !== width || canvasH !== height)) {
      canvasW = width; canvasH = height;
      elCanvas.width = width;
      elCanvas.height = height;
    }

    var jpegBytes = new Uint8Array(buffer, 8);
    var blob = new Blob([jpegBytes], { type: "image/jpeg" });

    createImageBitmap(blob).then(function (bitmap) {
      try {
        ctx.drawImage(bitmap, 0, 0, elCanvas.width, elCanvas.height);
      } catch (e) { /* ignore */ }
      bitmap.close && bitmap.close();
      onFrameRendered();
    }).catch(function () {
      // Trame illisible : on l'ignore.
    });
  }

  function onFrameRendered() {
    frameCount++;
    lastFrameAt = performance.now();
    if (!elScreen.classList.contains("has-stream")) {
      elScreen.classList.add("has-stream");
      setLiveState("live");
    }
  }

  // ---------------------------------------------------------------------------
  // Réception : messages texte (pong de latence, info écran, etc.)
  // ---------------------------------------------------------------------------
  function handleTextMessage(text) {
    var msg;
    try { msg = JSON.parse(text); } catch (e) { return; }
    if (!msg || !msg.t) return;

    if (msg.t === "pong") {
      var rtt = Math.round(performance.now() - lastPingSentAt);
      if (elLatency) elLatency.textContent = rtt;
      maybeAdapt(rtt);
    } else if (msg.t === "monitors" && Array.isArray(msg.list)) {
      renderMonitorButtons(msg.list);
    } else if (msg.t === "user" && elUser) {
      elUser.textContent = msg.name || "—";
    }
  }

  function renderMonitorButtons(list) {
    if (!elMonitors) return;
    // Conserve le libellé, retire les anciens boutons.
    Array.prototype.slice.call(elMonitors.querySelectorAll(".btn")).forEach(function (b) { b.remove(); });
    if (list.length <= 1) { elMonitors.classList.add("hidden"); return; }
    elMonitors.classList.remove("hidden");
    list.forEach(function (_, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "btn" + (i === currentMonitor ? " go" : "");
      b.style.flex = "0 0 auto";
      b.style.height = "30px";
      b.style.padding = "0 12px";
      b.textContent = "Écran " + (i + 1);
      b.addEventListener("click", function () {
        sendInput({ t: "set_monitor", i: i });
        sendInput({ t: "request_keyframe" });
      });
      elMonitors.appendChild(b);
    });
  }

  // ---------------------------------------------------------------------------
  // Envoi : entrées viewer→agent (JSON texte, coords normalisées 0..1)
  // ---------------------------------------------------------------------------
  function sendInput(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify(obj)); } catch (e) { /* ignore */ }
    }
  }

  // Convertit un événement souris en coordonnées normalisées 0..1 sur l'écran distant.
  function normCoords(ev) {
    var rect = elCanvas.getBoundingClientRect();
    // L'image est dessinée en "object-fit:contain" : on calcule la zone réellement occupée.
    var dispW = rect.width, dispH = rect.height;
    var ratioImg = canvasW / canvasH;
    var ratioBox = dispW / dispH;
    var drawW, drawH, offX, offY;
    if (ratioBox > ratioImg) {
      drawH = dispH; drawW = dispH * ratioImg;
      offX = (dispW - drawW) / 2; offY = 0;
    } else {
      drawW = dispW; drawH = dispW / ratioImg;
      offX = 0; offY = (dispH - drawH) / 2;
    }
    var x = (ev.clientX - rect.left - offX) / drawW;
    var y = (ev.clientY - rect.top - offY) / drawH;
    return {
      x: Math.max(0, Math.min(1, x)),
      y: Math.max(0, Math.min(1, y)),
    };
  }

  function mouseButtonName(btn) {
    return btn === 2 ? "right" : btn === 1 ? "middle" : "left";
  }

  var inputsBound = false;
  function bindInputs() {
    if (inputsBound) return;
    inputsBound = true;

    elCanvas.addEventListener("mousemove", function (ev) {
      if (!controlling) return;
      var c = normCoords(ev);
      sendInput({ t: "mouse_move", x: c.x, y: c.y });
    });

    elCanvas.addEventListener("mousedown", function (ev) {
      if (!controlling) return;
      ev.preventDefault();
      elCanvas.focus();
      var c = normCoords(ev);
      sendInput({ t: "mouse_down", button: mouseButtonName(ev.button), x: c.x, y: c.y });
    });

    elCanvas.addEventListener("mouseup", function (ev) {
      if (!controlling) return;
      ev.preventDefault();
      var c = normCoords(ev);
      sendInput({ t: "mouse_up", button: mouseButtonName(ev.button), x: c.x, y: c.y });
    });

    elCanvas.addEventListener("contextmenu", function (ev) {
      // Empêche le menu contextuel du navigateur quand on contrôle.
      if (controlling) ev.preventDefault();
    });

    elCanvas.addEventListener("wheel", function (ev) {
      if (!controlling) return;
      ev.preventDefault();
      sendInput({ t: "wheel", dy: Math.round(ev.deltaY) });
    }, { passive: false });

    elCanvas.addEventListener("keydown", function (ev) {
      if (!controlling) return;
      ev.preventDefault();
      var msg = { t: "key_down", vk: ev.keyCode };
      if (ev.key && ev.key.length === 1) msg.unicode = ev.key;
      sendInput(msg);
    });

    elCanvas.addEventListener("keyup", function (ev) {
      if (!controlling) return;
      ev.preventDefault();
      var msg = { t: "key_up", vk: ev.keyCode };
      if (ev.key && ev.key.length === 1) msg.unicode = ev.key;
      sendInput(msg);
    });

    // Sortie du canvas : on relâche le contrôle clavier (sécurité).
    elCanvas.addEventListener("blur", function () {
      if (controlling) setControlling(false);
    });
  }

  // ---------------------------------------------------------------------------
  // Compteurs (fps / latence)
  // ---------------------------------------------------------------------------
  function startFpsCounter() {
    stopFpsCounter();
    frameCount = 0;
    fpsTimer = setInterval(function () {
      if (elFps) elFps.textContent = frameCount;
      frameCount = 0;
    }, 1000);
  }
  function stopFpsCounter() {
    if (fpsTimer) { clearInterval(fpsTimer); fpsTimer = null; }
  }

  function startPing() {
    stopPing();
    pingTimer = setInterval(function () {
      if (ws && ws.readyState === WebSocket.OPEN) {
        lastPingSentAt = performance.now();
        sendInput({ t: "ping", ts: Date.now() });
      }
    }, 2000);
  }
  function stopPing() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  // Réglages cible en mode Auto selon la latence mesurée (RTT).
  function autoSettingsForRtt(rtt) {
    if (rtt == null) return { q: 65, fps: 18, w: 1600 };
    if (rtt < 80) return { q: 75, fps: 22, w: 1600 };
    if (rtt < 180) return { q: 60, fps: 18, w: 1600 };
    if (rtt < 350) return { q: 48, fps: 14, w: 1366 };
    return { q: 35, fps: 10, w: 1280 };
  }

  function applySettings(s) {
    sendInput({ t: "set_max_width", w: s.w });
    sendInput({ t: "set_quality", q: s.q });
    sendInput({ t: "set_fps", fps: s.fps });
    sendInput({ t: "request_keyframe" });
  }

  function applyPreset(name) {
    currentPreset = name;
    try { localStorage.setItem("ts-remote-mode", name); } catch (e) { /* ignore */ }
    if (name === "auto") {
      adaptiveOn = true;
      autoApplied = autoSettingsForRtt(lastRtt);
      applySettings(autoApplied);
    } else {
      adaptiveOn = false;
      applySettings(PRESETS[name] || PRESETS.balanced);
    }
  }

  // En mode Auto : réagit aux variations de latence (sans osciller : on ne ré-applique
  // que si le palier qualité/cadence change réellement).
  function maybeAdapt(rtt) {
    lastRtt = rtt;
    if (!adaptiveOn) return;
    var s = autoSettingsForRtt(rtt);
    if (!autoApplied || s.q !== autoApplied.q || s.fps !== autoApplied.fps) {
      autoApplied = s;
      applySettings(s);
    }
  }

  // ---------------------------------------------------------------------------
  // Arrêt / nettoyage
  // ---------------------------------------------------------------------------
  function stopSession() {
    if (ws) {
      try { ws.close(1000, "viewer_end"); } catch (e) { /* ignore */ }
    } else {
      teardown();
    }
  }

  function teardown() {
    stopFpsCounter();
    stopPing();
    ws = null;
    sessionId = null;
    setLiveState("off");
    setButtonsForActive(false);
    if (elFps) elFps.textContent = "0";
    if (elLatency) elLatency.textContent = "—";
    elScreen.classList.remove("has-stream");
    // Efface le canvas.
    try { ctx.fillStyle = "#05080a"; ctx.fillRect(0, 0, elCanvas.width, elCanvas.height); } catch (e) { /* ignore */ }
    if (document.fullscreenElement) {
      try { document.exitFullscreen(); } catch (e) { /* ignore */ }
    }
  }

  // ---------------------------------------------------------------------------
  // Branchements boutons
  // ---------------------------------------------------------------------------
  elStart.addEventListener("click", startSession);
  elStop.addEventListener("click", stopSession);

  elControl.addEventListener("click", function () {
    if (elControl.disabled) return;
    setControlling(!controlling);
  });

  elFull.addEventListener("click", function () {
    if (elFull.disabled) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (elScreen.requestFullscreen) {
      elScreen.requestFullscreen().catch(function () { /* ignore */ });
    }
  });

  // Ferme proprement la session si l'onglet est quitté.
  window.addEventListener("beforeunload", function () {
    if (ws) { try { ws.close(1000, "page_unload"); } catch (e) { /* ignore */ } }
  });

  // Sélecteur de fluidité : restaure le choix mémorisé et applique à la volée.
  if (elMode) {
    var saved = null;
    try { saved = localStorage.getItem("ts-remote-mode"); } catch (e) { /* ignore */ }
    if (saved && (saved === "auto" || PRESETS[saved])) elMode.value = saved;
    currentPreset = elMode.value || "balanced";
    elMode.addEventListener("change", function () {
      currentPreset = elMode.value;
      if (ws && ws.readyState === WebSocket.OPEN) applyPreset(currentPreset);
      else { try { localStorage.setItem("ts-remote-mode", currentPreset); } catch (e) { /* ignore */ } }
    });
  }

  // État initial.
  setButtonsForActive(false);
  setLiveState("off");
})();
