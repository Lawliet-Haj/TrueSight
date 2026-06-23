"""Session « Bureau à distance » côté agent : client WebSocket synchrone.

Architecture (volontairement simple, sans asyncio côté agent) :
  - un **thread d'envoi** itère le générateur de trames de ``capture`` et les
    pousse en binaire sur la WebSocket (agent → viewer) ;
  - la **boucle principale** reçoit les messages texte JSON (viewer → agent) et
    les applique : entrées souris/clavier via ``inject``, contrôles
    set_quality / request_keyframe / set_monitor sur le ``ScreenCapturer``.

Transport : ``websocket-client`` (``websocket.create_connection``). On reste en
mode bloquant : la lib gère le framing WebSocket, le TLS (wss) et le ping/pong.

Robustesse : la session ne crashe jamais l'agent. Toute exception est
journalisée ; la déconnexion d'un côté provoque l'arrêt propre de l'autre (le
relais ferme de toute façon la session côté serveur).

Cette classe est utilisée :
  - directement en mode console (agent en session utilisateur), et
  - par le helper lancé dans la session interactive (``remote-helper``) quand
    l'agent tourne en service SYSTEM.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time

from . import capture as capture_mod
from . import inject as inject_mod

_logger = logging.getLogger("truesight.remote.session")

# Import tolérant de websocket-client (la session échoue proprement si absent).
try:
    import websocket  # type: ignore  (paquet « websocket-client »)
    _WS_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    websocket = None  # type: ignore
    _WS_AVAILABLE = False
    _logger.warning("websocket-client indisponible (%s) : bureau à distance inopérant.", _exc)

# Délai d'établissement de la connexion wss (secondes).
_CONNECT_TIMEOUT = 15
# Durée de vie maximale d'une session sans démantèlement explicite (garde-fou).
_MAX_SESSION_SECONDS = 60 * 60  # 1 h


class RemoteSession:
    """Pilote une session de bureau à distance jusqu'à sa fermeture."""

    def __init__(self, token: str, ws_url: str, verify_tls: bool = True,
                 desktop_follow: bool = False) -> None:
        self.token = token
        self.ws_url = ws_url
        self.verify_tls = verify_tls
        # Mode NON-ASSISTÉ : la capture ET l'injection suivent le bureau d'entrée
        # actif (Default ↔ Winlogon). Activé par le helper SYSTEM (--unattended).
        self._desktop_follow = desktop_follow
        self._inject_desk: str | None = None  # dernier bureau attaché côté injection
        self._stop = threading.Event()
        self._ws = None
        self._send_thread: threading.Thread | None = None
        self._capturer = capture_mod.ScreenCapturer()
        self._injector = inject_mod.InputInjector(self._capturer.current_monitor_geometry())
        # Sérialise les envois (le thread capture et d'éventuels acks ne se chevauchent pas).
        self._send_lock = threading.Lock()
        self._started_at = 0.0
        # Thread « curseur » : remonte la position du curseur au viewer (surcouche).
        self._cursor_thread: threading.Thread | None = None
        # Contrôle exclusif (piloté par le viewer) :
        self._input_locked = False            # saisie physique locale bloquée (BlockInput)
        self._lock_on_disconnect = False      # verrouiller le poste en fin de session
        self._privacy = None                  # PrivacyScreen (voile noir local) ou None
        # Écoute audio (son système du poste) : capture à la demande du viewer.
        self._audio = None                    # AudioCapture ou None
        self._audio_thread: threading.Thread | None = None
        self._audio_on = False
        # Transfert de fichiers (pendant la session) :
        self._file_send_thread: threading.Thread | None = None  # download en cours
        self._file_cancel: set = set()        # ids de download annulés
        self._uploads: dict = {}              # id -> {fh, tmp, final, received}

    # -- Cycle de vie ---------------------------------------------------------
    def stop(self) -> None:
        """Demande l'arrêt propre de la session."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        if self._started_at and (time.monotonic() - self._started_at) > _MAX_SESSION_SECONDS:
            _logger.info("Durée maximale de session atteinte, fermeture.")
            return True
        return False

    # -- Connexion ------------------------------------------------------------
    def _connect(self) -> bool:
        """Établit la WebSocket vers le relais (chemin /ws/remote/agent)."""
        if not _WS_AVAILABLE:
            _logger.error("Connexion impossible : websocket-client absent.")
            return False
        try:
            sslopt = None
            if self.ws_url.lower().startswith("wss://") and not self.verify_tls:
                # Mode dev : on ne vérifie pas le certificat (aligné sur verify_tls).
                import ssl
                sslopt = {"cert_reqs": ssl.CERT_NONE}
            self._ws = websocket.create_connection(
                self.ws_url,
                timeout=_CONNECT_TIMEOUT,
                sslopt=sslopt,
                enable_multithread=True,  # envoi (thread) + réception (boucle) en parallèle.
            )
            # Au-delà du handshake, on veut une réception bloquante mais réveillable.
            self._ws.settimeout(1.0)
            _logger.info("Connecté au relais (agent) : %s", _redact(self.ws_url))
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.error("Connexion au relais impossible : %s", exc)
            return False

    # -- Boucle d'envoi des trames (thread) -----------------------------------
    def _send_loop(self) -> None:
        """Capture en continu et envoie les trames binaires (agent → viewer)."""
        _logger.info("Flux de capture démarré.")
        try:
            for frame in self._capturer.frames(self._should_stop):
                if self._should_stop():
                    break
                if not self._send_binary(frame):
                    break
        except Exception as exc:  # noqa: BLE001
            _logger.error("Boucle d'envoi interrompue : %s", exc)
        finally:
            _logger.info("Flux de capture arrêté.")
            # Si la capture s'arrête, on termine toute la session.
            self._stop.set()

    def _send_loop_unattended(self) -> None:
        """Capture NON-ASSISTÉE : suit le bureau d'entrée actif (Default ↔ Winlogon).

        Privilégie **DXGI Desktop Duplication** (``capture_dxgi``) : c'est le SEUL
        moyen de capturer le **bureau sécurisé** (écran de connexion / verrouillage
        / UAC) — GDI/BitBlt (``mss``) y renvoie une image NOIRE. Repli automatique
        sur ``mss`` si DXGI est indisponible (le bureau sécurisé y restera noir,
        mais le bureau utilisateur reste capturable).

        Contrainte DXGI (cf. ``capture_dxgi``) : la caméra ne peut être ni recréée
        ni libérée dans le process (crash COM). On en crée donc UNE, on la
        réutilise, et à la moindre bascule de bureau/écran on ARRÊTE la session :
        le viewer se reconnecte et un nouveau helper repart sur le bon bureau. Le
        helper sort ensuite via ``os._exit()`` (cf. ``__main__``).
        """
        from . import desktop as desk
        try:
            from . import capture_dxgi
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Module DXGI introuvable (%s) : repli mss.", exc)
            capture_dxgi = None  # type: ignore

        if capture_dxgi is None or not capture_dxgi.is_available():
            _logger.info(
                "DXGI indisponible : capture non-assistée via mss (GDI) — "
                "le bureau sécurisé (écran de connexion) restera NOIR."
            )
            self._send_loop_unattended_mss()
            return

        _logger.info("Flux de capture NON-ASSISTÉ démarré (DXGI Desktop Duplication).")
        try:
            # Attache le thread au bureau d'entrée AVANT de créer la duplication
            # DXGI : la capture est liée au bureau actif au moment de l'appel. Au
            # démarrage (boot/logon), ce bureau peut n'être pas prêt : on patiente.
            desk_name = None
            for _ in range(40):  # ~20 s max.
                if self._should_stop():
                    return
                desk_name = desk.attach_thread_to_input_desktop()
                if desk_name is not None:
                    break
                time.sleep(0.5)
            if desk_name is None:
                _logger.error("Bureau d'entrée inaccessible : repli mss.")
                self._send_loop_unattended_mss()
                return

            mon_idx = self._capturer.monitor_index
            cam = capture_dxgi.create(mon_idx)
            if cam is None:
                _logger.error("Caméra DXGI indisponible : repli mss.")
                self._send_loop_unattended_mss()
                return
            _logger.info("Capture DXGI active sur le bureau « %s » (écran %d).", desk_name, mon_idx)

            # Dernière trame brute (raw, w, h) : permet de re-servir une keyframe
            # (connexion viewer, changement de qualité/largeur) même si DXGI ne
            # renvoie rien (écran statique → grab() == None).
            last = None
            while not self._should_stop():
                loop_start = time.monotonic()
                # Bascule de bureau (login terminé, UAC, verrouillage) ou changement
                # d'écran : on NE recrée PAS la duplication (crash) → fin de session,
                # le viewer se reconnecte sur un helper neuf attaché au bon bureau.
                if desk.current_input_desktop_name() != desk_name:
                    _logger.info("Bascule de bureau (%s → autre) : fin de session (reconnexion attendue).", desk_name)
                    break
                if self._capturer.monitor_index != mon_idx:
                    _logger.info("Changement d'écran demandé : fin de session (reconnexion attendue).")
                    break

                got = capture_dxgi.grab(cam)
                keyframe = self._capturer._force_keyframe
                self._capturer._force_keyframe = False
                if got is not None:
                    last = got
                elif keyframe and last is not None:
                    # Rien de neuf, mais le viewer réclame une image → re-sert la dernière.
                    got = last
                if got is not None:
                    raw, width, height = got
                    raw, width, height = capture_mod._downscale_bgra(
                        raw, width, height, self._capturer.max_width
                    )
                    jpeg = capture_mod._encode_jpeg(raw, width, height, self._capturer.quality)
                    if jpeg is not None:
                        frame = capture_mod.build_frame(jpeg, width, height, mon_idx)
                        if not self._send_binary(frame):
                            break
                elapsed = time.monotonic() - loop_start
                remaining = (1.0 / self._capturer.target_fps) - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Boucle non-assistée (DXGI) interrompue : %s", exc)
        finally:
            _logger.info("Flux de capture non-assisté (DXGI) arrêté.")
            self._stop.set()

    def _send_loop_unattended_mss(self) -> None:
        """Repli NON-ASSISTÉ via ``mss`` (GDI) si DXGI est indisponible.

        Ce thread (SYSTEM) s'attache au bureau d'entrée, capture via mss, et
        RE-crée la capture à chaque bascule de bureau (connexion, UAC, verrouillage)
        — un objet ``mss`` est lié au bureau du thread au moment de sa création.
        ATTENTION : GDI renvoie une image NOIRE sur le bureau sécurisé (écran de
        connexion). Ce chemin ne sert que de filet quand DXGI manque.
        """
        from . import desktop as desk
        _logger.info("Flux de capture NON-ASSISTÉ (mss/GDI) démarré (suivi du bureau d'entrée).")
        try:
            while not self._should_stop():
                desk_name = desk.attach_thread_to_input_desktop()
                if desk_name is None:
                    time.sleep(0.5)
                    continue
                _logger.info("Capture attachée au bureau d'entrée : %s", desk_name)
                try:
                    with capture_mod.mss.mss() as sct:
                        self._capturer.request_keyframe()
                        while not self._should_stop():
                            # Bascule de bureau ? (connexion d'un utilisateur, UAC, verrouillage)
                            if desk.current_input_desktop_name() != desk_name:
                                _logger.info("Bascule de bureau détectée (%s → autre) : ré-attachement.", desk_name)
                                break
                            loop_start = time.monotonic()
                            frame = self._capturer._grab_and_encode(sct)
                            if frame is not None and not self._send_binary(frame):
                                self._stop.set()
                                break
                            elapsed = time.monotonic() - loop_start
                            remaining = (1.0 / self._capturer.target_fps) - elapsed
                            if remaining > 0:
                                time.sleep(remaining)
                except Exception as exc:  # noqa: BLE001 - capture jamais fatale.
                    _logger.error("Capture non-assistée interrompue : %s", exc)
                    time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            _logger.error("Boucle non-assistée interrompue : %s", exc)
        finally:
            _logger.info("Flux de capture non-assisté arrêté.")
            self._stop.set()

    def _send_binary(self, data: bytes) -> bool:
        """Envoie une trame binaire ; renvoie False si la WebSocket est tombée."""
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send_binary(data)
            return True
        except Exception as exc:  # noqa: BLE001 - viewer parti / relais fermé.
            _logger.info("Envoi d'une trame impossible (session probablement fermée) : %s", exc)
            return False

    def _send_text(self, obj: dict) -> bool:
        """Envoie un message texte JSON (agent → viewer : confort/latence/écrans)."""
        ws = self._ws
        if ws is None:
            return False
        try:
            with self._send_lock:
                ws.send(json.dumps(obj))
            return True
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi d'un message texte impossible : %s", exc)
            return False

    def _send_metadata(self) -> None:
        """Transmet au viewer les infos de confort : liste des écrans + utilisateur connecté."""
        try:
            self._send_text({"t": "monitors", "list": capture_mod.list_monitors()})
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi de la liste des moniteurs impossible : %s", exc)
        try:
            if self._desktop_follow:
                from . import desktop as desk
                d = (desk.current_input_desktop_name() or "").lower()
                label = "Écran de connexion (non-assisté)" if d == "winlogon" else "Poste — non-assisté (SYSTEM)"
            else:
                label = _current_user_label()
            self._send_text({"t": "user", "name": label})
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Envoi de l'utilisateur impossible : %s", exc)

    # -- Boucle « curseur » (thread) ------------------------------------------
    def _cursor_loop(self) -> None:
        """Remonte la position du curseur distant au viewer (~25x/s).

        Ni mss ni DXGI ne dessinent le curseur : le viewer l'affiche en surcouche
        à partir de cette position, normalisée au moniteur courant. N'émet que sur
        changement (un bureau immobile ne génère donc presque aucun trafic).
        """
        if not self._injector.available:
            return
        # En mode non-assisté, lire le curseur du bureau d'entrée (login compris) :
        # une seule attache suffit (la session se termine si le bureau bascule).
        if self._desktop_follow:
            try:
                from . import desktop as desk
                desk.attach_thread_to_input_desktop()
            except Exception:  # noqa: BLE001
                pass
        interval = 1.0 / 25.0
        last_sent = None
        geo = None
        geo_idx = -1
        geo_at = 0.0
        try:
            while not self._should_stop():
                loop_start = time.monotonic()
                idx = self._capturer.monitor_index
                # Géométrie du moniteur courant : rafraîchie au changement / ~1 s.
                if geo is None or idx != geo_idx or (loop_start - geo_at) > 1.0:
                    geo = self._capturer.current_monitor_geometry()
                    geo_idx = idx
                    geo_at = loop_start
                state = inject_mod.get_cursor_state()
                if state and geo:
                    sx, sy, showing = state
                    mw = max(1, int(geo.get("width", 1)))
                    mh = max(1, int(geo.get("height", 1)))
                    nx = (sx - int(geo.get("left", 0))) / mw
                    ny = (sy - int(geo.get("top", 0))) / mh
                    inside = 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0
                    payload = (round(nx, 4), round(ny, 4), bool(showing and inside))
                    if payload != last_sent:
                        last_sent = payload
                        self._send_text({"t": "cursor", "x": payload[0],
                                         "y": payload[1], "v": payload[2]})
                elapsed = time.monotonic() - loop_start
                remaining = interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Boucle curseur interrompue : %s", exc)

    # -- Contrôle exclusif (verrou saisie / confidentialité) ------------------
    def _set_input_lock(self, on: bool) -> None:
        """Bloque/débloque la saisie physique locale (BlockInput)."""
        if self._desktop_follow:
            self._ensure_inject_desktop()
        if inject_mod.block_input(on):
            self._input_locked = on
            _logger.info("Saisie locale %s.", "verrouillée" if on else "déverrouillée")
        else:
            _logger.info("Verrouillage de la saisie locale indisponible.")
        self._send_text({"t": "lock_state", "on": self._input_locked})

    def _set_privacy(self, on: bool) -> None:
        """Active/retire le voile noir local (écran de confidentialité)."""
        try:
            from . import privacy as privacy_mod
        except Exception as exc:  # noqa: BLE001
            _logger.info("Module confidentialité indisponible : %s", exc)
            self._send_text({"t": "privacy_state", "on": False, "ok": False})
            return
        if on:
            if self._privacy is None:
                self._privacy = privacy_mod.PrivacyScreen()
            ok = self._privacy.start()
            _logger.info("Écran de confidentialité %s.", "activé" if ok else "indisponible")
            self._send_text({"t": "privacy_state", "on": bool(ok), "ok": bool(ok)})
        else:
            if self._privacy is not None:
                self._privacy.stop()
            self._send_text({"t": "privacy_state", "on": False, "ok": True})

    # -- Écoute audio (son système du poste) ----------------------------------
    def _set_audio(self, on: bool) -> None:
        """Démarre/arrête la capture du son système (loopback) à la demande du viewer."""
        if not on:
            self._audio_on = False  # la boucle s'arrête et libère le flux.
            self._send_text({"t": "audio_state", "on": False, "ok": True})
            return
        # Pas de son à l'écran de connexion (helper SYSTEM) : on l'indique au viewer.
        if self._desktop_follow:
            _logger.info("Écoute audio ignorée : session non-assistée (écran de connexion).")
            self._send_text({"t": "audio_state", "on": False, "ok": False})
            return
        if self._audio_on:
            return
        try:
            from . import audio as audio_mod
        except Exception as exc:  # noqa: BLE001
            _logger.info("Module audio indisponible : %s", exc)
            self._send_text({"t": "audio_state", "on": False, "ok": False})
            return
        if not audio_mod.is_available():
            self._send_text({"t": "audio_state", "on": False, "ok": False})
            return
        self._audio = audio_mod.AudioCapture()
        if not self._audio.start():
            self._audio = None
            self._send_text({"t": "audio_state", "on": False, "ok": False})
            return
        self._audio_on = True
        self._audio_thread = threading.Thread(
            target=self._audio_loop, name="truesight-remote-audio", daemon=True
        )
        self._audio_thread.start()
        self._send_text({"t": "audio_state", "on": True, "ok": True})

    def _audio_loop(self) -> None:
        """Lit le son loopback en continu et l'envoie en trames binaires (type 0x10)."""
        from . import audio as audio_mod
        capturer = self._audio
        if capturer is None:
            return
        _logger.info("Flux audio démarré.")
        try:
            while self._audio_on and not self._should_stop():
                pcm = capturer.read_mono()
                if not pcm:
                    continue
                frame = audio_mod.build_audio_frame(pcm, capturer.sample_rate)
                if not self._send_binary(frame):
                    break
        except Exception as exc:  # noqa: BLE001 - jamais fatal.
            _logger.debug("Boucle audio interrompue : %s", exc)
        finally:
            try:
                capturer.stop()
            except Exception:  # noqa: BLE001
                pass
            _logger.info("Flux audio arrêté.")

    # -- Transfert de fichiers (download binaire / upload base64) -------------
    def _fs_send_roots(self) -> None:
        """Emplacements de départ (profil + lecteurs) pour l'explorateur du viewer."""
        if self._desktop_follow:
            self._send_text({"t": "fs_error", "id": 0, "code": "unattended"})
            return
        from . import fileio
        self._send_text({"t": "fs_roots", "list": fileio.list_roots()})

    def _fs_list(self, data: dict) -> None:
        """Liste un dossier du poste et renvoie son contenu au viewer."""
        if self._desktop_follow:
            self._send_text({"t": "fs_error", "id": 0, "code": "unattended"})
            return
        from . import fileio
        res = fileio.list_dir(data.get("path") or "")
        if "error" in res:
            self._send_text({"t": "fs_error", "id": 0, "code": res["error"],
                             "path": data.get("path")})
            return
        self._send_text({"t": "fs_listing", "path": res["path"],
                         "parent": res["parent"], "entries": res["entries"]})

    def _fs_download(self, data: dict) -> None:
        """Démarre l'envoi d'un fichier (agent → viewer) en trames binaires 0x20."""
        tid = _to_int(data.get("id"))
        if self._desktop_follow:
            self._send_text({"t": "fs_error", "id": tid, "code": "unattended"})
            return
        if self._file_send_thread is not None and self._file_send_thread.is_alive():
            self._send_text({"t": "fs_error", "id": tid, "code": "busy"})
            return
        from . import fileio
        fh, name, size, err = fileio.open_download(data.get("path") or "")
        if err:
            self._send_text({"t": "fs_error", "id": tid, "code": err})
            return
        self._file_cancel.discard(tid)
        self._send_text({"t": "fs_download_start", "id": tid, "name": name, "size": size})
        self._file_send_thread = threading.Thread(
            target=self._file_send_loop, args=(tid, fh, name, size),
            name="truesight-remote-file", daemon=True,
        )
        self._file_send_thread.start()

    def _file_send_loop(self, tid: int, fh, name: str, size: int) -> None:
        """Lit le fichier par chunks et l'envoie en trames 0x20 (dernier = flag)."""
        from . import fileio
        seq = 0
        sent = 0
        sent_last = False
        try:
            for block in fileio.read_chunks(fh):
                if self._should_stop() or tid in self._file_cancel:
                    break
                sent += len(block)
                last = bool(size) and sent >= size
                if not self._send_binary(fileio.build_file_chunk_frame(tid, seq, block, last)):
                    break
                seq += 1
                if last:
                    sent_last = True
                    break
            # Fichier vide ou taille mal estimée : trame finale vide pour clore.
            if not sent_last and not self._should_stop() and tid not in self._file_cancel:
                self._send_binary(fileio.build_file_chunk_frame(tid, seq, b"", True))
            self._send_text({"t": "fs_done", "id": tid, "dir": "down", "name": name})
        except Exception as exc:  # noqa: BLE001 - jamais fatal.
            _logger.info("Download interrompu (%s) : %s", name, exc)
            self._send_text({"t": "fs_error", "id": tid, "code": "io"})
        finally:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._file_cancel.discard(tid)

    def _fs_upload_start(self, data: dict) -> None:
        """Prépare la réception d'un fichier (viewer → agent) : ouvre un .tspart."""
        tid = _to_int(data.get("id"))
        if self._desktop_follow:
            self._send_text({"t": "fs_error", "id": tid, "code": "unattended"})
            return
        from . import fileio
        fh, tmp_path, final_path, err = fileio.open_upload(
            data.get("dir") or "", data.get("name") or "", data.get("size"))
        if err:
            self._send_text({"t": "fs_error", "id": tid, "code": err})
            return
        self._uploads[tid] = {"fh": fh, "tmp": tmp_path, "final": final_path, "received": 0}
        self._send_text({"t": "fs_upload_ready", "id": tid})

    def _fs_upload_chunk(self, data: dict) -> None:
        """Reçoit un chunk d'upload (base64), l'écrit, et finalise au dernier."""
        from . import MAX_FILE_BYTES
        from . import fileio
        tid = _to_int(data.get("id"))
        up = self._uploads.get(tid)
        if up is None:
            return
        try:
            raw = base64.b64decode(data.get("data") or "")
        except Exception:  # noqa: BLE001
            self._abort_upload(tid, "io")
            return
        up["received"] += len(raw)
        if up["received"] > MAX_FILE_BYTES:
            self._abort_upload(tid, "too_big")
            return
        try:
            up["fh"].write(raw)
        except OSError:
            self._abort_upload(tid, "io")
            return
        if data.get("last"):
            try:
                up["fh"].close()
            except Exception:  # noqa: BLE001
                pass
            ok = fileio.finalize_upload(up["tmp"], up["final"])
            self._uploads.pop(tid, None)
            if ok:
                self._send_text({"t": "fs_done", "id": tid, "dir": "up",
                                 "name": os.path.basename(up["final"])})
            else:
                self._send_text({"t": "fs_error", "id": tid, "code": "io"})

    def _abort_upload(self, tid: int, code: str) -> None:
        """Annule un upload : ferme + supprime le .tspart, prévient le viewer."""
        from . import fileio
        up = self._uploads.pop(tid, None)
        if up is not None:
            try:
                up["fh"].close()
            except Exception:  # noqa: BLE001
                pass
            fileio._cleanup(up["tmp"])
        self._send_text({"t": "fs_error", "id": tid, "code": code})

    def _fs_cancel(self, data: dict) -> None:
        """Annule un transfert en cours (download ou upload)."""
        tid = _to_int(data.get("id"))
        self._file_cancel.add(tid)
        if tid in self._uploads:
            self._abort_upload(tid, "cancelled")

    def _fs_cleanup(self) -> None:
        """Nettoie les uploads partiels à la fin de la session."""
        from . import fileio
        for _tid, up in list(self._uploads.items()):
            try:
                up["fh"].close()
            except Exception:  # noqa: BLE001
                pass
            fileio._cleanup(up["tmp"])
        self._uploads.clear()

    # -- Boucle de réception des entrées (principal) --------------------------
    def _recv_loop(self) -> None:
        """Reçoit les messages texte JSON (viewer → agent) et les applique."""
        while not self._should_stop():
            ws = self._ws
            if ws is None:
                break
            try:
                message = ws.recv()
            except Exception as exc:  # noqa: BLE001
                # Timeout de lecture (settimeout) : on reboucle pour vérifier l'arrêt.
                name = exc.__class__.__name__
                if "timeout" in name.lower():
                    continue
                _logger.info("Réception terminée (session fermée) : %s", exc)
                break
            if message is None or message == "":
                continue
            # Le viewer n'envoie que du texte JSON (entrées + contrôles).
            if isinstance(message, (bytes, bytearray)):
                # Inattendu dans le sens viewer → agent ; on ignore.
                continue
            self._handle_text(message)
        self._stop.set()

    def _handle_text(self, raw: str) -> None:
        """Décode et applique un message JSON viewer → agent."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            _logger.debug("Message viewer non-JSON ignoré.")
            return
        if not isinstance(data, dict):
            return

        msg_type = data.get("t")
        # Mesure de latence : on renvoie un pong (texte agent → viewer).
        if msg_type == "ping":
            self._send_text({"t": "pong", "ts": data.get("ts")})
            return
        # Messages de contrôle : pilotent la capture.
        if msg_type == "set_quality":
            self._capturer.set_quality(data.get("q", 70))
            return
        if msg_type == "set_fps":
            self._capturer.set_fps(data.get("fps", 18))
            return
        if msg_type == "set_max_width":
            self._capturer.set_max_width(data.get("w", 1600))
            return
        if msg_type == "request_keyframe":
            self._capturer.request_keyframe()
            return
        if msg_type == "set_monitor":
            self._capturer.set_monitor(data.get("i", 0))
            # L'injection doit suivre le moniteur courant (échelle des coordonnées).
            self._injector.set_monitor(self._capturer.current_monitor_geometry())
            return
        # Contrôle exclusif (piloté par le viewer).
        if msg_type == "lock_input":
            self._set_input_lock(bool(data.get("on")))
            return
        if msg_type == "send_sas":
            if self._desktop_follow:
                self._ensure_inject_desktop()
            inject_mod.send_sas()
            return
        if msg_type == "lock_on_disconnect":
            self._lock_on_disconnect = bool(data.get("on"))
            _logger.info("Verrouillage à la déconnexion : %s.", self._lock_on_disconnect)
            return
        if msg_type == "privacy":
            self._set_privacy(bool(data.get("on")))
            return
        if msg_type == "audio":
            self._set_audio(bool(data.get("on")))
            return
        # Transfert de fichiers (pendant la session de bureau à distance).
        if msg_type == "fs_roots":
            self._fs_send_roots()
            return
        if msg_type == "fs_list":
            self._fs_list(data)
            return
        if msg_type == "fs_download":
            self._fs_download(data)
            return
        if msg_type == "fs_upload_start":
            self._fs_upload_start(data)
            return
        if msg_type == "fs_upload_chunk":
            self._fs_upload_chunk(data)
            return
        if msg_type == "fs_cancel":
            self._fs_cancel(data)
            return
        # Sinon : entrée souris/clavier effective.
        if self._desktop_follow:
            self._ensure_inject_desktop()
        inject_mod.apply_input_message(self._injector, data)

    def _ensure_inject_desktop(self) -> None:
        """Mode non-assisté : attache le thread de réception (qui injecte) au bureau
        d'entrée actif, et ré-attache à chaque bascule — ``SendInput`` cible le
        bureau du thread appelant."""
        try:
            from . import desktop as desk
            name = desk.current_input_desktop_name()
            if name and name != self._inject_desk:
                desk.attach_thread_to_input_desktop()
                self._inject_desk = name
                _logger.info("Injection ré-attachée au bureau : %s", name)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Ré-attachement de l'injection impossible : %s", exc)

    # -- Exécution ------------------------------------------------------------
    def run(self) -> None:
        """Établit la session et la fait tourner jusqu'à fermeture."""
        # Conscience DPI AVANT toute capture/métrique : aligne mss (pixels
        # physiques) et les coordonnées d'injection sur les écrans à 125/150 %.
        inject_mod.set_dpi_awareness()
        if not capture_mod.is_available():
            _logger.error("Capture indisponible (mss/encodeur manquant) : session annulée.")
            return
        if not self._connect():
            return

        self._started_at = time.monotonic()
        # Keyframe d'amorçage : le viewer doit recevoir une image dès l'appairage.
        self._capturer.request_keyframe()
        # Infos de confort au viewer (écrans + utilisateur connecté).
        self._send_metadata()

        # Thread d'envoi (trames) ; la boucle principale reçoit les entrées.
        send_target = self._send_loop_unattended if self._desktop_follow else self._send_loop
        self._send_thread = threading.Thread(
            target=send_target, name="truesight-remote-send", daemon=True
        )
        self._send_thread.start()

        # Thread « curseur » : position du curseur distant → surcouche viewer.
        self._cursor_thread = threading.Thread(
            target=self._cursor_loop, name="truesight-remote-cursor", daemon=True
        )
        self._cursor_thread.start()

        try:
            self._recv_loop()
        finally:
            self._teardown()

    def _teardown(self) -> None:
        """Ferme proprement : libère le contrôle exclusif, ferme la WebSocket, joint les threads."""
        self._stop.set()

        # Libère tout contrôle exclusif éventuellement posé pendant la session,
        # AVANT de fermer (sinon le poste resterait clavier/souris bloqués).
        if self._input_locked:
            try:
                inject_mod.block_input(False)
            except Exception:  # noqa: BLE001
                pass
            self._input_locked = False
        if self._privacy is not None:
            try:
                self._privacy.stop()
            except Exception:  # noqa: BLE001
                pass
            self._privacy = None
        # Arrête l'écoute audio (la boucle libère le flux PortAudio).
        self._audio_on = False
        if self._audio is not None:
            try:
                self._audio.stop()
            except Exception:  # noqa: BLE001
                pass
            self._audio = None
        # Transferts de fichiers : annule le download en cours et supprime les
        # uploads partiels (.tspart) pour ne pas laisser de résidus.
        self._fs_cleanup()
        # Verrouillage du poste à la déconnexion (option viewer).
        if self._lock_on_disconnect:
            try:
                inject_mod.lock_workstation()
            except Exception:  # noqa: BLE001
                pass

        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        for thread in (self._send_thread, self._cursor_thread, self._audio_thread,
                       self._file_send_thread):
            if thread is not None:
                try:
                    thread.join(timeout=5)
                except RuntimeError:
                    pass
        _logger.info("Session de bureau à distance terminée.")


def run(token: str, ws_url: str, verify_tls: bool = True, desktop_follow: bool = False) -> int:
    """Point d'entrée de la session (utilisé par le helper et le mode console).

    ``desktop_follow`` (mode non-assisté) : la capture/injection suivent le bureau
    d'entrée actif (helper SYSTEM, écran de connexion compris).

    Bloque jusqu'à la fin de la session. Renvoie 0 (succès), 1 (échec
    d'initialisation). Ne lève jamais.
    """
    if not token or not ws_url:
        _logger.error("Session impossible : token ou ws_url manquant.")
        return 1
    try:
        session = RemoteSession(token, ws_url, verify_tls=verify_tls, desktop_follow=desktop_follow)
        session.run()
        return 0
    except Exception as exc:  # noqa: BLE001 - filet ultime.
        _logger.error("Session de bureau à distance en échec : %s", exc)
        return 1


def _to_int(value) -> int:
    """Convertit en entier de façon tolérante (id de transfert), ou 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _current_user_label() -> str:
    """Libellé de l'utilisateur connecté (DOMAINE\\user) — le helper tourne dans sa session."""
    domain = os.environ.get("USERDOMAIN", "") or os.environ.get("COMPUTERNAME", "")
    user = os.environ.get("USERNAME", "")
    if domain and user:
        return f"{domain}\\{user}"
    return user or "—"


def _redact(ws_url: str) -> str:
    """Masque le token dans l'URL pour les logs (le token reste secret)."""
    if "token=" not in ws_url:
        return ws_url
    head, _sep, _rest = ws_url.partition("token=")
    return head + "token=***"
