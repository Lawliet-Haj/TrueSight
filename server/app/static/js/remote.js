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
  var elShot = document.getElementById("remote-shot");
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
  var elPhase = document.getElementById("remote-phase");
  var elMode = document.getElementById("remote-mode");
  // Surcouche curseur + boutons de contrôle exclusif (lot navigation).
  var elCursor = document.getElementById("remote-cursor");
  var elLockInput = document.getElementById("remote-lockinput");
  var elSas = document.getElementById("remote-sas");
  var elPrivacy = document.getElementById("remote-privacy");
  var elLockExit = document.getElementById("remote-lockexit");
  var elAudio = document.getElementById("remote-audio");
  var elFiles = document.getElementById("remote-files");
  var elFilesPanel = document.getElementById("remote-files-panel");
  var elRfUp = document.getElementById("rf-up");
  var elRfPath = document.getElementById("rf-path");
  var elRfRoots = document.getElementById("rf-roots");
  var elRfList = document.getElementById("rf-list");
  var elRfStatus = document.getElementById("rf-status");
  var elRfUpload = document.getElementById("rf-upload");
  // Presse-papiers partagé (texte).
  var elClip = document.getElementById("remote-clip");
  var elClipPanel = document.getElementById("remote-clip-panel");
  var elRcPull = document.getElementById("rc-pull");
  var elRcPush = document.getElementById("rc-push");
  var elRcMine = document.getElementById("rc-mine");
  var elRcText = document.getElementById("rc-text");
  var elRcStatus = document.getElementById("rc-status");
  if (!elStart || !elCanvas) return;

  var ctx = elCanvas.getContext("2d", { alpha: false });

  // --- État de session ---
  var ws = null;
  var sessionId = null;
  var controlling = false;     // le viewer envoie-t-il les entrées ?
  var currentMonitor = 0;
  var canvasW = elCanvas.width;
  var canvasH = elCanvas.height;

  // --- Robustesse de session ---
  // La liaison peut tomber pour des raisons banales (Wi-Fi, veille, reboot du
  // poste, relais recyclé). Plutôt que de rendre la main à l'utilisateur, on
  // reconnecte tout seul : le jeton de session est à usage unique, donc on
  // redemande une NOUVELLE session au serveur à chaque tentative.
  var userStopped = false;         // arrêt volontaire → aucune reconnexion
  var reconnectAttempt = 0;        // 0 = connexion initiale
  var reconnectTimer = null;
  var noFrameTimer = null;         // garde « relais connecté mais aucune image »
  var gotFrameThisConn = false;    // ≥ 1 trame reçue sur CETTE connexion
  var MAX_RECONNECT = 5;
  var NO_FRAME_TIMEOUT_MS = 20000;    // connexion initiale : retour d'info rapide
  // En RECONNEXION on patiente plus longtemps : le poste peut être en train de
  // redémarrer (reprise après reboot), ce qui dépasse largement 20 s.
  var NO_FRAME_RECONNECT_MS = 45000;

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

  // --- Contrôle exclusif (toggles ; l'agent confirme via lock_state/privacy_state) ---
  var lockInputOn = false;
  var privacyOn = false;
  var lockExitOn = false;

  // --- Écoute audio (son système du poste, lecture Web Audio) ---
  var audioOn = false;
  var audioCtx = null;
  var audioGain = null;
  var audioPlayTime = 0;          // prochain instant de lecture planifié (s)
  var AUDIO_JITTER = 0.18;        // tampon initial anti-coupure (s)

  // --- Transfert de fichiers (explorateur in-session) ---
  var filesOpen = false;
  var fsXferId = 0;               // compteur d'id de transfert (u32)
  var fsCurrentPath = null;       // dossier courant côté poste
  var fsParentPath = null;        // parent (bouton « remonter »)
  var fsDownloads = {};           // id -> { name, size, received, chunks: [] }
  var fsUploadQueue = [];         // fichiers en attente d'envoi
  var fsActiveUpload = null;      // { id, file, name } en cours
  var FS_UP_CHUNK = 192 * 1024;   // taille d'un chunk d'upload (octets bruts)
  var FS_BUF_MAX = 4 * 1024 * 1024; // seuil de backpressure (bufferedAmount)
  var FS_ERR = {
    not_found: "Introuvable.", denied: "Accès refusé.",
    too_big: "Fichier trop volumineux (max 1 Go).", bad_path: "Chemin non autorisé.",
    busy: "Un transfert est déjà en cours.", io: "Erreur de lecture/écriture.",
    unattended: "Indisponible à l'écran de connexion (aucune session ouverte).",
    cancelled: "Transfert annulé.",
  };

  // --- Presse-papiers partagé (texte) ---
  var clipOpen = false;
  var CLIP_ERR = {
    unattended: "Indisponible à l'écran de connexion (aucune session ouverte).",
    unavailable: "Presse-papiers du poste inaccessible (verrouillé par une application ?).",
    bad_payload: "Contenu invalide.",
  };

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

  // Progression de connexion (informatif, PAS une erreur) : l'utilisateur voit à
  // quelle étape on en est plutôt que de fixer un écran noir muet.
  function setPhase(msg) {
    if (elPhase) elPhase.textContent = msg || "";
  }

  // Traduit un code de fermeture WebSocket en langage compréhensible.
  function closeReason(code) {
    if (code === 1006) return "liaison interrompue";
    if (code === 1001) return "poste ou navigateur mis en veille";
    if (code === 1011) return "erreur du relais";
    if (code === 1012 || code === 1013) return "relais indisponible";
    return "code " + code;
  }

  function clearNoFrameTimer() {
    if (noFrameTimer) { clearTimeout(noFrameTimer); noFrameTimer = null; }
  }

  // Première trame reçue : la liaison est réellement opérationnelle.
  function noteFrameReceived() {
    if (gotFrameThisConn) return;
    gotFrameThisConn = true;
    clearNoFrameTimer();
    reconnectAttempt = 0;   // la session est saine : on repart d'un budget neuf
    setPhase("");
    clearError();
    // Après une reconnexion, la classe has-stream est déjà là (image figée
    // conservée) : handleFullFrame ne repasserait donc pas l'état à « live ».
    setLiveState("live");
  }

  // Coupe le TRANSPORT sans réinitialiser l'UI : on garde la dernière image
  // figée et les boutons en état « session active » pendant une reconnexion.
  function teardownTransport() {
    stopFpsCounter();
    stopPing();
    clearNoFrameTimer();
    if (ws) {
      try { ws.onclose = null; ws.onerror = null; ws.onmessage = null; } catch (e) { /* ignore */ }
      try { ws.close(); } catch (e) { /* ignore */ }
    }
    ws = null;
    if (elFps) elFps.textContent = "0";
  }

  // Reconnexion différée avec backoff exponentiel (1, 2, 4, 8, 8 s).
  function scheduleReconnect(code) {
    reconnectAttempt += 1;
    var delay = Math.min(8000, 1000 * Math.pow(2, reconnectAttempt - 1));
    setLiveState("connecting");
    setPhase("Connexion perdue (" + closeReason(code) + ") — reconnexion dans "
      + Math.round(delay / 1000) + " s… (" + reconnectAttempt + "/" + MAX_RECONNECT + ")");
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      if (userStopped) return;
      startSession(true);
    }, delay);
  }

  // Le relais est connecté mais l'agent n'envoie aucune image : on interroge
  // l'état du poste pour donner une CAUSE probable et une ACTION, au lieu de
  // laisser l'écran noir indéfiniment (symptôme historique le plus déroutant).
  async function diagnoseNoFrame() {
    noFrameTimer = null;
    if (gotFrameThisConn || userStopped) return;

    // En reconnexion : le poste redémarre peut-être. On ne renonce pas tant qu'il
    // reste du budget — on repart pour un tour en l'annonçant clairement.
    if (reconnectAttempt > 0 && reconnectAttempt < MAX_RECONNECT) {
      setPhase("Poste pas encore revenu (redémarrage ?) — nouvelle tentative… ("
        + reconnectAttempt + "/" + MAX_RECONNECT + ")");
      teardownTransport();
      scheduleReconnect(1006);
      return;
    }

    var hint = "Le poste n'a envoyé aucune image.";
    try {
      var r = await fetch("/api/v1/agents/" + AGENT_ID, { headers: { Accept: "application/json" } });
      if (r.ok) {
        var d = await r.json();
        if (d && d.status === "offline") {
          hint = "Le poste est hors ligne : il n'a pas reçu la demande. "
               + "Vérifiez qu'il est allumé et connecté au réseau.";
        } else {
          hint = "Le poste est en ligne mais n'a pas ouvert le flux. Causes probables : "
               + "aucune session Windows ouverte (le compagnon ne tourne pas), "
               + "ou le poste n'atteint pas le relais (réseau / pare-feu).";
        }
      }
    } catch (e) { /* diagnostic au mieux : on garde le message générique */ }
    setPhase("");
    showError(hint);
    // Socket ouverte mais muette : inutile de boucler en reconnexion.
    userStopped = true;
    teardownTransport();
    teardown();
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
      if (elShot) elShot.disabled = false;
      setExclusiveButtons(true);
    } else {
      elStart.classList.remove("hidden");
      elStart.disabled = false;
      elStart.innerHTML = '<svg><use href="#i-play"/></svg>Prendre la main';
      elStop.classList.add("hidden");
      elFull.disabled = true;
      elControl.disabled = true;
      if (elShot) elShot.disabled = true;
      setControlling(false);
      // Réinitialise les bascules de contrôle exclusif (l'agent les relâche aussi).
      setExclusiveButtons(false);
      lockInputOn = false; privacyOn = false; lockExitOn = false; audioOn = false;
      setToggle(elLockInput, false);
      setToggle(elPrivacy, false);
      setToggle(elLockExit, false);
      setToggle(elAudio, false);
      stopAudioPlayback();
      fsResetState();
      clipResetState();
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

  // État visuel d'un bouton-bascule (vert « actif » = classe .go).
  function setToggle(btn, on) {
    if (!btn) return;
    if (on) btn.classList.add("go"); else btn.classList.remove("go");
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  // Active/désactive les boutons de contrôle exclusif (session ouverte requise).
  function setExclusiveButtons(enabled) {
    [elLockInput, elSas, elPrivacy, elLockExit, elAudio, elFiles, elClip].forEach(function (b) {
      if (b) b.disabled = !enabled;
    });
  }

  // Place la surcouche curseur (coords normalisées 0..1 au moniteur courant).
  // Même calcul object-fit:contain que normCoords (image centrée dans le canvas).
  function updateCursor(nx, ny, visible) {
    if (!elCursor) return;
    if (!visible || nx == null || ny == null || !elScreen.classList.contains("has-stream")) {
      elCursor.classList.add("hidden");
      return;
    }
    var rs = elScreen.getBoundingClientRect();
    var rc = elCanvas.getBoundingClientRect();
    if (!rc.width || !rc.height) { elCursor.classList.add("hidden"); return; }
    var dispW = rc.width, dispH = rc.height;
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
    var px = (rc.left - rs.left) + offX + Math.max(0, Math.min(1, nx)) * drawW;
    var py = (rc.top - rs.top) + offY + Math.max(0, Math.min(1, ny)) * drawH;
    elCursor.style.transform = "translate(" + px + "px," + py + "px)";
    elCursor.classList.remove("hidden");
  }

  // ---------------------------------------------------------------------------
  // Démarrage de session (signalisation)
  // ---------------------------------------------------------------------------
  // ``isRetry`` : appel issu de la reconnexion automatique (on ne réinitialise
  // ni le compteur de tentatives ni l'état des boutons).
  async function startSession(isRetry) {
    if (!isRetry) {
      userStopped = false;
      reconnectAttempt = 0;
      clearError();
      elStart.disabled = true;
      elStart.innerHTML = '<span class="spin"></span>Ouverture…';
    }
    setLiveState("connecting");
    setPhase(isRetry
      ? "Reconnexion… (" + reconnectAttempt + "/" + MAX_RECONNECT + ")"
      : "Ouverture de la session…");

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
      // Échec de signalisation : en reconnexion, on retente tant qu'il reste du budget.
      if (isRetry && !userStopped && reconnectAttempt < MAX_RECONNECT) {
        scheduleReconnect(1006);
        return;
      }
      setPhase("");
      showError("Impossible d'ouvrir la session : " + (e.message || e));
      setLiveState("off");
      setButtonsForActive(false);
      return;
    }

    sessionId = data.session_id || null;
    var wsUrl = data.ws_url;
    if (!wsUrl) {
      setPhase("");
      showError("Réponse serveur incomplète (ws_url manquant).");
      setLiveState("off");
      setButtonsForActive(false);
      return;
    }

    // L'ordre est déposé côté serveur ; l'agent le récupère à son prochain
    // sondage (≤ 8 s) puis se connecte au relais.
    setPhase("Ordre transmis au poste — en attente de sa connexion (jusqu'à 8 s)…");
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
      gotFrameThisConn = false;
      setPhase("Relais connecté — en attente de la première image…");
      // Garde-fou : si l'agent ne se connecte jamais (poste éteint, aucune session
      // ouverte, relais inatteignable), on diagnostique au lieu d'attendre à vide.
      clearNoFrameTimer();
      noFrameTimer = setTimeout(diagnoseNoFrame,
        reconnectAttempt > 0 ? NO_FRAME_RECONNECT_MS : NO_FRAME_TIMEOUT_MS);
      bindInputs();
      startFpsCounter();
      startPing();
      // Applique le mode de fluidité choisi (qualité + cadence + largeur) puis
      // demande une keyframe pleine trame.
      applyPreset(currentPreset);
      // Demande explicitement la liste des écrans : l'agent ne l'envoie qu'une
      // fois, à sa connexion, et le relais la JETTE si notre socket n'est pas
      // encore appariée — d'où un sélecteur d'écran absent une fois sur deux.
      sendInput({ t: "request_monitors" });
    };

    ws.onmessage = function (ev) {
      if (typeof ev.data === "string") {
        handleTextMessage(ev.data);
      } else {
        handleBinaryFrame(ev.data);
      }
    };

    ws.onerror = function () {
      // Toujours suivi de onclose : c'est lui qui décide (reconnexion ou abandon).
      // Afficher une erreur ici parasiterait le message de reconnexion.
    };

    ws.onclose = function (ev) {
      var code = ev ? ev.code : 0;
      var normal = (code === 1000 || code === 1005);
      clearNoFrameTimer();

      // Coupure inattendue → on reconnecte automatiquement (budget limité).
      if (!userStopped && !normal && reconnectAttempt < MAX_RECONNECT) {
        teardownTransport();
        scheduleReconnect(code);
        return;
      }

      teardown();
      if (!userStopped && !normal) {
        showError("Session interrompue (" + closeReason(code) + ") après "
          + MAX_RECONNECT + " tentatives de reconnexion. Réessayez « Prendre la main ».");
      }
    };
  }

  // ---------------------------------------------------------------------------
  // Réception : trames binaires
  // ---------------------------------------------------------------------------
  function handleBinaryFrame(buffer) {
    if (!buffer || buffer.byteLength < 8) return;
    var dv = new DataView(buffer);
    var version = dv.getUint8(0);
    var frameType = dv.getUint8(1);
    if (version !== 0x01) return;
    // Une image d'écran = la liaison est réellement opérationnelle (annule la
    // garde « pas d'image » et remet à zéro le budget de reconnexion).
    if (frameType === 0x00 || frameType === 0x02) noteFrameReceived();
    if (frameType === 0x00) handleFullFrame(dv, buffer);       // trame pleine (keyframe)
    else if (frameType === 0x02) handleTiledFrame(dv, buffer); // trame tuilée (delta)
    else if (frameType === 0x10) handleAudioFrame(dv, buffer); // son système (PCM)
    else if (frameType === 0x20) handleFileChunk(dv, buffer);  // chunk de download
    // autres types : ignorés (compat ascendante).
  }

  // Chunk de DOWNLOAD : [version][type=0x20][id u32][seq u32][flags u8] + octets.
  // On accumule par id ; au dernier chunk (flag bit0), on assemble et télécharge.
  function handleFileChunk(dv, buffer) {
    if (buffer.byteLength < 11) return;
    var id = dv.getUint32(2, true);
    var flags = dv.getUint8(10);
    var payload = new Uint8Array(buffer, 11);
    var d = fsDownloads[id];
    if (!d) return; // transfert inconnu / annulé
    if (payload.length) { d.chunks.push(payload); d.received += payload.length; }
    if (d.size) fsStatus("Téléchargement de « " + d.name + " » — " + Math.round(d.received / d.size * 100) + " %");
    if (flags & 0x01) {
      try {
        var blob = new Blob(d.chunks, { type: "application/octet-stream" });
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = d.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { try { URL.revokeObjectURL(a.href); } catch (e) { /* ignore */ } }, 15000);
        fsStatus("« " + d.name + " » téléchargé (" + fsHumanSize(d.received) + ").");
      } catch (e) {
        fsStatus("Échec de l'assemblage du fichier.");
      }
      delete fsDownloads[id];
    }
  }

  // Trame AUDIO : [version][type=0x10][rate u32][channels u8][flags u8] + PCM int16 mono.
  // Lecture via Web Audio : on planifie chaque bloc à la suite (tampon anti-jitter).
  function handleAudioFrame(dv, buffer) {
    if (!audioOn || !audioCtx) return;
    var rate = dv.getUint32(2, true);
    if (!rate) return;
    var pcm = new Int16Array(buffer, 8);
    if (!pcm.length) return;
    var f32 = new Float32Array(pcm.length);
    for (var i = 0; i < pcm.length; i++) f32[i] = pcm[i] / 32768;
    try {
      var buf = audioCtx.createBuffer(1, f32.length, rate);
      buf.getChannelData(0).set(f32);
      var src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioGain);
      var now = audioCtx.currentTime;
      // Si on a pris du retard (sous-alimentation), on resynchronise avec un peu de marge.
      if (audioPlayTime < now + 0.01) audioPlayTime = now + AUDIO_JITTER;
      src.start(audioPlayTime);
      audioPlayTime += buf.duration;
    } catch (e) { /* bloc illisible : ignoré */ }
  }

  function ensureAudioContext() {
    if (audioCtx) return true;
    var Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return false;
    try {
      audioCtx = new Ctx();
      audioGain = audioCtx.createGain();
      audioGain.gain.value = 1.0;
      audioGain.connect(audioCtx.destination);
    } catch (e) { audioCtx = null; return false; }
    return true;
  }

  function stopAudioPlayback() {
    audioPlayTime = 0;
    if (audioCtx) {
      try { audioCtx.suspend(); } catch (e) { /* ignore */ }
    }
  }

  // Trame PLEINE : [version][type=0x00][w u16][h u16][monitor u8][flags u8] + JPEG.
  function handleFullFrame(dv, buffer) {
    var width = dv.getUint16(2, true);
    var height = dv.getUint16(4, true);
    var monitor = dv.getUint8(6);

    currentMonitor = monitor;
    if (elMonitor) elMonitor.textContent = (monitor + 1);

    // La keyframe (re)dimensionne le canvas à la résolution annoncée.
    if (width && height && (canvasW !== width || canvasH !== height)) {
      canvasW = width; canvasH = height;
      elCanvas.width = width;
      elCanvas.height = height;
    }

    var jpegBytes = new Uint8Array(buffer, 8);
    var blob = new Blob([jpegBytes], { type: "image/jpeg" });
    createImageBitmap(blob).then(function (bitmap) {
      try { ctx.drawImage(bitmap, 0, 0, elCanvas.width, elCanvas.height); } catch (e) { /* ignore */ }
      bitmap.close && bitmap.close();
      onFrameRendered();
    }).catch(function () { /* trame illisible : ignorée */ });
  }

  // Trame TUILÉE : en-tête 10 octets + N×([x u16][y u16][w u16][h u16][len u32]+JPEG).
  // On ne redessine QUE les régions modifiées (le reste du canvas est conservé).
  function handleTiledFrame(dv, buffer) {
    var width = dv.getUint16(2, true);
    var height = dv.getUint16(4, true);
    var monitor = dv.getUint8(6);
    var tileCount = dv.getUint16(8, true);

    currentMonitor = monitor;
    if (elMonitor) elMonitor.textContent = (monitor + 1);

    // Si le canvas ne correspond pas (pas encore de keyframe à cette résolution),
    // on demande une trame pleine et on ignore ce delta.
    if (width && height && (canvasW !== width || canvasH !== height)) {
      sendInput({ t: "request_keyframe" });
      return;
    }

    var offset = 10;
    for (var i = 0; i < tileCount; i++) {
      if (offset + 12 > buffer.byteLength) break;
      var tx = dv.getUint16(offset, true);
      var ty = dv.getUint16(offset + 2, true);
      // var tw = dv.getUint16(offset + 4, true); // dimensions portées par le bitmap
      // var th = dv.getUint16(offset + 6, true);
      var len = dv.getUint32(offset + 8, true);
      offset += 12;
      if (offset + len > buffer.byteLength) break;
      var jpeg = new Uint8Array(buffer, offset, len);
      offset += len;
      var blob = new Blob([jpeg], { type: "image/jpeg" });
      (function (px, py) {
        createImageBitmap(blob).then(function (bitmap) {
          try { ctx.drawImage(bitmap, px, py); } catch (e) { /* ignore */ }
          bitmap.close && bitmap.close();
        }).catch(function () { /* tuile illisible : ignorée */ });
      })(tx, ty);
    }
    onFrameRendered();
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
    } else if (msg.t === "cursor") {
      updateCursor(msg.x, msg.y, msg.v);
    } else if (msg.t === "lock_state") {
      // Confirmation agent : la saisie locale est (dé)verrouillée.
      lockInputOn = !!msg.on;
      setToggle(elLockInput, lockInputOn);
    } else if (msg.t === "privacy_state") {
      // Confirmation agent : voile noir (in)actif ; ok=false si non supporté.
      privacyOn = !!msg.on;
      setToggle(elPrivacy, privacyOn);
      if (!msg.ok && window.TS && TS.toast) {
        TS.toast("Écran de confidentialité indisponible sur ce poste (Windows 10 2004+ requis).", "error");
      }
    } else if (msg.t === "audio_state") {
      // Confirmation agent : écoute (in)active ; ok=false si indisponible.
      audioOn = !!msg.on;
      setToggle(elAudio, audioOn);
      if (!audioOn) stopAudioPlayback();
      if (!msg.ok && window.TS && TS.toast) {
        TS.toast("Écoute audio indisponible (session ouverte requise sur le poste).", "error");
      }
    // --- Transfert de fichiers ---
    } else if (msg.t === "fs_roots") {
      fsRenderRoots(msg.list || []);
    } else if (msg.t === "fs_listing") {
      fsRenderListing(msg);
    } else if (msg.t === "fs_download_start") {
      fsDownloads[msg.id] = { name: msg.name || "fichier", size: msg.size || 0, received: 0, chunks: [] };
      fsStatus("Téléchargement de « " + (msg.name || "fichier") + " »…");
    } else if (msg.t === "fs_upload_ready") {
      fsSendUploadChunks(msg.id);
    } else if (msg.t === "fs_done") {
      if (msg.dir === "up") {
        fsStatus("« " + (msg.name || "fichier") + " » envoyé.");
        fsActiveUpload = null;
        fsStartNextUpload();
      }
    } else if (msg.t === "fs_error") {
      var em = FS_ERR[msg.code] || ("Erreur (" + (msg.code || "?") + ")");
      fsStatus(em);
      if (window.TS && TS.toast) TS.toast("Fichiers : " + em, "error");
      // Si l'erreur concerne l'upload courant, on libère la file.
      if (fsActiveUpload && (msg.id === fsActiveUpload.id || msg.code === "busy" || msg.code === "unattended")) {
        fsActiveUpload = null;
        fsStartNextUpload();
      }
      if (msg.id && fsDownloads[msg.id]) delete fsDownloads[msg.id];
    } else if (msg.t === "clip") {
      // Presse-papiers du poste reçu : on le montre ET on tente de le copier
      // localement (writeText est autorisé sur geste utilisateur, ce qui est le cas).
      // Le panneau doit être visible : le repli execCommand("copy") ne peut pas
      // sélectionner un champ masqué.
      if (!clipOpen && elClipPanel) {
        clipOpen = true;
        elClipPanel.classList.remove("hidden");
        setToggle(elClip, true);
      }
      if (elRcText) elRcText.value = msg.text || "";
      var n = (msg.text || "").length;
      if (!n) {
        clipStatus("Le presse-papiers du poste est vide (ou ne contient pas de texte).");
      } else {
        copyToLocalClipboard(msg.text);
      }
    } else if (msg.t === "clip_ok") {
      clipStatus("Envoyé au poste (" + (msg.n || 0) + " caractères).");
    } else if (msg.t === "clip_error") {
      var cm = CLIP_ERR[msg.code] || ("Erreur (" + (msg.code || "?") + ")");
      clipStatus(cm);
      if (window.TS && TS.toast) TS.toast("Presse-papiers : " + cm, "error");
    }
  }

  // ---------------------------------------------------------------------------
  // Presse-papiers partagé (texte)
  // ---------------------------------------------------------------------------
  function clipStatus(msg) {
    if (elRcStatus) elRcStatus.textContent = msg || "";
  }

  // Copie vers le presse-papiers LOCAL. navigator.clipboard exige un contexte
  // sécurisé (https) ; on garde un repli execCommand + sélection du textarea.
  function copyToLocalClipboard(text) {
    function fallback() {
      if (!elRcText) return false;
      try {
        elRcText.focus();
        elRcText.select();
        var ok = document.execCommand && document.execCommand("copy");
        return !!ok;
      } catch (e) { return false; }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        clipStatus("Presse-papiers du poste copié chez vous (" + text.length + " caractères).");
      }).catch(function () {
        clipStatus(fallback()
          ? "Copié chez vous (" + text.length + " caractères)."
          : "Texte récupéré ci-dessous — copiez-le manuellement (Ctrl+C).");
      });
      return;
    }
    clipStatus(fallback()
      ? "Copié chez vous (" + text.length + " caractères)."
      : "Texte récupéré ci-dessous — copiez-le manuellement (Ctrl+C).");
  }

  function setupClipboard() {
    if (!elClip || !elClipPanel) return;

    elClip.addEventListener("click", function () {
      clipOpen = !clipOpen;
      elClipPanel.classList.toggle("hidden", !clipOpen);
      setToggle(elClip, clipOpen);
      if (clipOpen) {
        clipStatus("");
        if (elRcText) elRcText.focus();
      }
    });

    // ↓ Récupérer : demande le presse-papiers du poste (réponse : message "clip").
    if (elRcPull) elRcPull.addEventListener("click", function () {
      if (!ws || ws.readyState !== WebSocket.OPEN) { clipStatus("Aucune session active."); return; }
      clipStatus("Lecture du presse-papiers du poste…");
      sendInput({ t: "clip_get" });
    });

    // ↑ Envoyer : écrit le contenu du champ dans le presse-papiers du poste.
    if (elRcPush) elRcPush.addEventListener("click", function () {
      if (!ws || ws.readyState !== WebSocket.OPEN) { clipStatus("Aucune session active."); return; }
      var txt = elRcText ? elRcText.value : "";
      if (!txt) { clipStatus("Rien à envoyer : le champ est vide."); return; }
      clipStatus("Envoi…");
      sendInput({ t: "clip_set", text: txt });
    });

    // « Coller le mien » : confort quand le navigateur autorise la LECTURE du
    // presse-papiers (Chrome/Edge sur geste utilisateur). Firefox la refuse :
    // dans ce cas on invite simplement à coller à la main (Ctrl+V), ce qui
    // fonctionne toujours — d'où le champ de saisie au centre du panneau.
    if (elRcMine) elRcMine.addEventListener("click", function () {
      if (!navigator.clipboard || !navigator.clipboard.readText) {
        clipStatus("Votre navigateur n'autorise pas la lecture automatique : collez dans le champ (Ctrl+V).");
        if (elRcText) elRcText.focus();
        return;
      }
      navigator.clipboard.readText().then(function (t) {
        if (elRcText) elRcText.value = t || "";
        clipStatus(t ? "Votre presse-papiers est prêt à être envoyé." : "Votre presse-papiers est vide.");
      }).catch(function () {
        clipStatus("Lecture refusée par le navigateur : collez dans le champ (Ctrl+V).");
        if (elRcText) elRcText.focus();
      });
    });
  }

  // Referme et vide le panneau (fin de session : ne pas laisser de texte traîner).
  function clipResetState() {
    clipOpen = false;
    if (elClipPanel) elClipPanel.classList.add("hidden");
    setToggle(elClip, false);
    if (elRcText) elRcText.value = "";
    clipStatus("");
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
    // Arrêt VOLONTAIRE : interdit la reconnexion automatique.
    userStopped = true;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    clearNoFrameTimer();
    if (ws) {
      try { ws.close(1000, "viewer_end"); } catch (e) { /* ignore */ }
    } else {
      teardown();
    }
  }

  function teardown() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    clearNoFrameTimer();
    setPhase("");
    gotFrameThisConn = false;
    stopFpsCounter();
    stopPing();
    ws = null;
    sessionId = null;
    setLiveState("off");
    setButtonsForActive(false);
    if (elFps) elFps.textContent = "0";
    if (elLatency) elLatency.textContent = "—";
    elScreen.classList.remove("has-stream");
    if (elCursor) elCursor.classList.add("hidden");
    // Libère complètement l'audio (contexte fermé → périphérique relâché).
    audioOn = false;
    if (audioCtx) { try { audioCtx.close(); } catch (e) { /* ignore */ } audioCtx = null; audioGain = null; }
    audioPlayTime = 0;
    // Efface le canvas.
    try { ctx.fillStyle = "#05080a"; ctx.fillRect(0, 0, elCanvas.width, elCanvas.height); } catch (e) { /* ignore */ }
    if (document.fullscreenElement) {
      try { document.exitFullscreen(); } catch (e) { /* ignore */ }
    }
  }

  // ---------------------------------------------------------------------------
  // Branchements boutons
  // ---------------------------------------------------------------------------
  // Wrapper OBLIGATOIRE : branché directement, l'événement de clic serait passé
  // comme argument `isRetry` (objet ⇒ truthy) et le démarrage manuel serait pris
  // pour une reconnexion (boutons et compteur non réinitialisés).
  elStart.addEventListener("click", function () { startSession(false); });
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

  // Capture instantanée : télécharge l'image courante du canvas en PNG.
  function takeScreenshot() {
    if (!elCanvas.width || !elCanvas.height) return;
    try {
      elCanvas.toBlob(function (blob) {
        if (!blob) return;
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        var hostEl = document.getElementById("title-hostname");
        var host = (hostEl ? hostEl.textContent : "poste").trim().replace(/\s+/g, "_") || "poste";
        var ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
        a.href = url;
        a.download = "truesight_" + host + "_" + ts + ".png";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
      }, "image/png");
    } catch (e) { /* ignore */ }
  }
  if (elShot) elShot.addEventListener("click", function () { if (!elShot.disabled) takeScreenshot(); });

  // --- Contrôle exclusif (verrou saisie / SAS / confidentialité / lock sortie) ---
  if (elLockInput) elLockInput.addEventListener("click", function () {
    if (elLockInput.disabled) return;
    lockInputOn = !lockInputOn;
    setToggle(elLockInput, lockInputOn);    // optimiste ; l'agent confirme via lock_state
    sendInput({ t: "lock_input", on: lockInputOn });
  });
  if (elSas) elSas.addEventListener("click", function () {
    if (elSas.disabled) return;
    sendInput({ t: "send_sas" });
  });
  if (elPrivacy) elPrivacy.addEventListener("click", function () {
    if (elPrivacy.disabled) return;
    privacyOn = !privacyOn;
    setToggle(elPrivacy, privacyOn);        // l'agent confirme/infirme via privacy_state
    sendInput({ t: "privacy", on: privacyOn });
  });
  if (elLockExit) elLockExit.addEventListener("click", function () {
    if (elLockExit.disabled) return;
    lockExitOn = !lockExitOn;
    setToggle(elLockExit, lockExitOn);
    sendInput({ t: "lock_on_disconnect", on: lockExitOn });
  });
  if (elAudio) elAudio.addEventListener("click", function () {
    if (elAudio.disabled) return;
    var want = !audioOn;
    if (want) {
      // Le contexte audio doit naître d'un geste utilisateur (ce clic).
      if (!ensureAudioContext()) {
        if (window.TS && TS.toast) TS.toast("Audio non supporté par ce navigateur.", "error");
        return;
      }
      try { audioCtx.resume(); } catch (e) { /* ignore */ }
      audioPlayTime = 0;
    } else {
      stopAudioPlayback();
    }
    audioOn = want;                  // optimiste ; l'agent confirme via audio_state
    setToggle(elAudio, audioOn);
    sendInput({ t: "audio", on: audioOn });
  });

  // Presse-papiers partagé : branchement des boutons du panneau.
  setupClipboard();

  // ---------------------------------------------------------------------------
  // Transfert de fichiers (explorateur in-session)
  // ---------------------------------------------------------------------------
  function fsNextId() {
    fsXferId = (fsXferId + 1) >>> 0;
    if (fsXferId === 0) fsXferId = 1;
    return fsXferId;
  }
  function fsStatus(text) { if (elRfStatus) elRfStatus.textContent = text || ""; }
  function fsEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fsHumanSize(b) {
    if (b == null) return "";
    if (b < 1024) return b + " o";
    if (b < 1024 * 1024) return (b / 1024).toFixed(0) + " Ko";
    if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + " Mo";
    return (b / 1024 / 1024 / 1024).toFixed(2) + " Go";
  }
  function fsJoin(dir, name) {
    if (!dir) return name;
    return dir.charAt(dir.length - 1) === "\\" ? dir + name : dir + "\\" + name;
  }
  function fsSleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function u8ToB64(u8) {
    var CH = 0x8000, s = "";
    for (var i = 0; i < u8.length; i += CH) {
      s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
    }
    return btoa(s);
  }
  function fsAudit(direction, name, size, path) {
    try {
      fetch("/api/v1/agents/" + AGENT_ID + "/remote-file-audit", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ direction: direction, name: name, size: size, path: path }),
      });
    } catch (e) { /* audit best-effort */ }
  }

  function fsList(path) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    sendInput({ t: "fs_list", path: path });
  }
  function fsDownload(path, name) {
    sendInput({ t: "fs_download", id: fsNextId(), path: path });
    fsStatus("Téléchargement de « " + name + " »…");
    fsAudit("down", name, null, path);
  }

  function fsRenderRoots(list) {
    if (!elRfRoots) return;
    elRfRoots.innerHTML = (list || []).map(function (r) {
      return '<button type="button" class="btn xs rf-root" data-path="' + fsEsc(r.path) + '">' + fsEsc(r.label) + "</button>";
    }).join("");
  }
  function fsRenderListing(msg) {
    fsCurrentPath = msg.path || null;
    fsParentPath = msg.parent || null;
    if (elRfPath) { elRfPath.textContent = fsCurrentPath || "—"; elRfPath.title = fsCurrentPath || ""; }
    if (elRfUp) elRfUp.disabled = !fsParentPath;
    var entries = msg.entries || [];
    if (!elRfList) return;
    if (!entries.length) {
      elRfList.innerHTML = '<div class="dl-loading">Dossier vide.</div>';
      return;
    }
    elRfList.innerHTML = entries.map(function (e) {
      var icon = e.is_dir ? "#i-folder" : "#i-box";
      var meta = e.is_dir ? (e.mtime || "") : (fsHumanSize(e.size) + (e.mtime ? " · " + e.mtime : ""));
      return '<div class="rf-row' + (e.is_dir ? " dir" : "") + '" data-name="' + fsEsc(e.name) + '" data-isdir="' + (e.is_dir ? "1" : "0") + '">' +
        '<svg><use href="' + icon + '"/></svg>' +
        '<span class="rf-name">' + fsEsc(e.name) + "</span>" +
        '<span class="rf-meta">' + fsEsc(meta) + "</span></div>";
    }).join("");
  }

  function fsQueueUploads(fileList) {
    if (!fsCurrentPath) { fsStatus("Choisissez d'abord un dossier de destination."); return; }
    for (var i = 0; i < fileList.length; i++) fsUploadQueue.push(fileList[i]);
    if (!fsActiveUpload) fsStartNextUpload();
  }
  function fsStartNextUpload() {
    if (fsActiveUpload) return;
    var file = fsUploadQueue.shift();
    if (!file) { if (fsCurrentPath) fsList(fsCurrentPath); return; }  // file vide → refresh
    var id = fsNextId();
    fsActiveUpload = { id: id, file: file, name: file.name };
    sendInput({ t: "fs_upload_start", id: id, name: file.name, dir: fsCurrentPath, size: file.size });
    fsStatus("Préparation de « " + file.name + " »…");
    fsAudit("up", file.name, file.size, fsCurrentPath);
  }
  async function fsSendUploadChunks(id) {
    var up = fsActiveUpload;
    if (!up || up.id !== id) return;
    var file = up.file, offset = 0, seq = 0;
    try {
      if (file.size === 0) {
        sendInput({ t: "fs_upload_chunk", id: id, seq: 0, data: "", last: true });
        return;
      }
      while (offset < file.size) {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        // Backpressure : on attend que le tampon d'envoi se vide.
        while (ws && ws.bufferedAmount > FS_BUF_MAX) { await fsSleep(30); }
        var end = Math.min(offset + FS_UP_CHUNK, file.size);
        var buf = await file.slice(offset, end).arrayBuffer();
        var last = end >= file.size;
        sendInput({ t: "fs_upload_chunk", id: id, seq: seq, data: u8ToB64(new Uint8Array(buf)), last: last });
        offset = end; seq++;
        fsStatus("Envoi de « " + up.name + " » — " + Math.round(offset / file.size * 100) + " %");
      }
    } catch (e) {
      fsStatus("Échec de l'envoi de « " + up.name + " ».");
      fsActiveUpload = null;
      fsStartNextUpload();
    }
  }

  function fsTogglePanel(open) {
    filesOpen = (open === undefined) ? !filesOpen : open;
    if (elFilesPanel) elFilesPanel.classList.toggle("hidden", !filesOpen);
    setToggle(elFiles, filesOpen);
    if (filesOpen && !fsCurrentPath) sendInput({ t: "fs_roots" });
  }
  function fsResetState() {
    filesOpen = false;
    fsCurrentPath = null; fsParentPath = null;
    fsDownloads = {}; fsUploadQueue = []; fsActiveUpload = null;
    if (elFilesPanel) elFilesPanel.classList.add("hidden");
    setToggle(elFiles, false);
    if (elRfList) elRfList.innerHTML = '<div class="dl-loading">Choisissez un emplacement pour explorer le poste.</div>';
    if (elRfRoots) elRfRoots.innerHTML = "";
    if (elRfPath) elRfPath.textContent = "—";
    if (elRfUp) elRfUp.disabled = true;
    fsStatus("");
  }

  if (elFiles) elFiles.addEventListener("click", function () {
    if (elFiles.disabled) return;
    fsTogglePanel();
  });
  if (elRfUp) elRfUp.addEventListener("click", function () { if (fsParentPath) fsList(fsParentPath); });
  var elRfRefresh = document.getElementById("rf-refresh");
  if (elRfRefresh) elRfRefresh.addEventListener("click", function () { if (fsCurrentPath) fsList(fsCurrentPath); });
  if (elRfRoots) elRfRoots.addEventListener("click", function (e) {
    var b = e.target.closest(".rf-root");
    if (b) fsList(b.getAttribute("data-path"));
  });
  if (elRfList) elRfList.addEventListener("click", function (e) {
    var row = e.target.closest(".rf-row");
    if (!row) return;
    var name = row.getAttribute("data-name");
    var full = fsJoin(fsCurrentPath, name);
    if (row.getAttribute("data-isdir") === "1") fsList(full);
    else fsDownload(full, name);
  });
  if (elRfUpload) elRfUpload.addEventListener("change", function () {
    if (this.files && this.files.length) fsQueueUploads(this.files);
    this.value = "";
  });
  if (elFilesPanel) {
    elFilesPanel.addEventListener("dragover", function (e) {
      e.preventDefault();
      elFilesPanel.classList.add("drop");
    });
    elFilesPanel.addEventListener("dragleave", function (e) {
      if (e.target === elFilesPanel) elFilesPanel.classList.remove("drop");
    });
    elFilesPanel.addEventListener("drop", function (e) {
      e.preventDefault();
      elFilesPanel.classList.remove("drop");
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        fsQueueUploads(e.dataTransfer.files);
      }
    });
  }

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
