import os
import math
import threading
import time
import colorsys
import ctypes

os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_HIDAPI_PS4_RUMBLE", "1")

import numpy as np
import soundcard as sc
import hid
import tkinter as tk
from tkinter import colorchooser
import customtkinter as ctk
import pygame

from ds4_bt_audio import (CodificadorSBC, Transmisor, Remuestreador, BufferPCM, VARIANTES, reporte_11,
                          generar_tono, SONY_VID, DS4_PIDS, DEST_JACK, DEST_ALTAVOZ, VOL_MAX)

BG = "#0c0d10"
CARD = "#14161a"
EDGE = "#2a2e36"
OFF = "#1d2026"
ON = "#6c8299"
DOT = "#8a919c"
TEXT = "#c4c8cf"
MUTED = "#757b86"
OK = "#5d8a74"
BAD = "#955f5f"
WAIT = "#6d727c"
BTN = "#22262d"
BTN_HOVER = "#2d323b"
ACCENT = "#4d6580"
KNOB = "#9aa1ac"
KNOB_HOVER = "#b0b6bf"

BUTTON_MAP = [
    ("A", "A"), ("B", "B"), ("X", "X"), ("Y", "Y"),
    ("BACK", "BACK"), ("START", "START"), ("GUIDE", "GUIDE"),
    ("LEFTSTICK", "LEFT_THUMB"), ("RIGHTSTICK", "RIGHT_THUMB"),
    ("LEFTSHOULDER", "LEFT_SHOULDER"), ("RIGHTSHOULDER", "RIGHT_SHOULDER"),
    ("DPAD_UP", "DPAD_UP"), ("DPAD_DOWN", "DPAD_DOWN"),
    ("DPAD_LEFT", "DPAD_LEFT"), ("DPAD_RIGHT", "DPAD_RIGHT"),
]

LED_PRESETS = [
    (255, 0, 0), (255, 90, 0), (255, 200, 0), (0, 255, 80),
    (0, 200, 255), (0, 60, 255), (160, 0, 255), (255, 0, 160), (255, 255, 255),
]
LED_MODOS = [
    ("none", "Sin tocar", "No cambia la luz: el mando usa su color normal."),
    ("solid", "Color fijo", "Luz fija con el color que elijas abajo."),
    ("rainbow", "Arcoíris", "El color recorre todo el arcoíris sin parar."),
    ("audio", "Ritmo", "Tu color elegido, con la intensidad latiendo al ritmo del sonido del PC."),
    ("audio_rainbow", "Ritmo + Arcoíris", "El arcoíris cambia de color con cada golpe de la música y brilla con el volumen."),
]
LED_DINAMICOS = ("rainbow", "audio", "audio_rainbow")
LED_CON_AUDIO = ("audio", "audio_rainbow")

MAX_MANDOS = 4
PLAYER_COLORS = [(0, 0, 64), (64, 0, 0), (0, 64, 0), (64, 0, 32)]


def radial_deadzone(x, y, dz):
    m = math.hypot(x, y)
    if m <= dz: return 0.0, 0.0
    scale = min(1.0, (m - dz) / (1.0 - dz)) / m
    return max(-1.0, min(1.0, x * scale)), max(-1.0, min(1.0, y * scale))

def to_i16(v): return int(max(-1.0, min(1.0, v)) * 32767)
def to_hex(rgb): return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(c))) for c in rgb])
def scale_rgb(rgb, k): return tuple(int(max(0, min(255, c * k))) for c in rgb)

class LedEngine:
    PISO = 0.04

    def __init__(self):
        self.env = 0.0
        self.avg = 0.0
        self.hue = 0.0
        self.salto = 0.0
        self.t_prev = None
        self.t_golpe = -10.0

    def step(self, now, mode, color, brillo, velocidad, sens, nivel):
        dt = 0.05 if self.t_prev is None else min(0.25, max(0.001, now - self.t_prev))
        self.t_prev = now

        x = max(0.0, min(1.0, ((nivel - 0.25) / 0.75) * sens)) if nivel > 0 else 0.0
        self.env += (x - self.env) * (1.0 - math.exp(-dt / (0.03 if x > self.env else 0.18)))
        if x > self.avg * 1.3 + 0.10 and now - self.t_golpe > 0.2:
            self.t_golpe = now
            self.salto = (self.salto + 0.12) % 1.0
        self.avg += (x - self.avg) * (1.0 - math.exp(-dt / 0.7))
        self.hue = (self.hue + dt * velocidad) % 1.0

        flash = max(0.0, 1.0 - (now - self.t_golpe) / 0.15)
        lvl = min(1.0, self.PISO + (1.0 - self.PISO) * self.env + 0.2 * flash)

        if mode == "solid":
            return scale_rgb(color, brillo)
        if mode == "rainbow":
            r, g, b = colorsys.hsv_to_rgb(self.hue, 1.0, 1.0)
            return scale_rgb((r * 255, g * 255, b * 255), brillo)
        if mode == "audio":
            return scale_rgb(color, brillo * lvl)
        if mode == "audio_rainbow":
            r, g, b = colorsys.hsv_to_rgb((self.hue + self.salto) % 1.0, 1.0 - 0.45 * flash, 1.0)
            return scale_rgb((r * 255, g * 255, b * 255), brillo * lvl)
        return None

class WinBoost:
    def __init__(self):
        self.on = False
        try:
            self.k32 = ctypes.windll.kernel32
            self.winmm = ctypes.windll.winmm
        except Exception:
            self.k32 = self.winmm = None
    def set(self, enable):
        if self.k32 is None or enable == self.on: return
        try:
            if enable:
                self.winmm.timeBeginPeriod(1)
                self.k32.SetPriorityClass(self.k32.GetCurrentProcess(), 0x00008000)
                self.k32.SetThreadPriority(self.k32.GetCurrentThread(), 2)
            else:
                self.k32.SetThreadPriority(self.k32.GetCurrentThread(), 0)
                self.k32.SetPriorityClass(self.k32.GetCurrentProcess(), 0x00000020)
                self.winmm.timeEndPeriod(1)
            self.on = enable
        except Exception: pass

EMPTY_SNAP = {
    "connected": False, "name": "", "pad_ok": False, "error": "",
    "lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0,
    "pressed": frozenset(), "led": False, "led_rgb": None, "hz": 0,
}

class Mando:
    def __init__(self, idx):
        self.idx = idx
        self.ctrl = None; self.joy = None; self.iid = None
        self.name = ""; self.huella = None; self.sony = False
        self.vpad = None
        self.hid_dev = None; self.hid_path = None
        self.hid_lock = threading.Lock()
        self.enabled = True
        self.deadzone = 0.08
        self.led_mode = "none"
        self.led_color = (70, 110, 200)
        self.led_brightness = 1.0
        self.led_speed = 0.2
        self.led_sens = 1.0
        self.led_engine = LedEngine()
        self.led_stats = [0, 0, ""]
        self.identificar_hasta = 0.0
        self.ident_activo = False
        self.audio_enabled = False
        self.volumen = 60
        self.destino = DEST_ALTAVOZ
        self.variante = 1
        self.audio_error = ""
        self.audio_stats = (0, 0, 0, "")
        self.tone_until = 0.0
        self.tone_buf = BufferPCM(32000 * 4)
        self.buf = BufferPCM(512 * 6)
        self.bateria = None
        self.aviso_bateria = True
        self.umbral_bateria = 20
        self.aviso_hecho = False
        self.snap = dict(EMPTY_SNAP)
        self.last_sent = None; self.last_rgb = None; self.last_led_t = 0.0
        self.last_mode = "none"; self.last_vol = -1
        self.last_batt = 0.0; self.last_snap = 0.0; self.fallos_seguidos = 0

    @property
    def conectado(self):
        return self.ctrl is not None

    def probar_tono(self):
        self.tone_buf = BufferPCM(32000 * 4)
        self.tone_buf.poner(np.vstack([generar_tono(440, 3.0), np.zeros((512, 2))]))
        self.tone_until = time.time() + 3.3


class SystemBridge:
    def __init__(self):
        self.low_latency = False
        self.audio_level = 0.0
        self.audio_error = ""
        self.error = ""
        self.mandos = [Mando(i) for i in range(MAX_MANDOS)]
        self.pref_slot = {}
        self.ignorados = set()
        self._vpad_t = 0.0
        self.vg = None
        self._ctl = None
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._controller_loop, daemon=True).start()
        threading.Thread(target=self._audio_capture_loop, daemon=True).start()
        for m in self.mandos:
            threading.Thread(target=self._audio_encode_loop, args=(m,), daemon=True).start()

    def stop(self):
        self._stop.set()

    def _audio_pedido(self):
        return any(m.audio_enabled for m in self.mandos)

    def _led_usa_audio(self):
        return any(m.conectado and m.led_mode in LED_CON_AUDIO for m in self.mandos)

    def _audio_capture_loop(self):
        while not self._stop.is_set():
            if not (self._audio_pedido() or self._led_usa_audio()):
                self.audio_level = 0.0
                time.sleep(0.3)
                continue
            try:
                alt = sc.default_speaker()
                mic = sc.get_microphone(id=str(alt.name), include_loopback=True)
                rem = Remuestreador(48000)
                pico = 0.02
                with mic.recorder(samplerate=48000, channels=2) as rec:
                    while not self._stop.is_set() and (self._audio_pedido() or self._led_usa_audio()):
                        datos = rec.record(numframes=48000 // 50)
                        x = np.asarray(datos, dtype=np.float64)
                        rms = float(np.sqrt(np.mean(x * x)))
                        pico = max(rms, pico * 0.997, 0.01)
                        self.audio_level = 0.0 if rms < 0.002 else min(1.0, rms / pico)
                        if self.audio_error:
                            self.audio_error = ""
                        if self._audio_pedido():
                            pcm = np.clip(rem.procesar(x), -1.0, 1.0) * 32767.0
                            for m in self.mandos:
                                if m.audio_enabled: m.buf.poner(pcm)
            except Exception as e:
                self.audio_level = 0.0
                self.audio_error = f"No se pudo capturar el audio del PC: {e!r}"
                time.sleep(1)

    def _audio_encode_loop(self, m):
        enc = None
        silencio = np.zeros((512, 2))
        periodo = 0.016
        t0 = time.perf_counter()
        k = 0
        tx, clave, armado_en, activo_antes = None, None, 0.0, False
        while not self._stop.is_set():
            tono = time.time() < m.tone_until
            dev = m.hid_dev
            activo = (m.audio_enabled or tono) and dev is not None
            if not activo:
                if activo_antes and tx is not None:
                    with m.hid_lock:
                        tx.armar(0, 0)
                    activo_antes = False
                time.sleep(0.1)
                t0, k = time.perf_counter(), 0
                continue
            if enc is None: enc = CodificadorSBC()

            nueva = (id(dev), m.variante, m.destino)
            if tx is None or nueva != clave:
                tx, clave, armado_en = Transmisor(dev, m.variante, m.destino), nueva, 0.0

            ahora = time.time()
            if ahora - armado_en > 1.0:
                primera = armado_en == 0.0
                with m.hid_lock:
                    tx.armar(m.volumen, m.volumen)
                armado_en, activo_antes = ahora, True
                if primera:
                    t0, k = time.perf_counter(), 0

            t_target = t0 + k * periodo
            dt = t_target - time.perf_counter()
            if dt < -0.1:
                t0, k = time.perf_counter(), 0
                t_target = t0
            if dt > 0.003: time.sleep(dt - 0.002)
            while time.perf_counter() < t_target: pass

            pcm = m.tone_buf.tomar(512) if tono else m.buf.tomar(512)
            if pcm is None: pcm = silencio

            bloque = b"".join(enc.codificar(pcm))
            with m.hid_lock:
                tx.enviar(bloque)
            m.audio_stats = (tx.ok, tx.fallos, tx.rc, tx.ultimo_error)
            if tx.seguidos >= 5:
                m.audio_error = "Windows rechaza los reportes de audio: " + (tx.ultimo_error or "sin detalle")
                time.sleep(0.25)
                t0, k = time.perf_counter(), 0
                continue
            if m.audio_error.startswith("Windows rechaza"):
                m.audio_error = ""
            k += 1

    def _hid_paths(self):
        try:
            cand = [d for d in hid.enumerate(SONY_VID, 0) if d["product_id"] in DS4_PIDS]
        except Exception:
            return []
        pref = [d for d in cand if d.get("usage_page") == 1 and d.get("usage") == 5]
        vistos, res = set(), []
        for d in (pref or cand):
            if d["path"] not in vistos:
                vistos.add(d["path"]); res.append(d["path"])
        return res

    def _reclamar_hid(self):
        usadas = {m.hid_path for m in self.mandos if m.hid_path is not None}
        for path in self._hid_paths():
            if path not in usadas:
                return path
        return None

    @staticmethod
    def _abrir_hid(path):
        try:
            dev = hid.device()
            dev.open_path(path)
            dev.set_nonblocking(1)
            try: dev.get_feature_report(0x02, 37)
            except Exception: pass
            return dev
        except Exception:
            return None

    @staticmethod
    def _guid(joy):
        try: return str(joy.get_guid()).lower()
        except Exception: return ""

    @staticmethod
    def _vid_pid(guid):
        try: return int(guid[10:12] + guid[8:10], 16), int(guid[18:20] + guid[16:18], 16)
        except Exception: return None, None

    @staticmethod
    def _parece_virtual(vid, pid, nombre):
        return (vid, pid) == (0x045E, 0x028E) or "xbox 360" in nombre.lower() or "xinput" in nombre.lower()

    def _ids_sdl(self):
        res = {}
        try:
            for i in range(self._ctl.get_count()):
                try:
                    joy = pygame.joystick.Joystick(i)
                    res[joy.get_instance_id()] = self._vid_pid(self._guid(joy))[0]
                except Exception: pass
        except Exception: pass
        return res

    def _crear_vpad(self, m):
        antes = self._ids_sdl()
        m.vpad = self.vg.VX360Gamepad()
        self._vpad_t = time.time()
        fin = time.time() + 1.5
        while time.time() < fin:
            time.sleep(0.1)
            try: pygame.event.pump()
            except Exception: pass
            nuevos = [k for k, vid in self._ids_sdl().items() if k not in antes and vid != SONY_VID]
            if nuevos:
                self.ignorados |= set(nuevos)
                break

    def _escanear(self, controller):
        for m in self.mandos:
            if m.conectado:
                vivo = False
                try: vivo = m.ctrl.attached()
                except Exception: pass
                if not vivo: self._liberar(m)
        for m in self.mandos:
            if m.conectado and m.sony and m.hid_dev is None:
                path = self._reclamar_hid()
                dev = self._abrir_hid(path) if path is not None else None
                if dev is not None:
                    with m.hid_lock: m.hid_dev, m.hid_path = dev, path
                    m.last_rgb = None
        libres = [m for m in self.mandos if not m.conectado]
        for i in range(controller.get_count()):
            if not libres: break
            try:
                if not controller.is_controller(i): continue
                joy = pygame.joystick.Joystick(i)
                try: iid = joy.get_instance_id()
                except Exception: iid = f"i{i}"
            except Exception:
                continue
            if iid in self.ignorados or any(m.iid == iid for m in self.mandos): continue
            try: nombre = controller.name_forindex(i) or "Mando"
            except Exception: nombre = "Mando"
            guid = self._guid(joy)
            vid, pid = self._vid_pid(guid)
            if self._parece_virtual(vid, pid, nombre) and time.time() - self._vpad_t < 8.0:
                continue
            self._conectar(controller, i, joy, iid, nombre, guid, vid, libres)
            libres = [m for m in self.mandos if not m.conectado]

    def _conectar(self, controller, i, joy, iid, nombre, guid, vid, libres):
        try: ctrl = controller.Controller(i)
        except Exception: return
        sony = vid in (None, SONY_VID)
        path = self._reclamar_hid() if sony else None
        huella = path if path is not None else (guid or nombre)
        pref = self.pref_slot.get(huella)
        m = self.mandos[pref] if pref is not None and not self.mandos[pref].conectado else libres[0]
        dev = self._abrir_hid(path) if path is not None else None
        with m.hid_lock:
            m.hid_dev, m.hid_path = dev, (path if dev is not None else None)
        m.ctrl, m.joy, m.iid, m.name, m.huella, m.sony = ctrl, joy, iid, nombre, huella, sony
        self.pref_slot[huella] = m.idx
        m.last_sent = None; m.last_rgb = None; m.bateria = None; m.last_batt = 0.0; m.fallos_seguidos = 0
        m.aviso_hecho = False
        if self.vg is not None and m.vpad is None:
            try: self._crear_vpad(m)
            except Exception as exc: self.error = f"Error ViGEmBus: {exc}"

    def _liberar(self, m):
        try: m.ctrl.quit()
        except Exception: pass
        with m.hid_lock:
            if m.hid_dev is not None:
                try: m.hid_dev.close()
                except Exception: pass
            m.hid_dev, m.hid_path = None, None
        if m.vpad is not None:
            try: m.vpad.reset(); m.vpad.update()
            except Exception: pass
        m.ctrl = m.joy = m.iid = None
        m.name = ""
        m.bateria = None
        m.aviso_hecho = False
        m.last_sent = None; m.last_rgb = None
        m.snap = {**EMPTY_SNAP, "pad_ok": m.vpad is not None, "error": self.error}

    def identificar(self, m):
        m.identificar_hasta = time.time() + 2.5

    def intercambiar_hid(self, a, b):
        p, q = (a, b) if a.idx < b.idx else (b, a)
        with p.hid_lock, q.hid_lock:
            a.hid_dev, b.hid_dev = b.hid_dev, a.hid_dev
            a.hid_path, b.hid_path = b.hid_path, a.hid_path
        for m in (a, b):
            m.bateria = None; m.last_batt = 0.0; m.last_rgb = None
            if m.hid_path is not None:
                m.huella = m.hid_path
                self.pref_slot[m.huella] = m.idx

    def _leer_bateria(self, m):
        data = None
        if m.hid_dev is not None:
            with m.hid_lock:
                try:
                    for _ in range(64):
                        r = m.hid_dev.read(96)
                        if not r: break
                        if r[0] in (0x01, 0x11): data = r
                except Exception:
                    data = None
        st = None
        if data:
            if data[0] == 0x11 and len(data) > 32: st = data[32]
            elif data[0] == 0x01 and len(data) > 30: st = data[30]
        if st is not None:
            nivel, cable = st & 0x0F, bool(st & 0x10)
            if cable:
                completa = nivel >= 11
                pct = 100 if completa else min(100, nivel * 100 // 11)
            else:
                completa = False
                pct = min(100, nivel * 100 // 8)
            return {"pct": pct, "carga": cable, "completa": completa, "aprox": False, "cable": cable}
        try: nivel = str(m.joy.get_power_level()).lower()
        except Exception: nivel = ""
        tabla = {"empty": 5, "low": 20, "medium": 55, "full": 90, "max": 100}
        if nivel in tabla:
            return {"pct": tabla[nivel], "carga": False, "completa": nivel == "max", "aprox": True, "cable": False}
        if nivel == "wired":
            return {"pct": None, "carga": False, "completa": False, "aprox": True, "cable": True}
        return None

    def _enviar_led(self, m, rgb, vol):
        rep11 = reporte_11(vol, vol, VARIANTES.get(m.variante, VARIANTES[1]), rgb)
        with m.hid_lock:
            try:
                n = m.hid_dev.write(rep11) if m.hid_dev is not None else -1
            except Exception as e:
                n = -1; m.led_stats[2] = repr(e)
        if n is None or n < 0: m.led_stats[1] += 1
        else: m.led_stats[0] += 1

    def _luz(self, m, now):
        if m.hid_dev is None: return
        vol = m.volumen if (m.audio_enabled or now < m.tone_until) else 0
        if now < m.identificar_hasta:
            if now - m.last_led_t >= 0.12:
                self._enviar_led(m, (255, 255, 255) if int(now * 4) % 2 == 0 else (0, 0, 0), vol)
                m.last_led_t = now
            m.ident_activo = True
            return
        if m.ident_activo:
            m.ident_activo = False
            m.last_rgb = None; m.last_led_t = 0.0
            if m.led_mode == "none":
                self._enviar_led(m, PLAYER_COLORS[m.idx % len(PLAYER_COLORS)], vol)
        mode = m.led_mode
        if mode != "none":
            dinamico = mode in LED_DINAMICOS
            if not dinamico or now - m.last_led_t >= 0.05:
                rgb = m.led_engine.step(now, mode, m.led_color, m.led_brightness, m.led_speed, m.led_sens,
                                        self.audio_level if mode in LED_CON_AUDIO else 0.0)
                if rgb is not None and (dinamico or rgb != m.last_rgb or mode != m.last_mode
                                        or vol != m.last_vol or now - m.last_led_t > 2.0):
                    self._enviar_led(m, rgb, vol)
                    m.last_rgb, m.last_led_t, m.last_vol = rgb, now, vol
        m.last_mode = mode

    def _procesar(self, m, btn_pairs, sdl_name, now, hz):
        ctrl = m.ctrl
        g = lambda ax: ctrl.get_axis(getattr(pygame, f"CONTROLLER_AXIS_{ax}")) / 32768.0
        lx, ly = radial_deadzone(g("LEFTX"), g("LEFTY"), m.deadzone)
        rx, ry = radial_deadzone(g("RIGHTX"), g("RIGHTY"), m.deadzone)
        ly, ry = -ly, -ry
        lt = max(0.0, min(1.0, g("TRIGGERLEFT"))); rt = max(0.0, min(1.0, g("TRIGGERRIGHT")))
        lt = 0.0 if lt < 0.03 else lt; rt = 0.0 if rt < 0.03 else rt

        pressed = set()
        for sdl_btn, xb_btn in btn_pairs:
            try:
                if ctrl.get_button(sdl_btn): pressed.add(sdl_name[sdl_btn])
            except Exception: pass

        vpad = m.vpad
        if vpad:
            if m.enabled:
                axes = (to_i16(lx), to_i16(ly), to_i16(rx), to_i16(ry), int(lt * 255), int(rt * 255))
                state = (frozenset(pressed), axes)
                if not (self.low_latency and state == m.last_sent):
                    for sdl_btn, xb_btn in btn_pairs:
                        if sdl_name[sdl_btn] in pressed: vpad.press_button(button=xb_btn)
                        else: vpad.release_button(button=xb_btn)
                    vpad.left_joystick(x_value=axes[0], y_value=axes[1])
                    vpad.right_joystick(x_value=axes[2], y_value=axes[3])
                    vpad.left_trigger(value=axes[4]); vpad.right_trigger(value=axes[5])
                    vpad.update(); m.last_sent = state
            elif m.last_sent != "off":
                vpad.reset(); vpad.update(); m.last_sent = "off"

        self._luz(m, now)
        if now - m.last_batt >= 2.0:
            m.last_batt = now
            m.bateria = self._leer_bateria(m)

        if not self.low_latency or now - m.last_snap >= 0.016:
            m.last_snap = now
            m.snap = {
                "connected": True, "name": m.name, "pad_ok": vpad is not None, "error": self.error,
                "lx": lx, "ly": ly, "rx": rx, "ry": ry, "lt": lt, "rt": rt,
                "pressed": frozenset(pressed), "led": m.hid_dev is not None, "led_rgb": m.last_rgb, "hz": hz,
            }

    def _controller_loop(self):
        from pygame._sdl2 import controller
        self._ctl = controller
        pygame.display.init(); pygame.joystick.init(); controller.init()
        XB = None
        try:
            import vgamepad as vg
            self.vg = vg; XB = vg.XUSB_BUTTON
        except Exception as exc: self.error = f"Error ViGEmBus: {exc}"

        btn_pairs = [(getattr(pygame, f"CONTROLLER_BUTTON_{s}"), getattr(XB, f"XUSB_GAMEPAD_{x}") if XB else None) for s, x in BUTTON_MAP]
        sdl_name = {getattr(pygame, f"CONTROLLER_BUTTON_{s}"): x for s, x in BUTTON_MAP}
        boost = WinBoost()
        last_scan, evt_n, loops, hz, hz_t = 0.0, 0, 0, 0, time.time()

        while not self._stop.is_set():
            boost.set(self.low_latency)
            try:
                pygame.event.pump(); evt_n += 1
                if evt_n % 250 == 0: pygame.event.clear()
            except Exception: pass

            now = time.time()
            if now - last_scan > 1.0:
                last_scan = now
                try: self._escanear(controller)
                except Exception as exc: self.error = f"Error al buscar mandos: {exc!r}"

            activos = [m for m in self.mandos if m.conectado]
            if not activos:
                time.sleep(0.1)
                continue
            for m in activos:
                try:
                    self._procesar(m, btn_pairs, sdl_name, now, hz)
                    m.fallos_seguidos = 0
                except Exception:
                    m.fallos_seguidos += 1
                    if m.fallos_seguidos >= 5: self._liberar(m)

            loops += 1
            if now - hz_t >= 1.0: hz, loops, hz_t = int(loops / (now - hz_t)), 0, now
            time.sleep(0.0005 if self.low_latency else 0.004)

        boost.set(False)


class Preview(tk.Canvas):
    W, H = 520, 240
    def __init__(self, master):
        super().__init__(master, width=self.W, height=self.H, bg=CARD, highlightthickness=0)
        self.items, self.sticks, self.trig = {}, {}, {}
        self._build()
    def _label(self, x, y, txt, size=10, color=MUTED):
        return self.create_text(x, y, text=txt, fill=color, font=("Segoe UI", size, "bold"))
    def _build(self):
        for side, x0 in (("L", 60), ("R", 360)):
            self.create_rectangle(x0, 10, x0 + 100, 20, outline=EDGE, fill=OFF)
            self.trig[side] = (x0, self.create_rectangle(x0, 10, x0, 20, outline="", fill=ON))
            self.items[f"{'LEFT' if side == 'L' else 'RIGHT'}_SHOULDER"] = self.create_rectangle(x0, 28, x0 + 100, 46, outline=EDGE, fill=OFF)
            self._label(x0 + 50, 37, side + "B", 9)
        for key, (cx, cy) in {"L": (120, 105), "R": (310, 180)}.items():
            ring = self.create_oval(cx - 38, cy - 38, cx + 38, cy + 38, outline=EDGE, fill=OFF, width=2)
            self.sticks[key] = (cx, cy, self.create_oval(cx - 14, cy - 14, cx + 14, cy + 14, fill=DOT, outline=""), ring)
        self.items["LEFT_THUMB"], self.items["RIGHT_THUMB"] = self.sticks["L"][3], self.sticks["R"][3]
        dcx, dcy, s = 205, 180, 17
        for name, (x, y) in {"DPAD_UP": (dcx, dcy - s), "DPAD_DOWN": (dcx, dcy + s), "DPAD_LEFT": (dcx - s, dcy), "DPAD_RIGHT": (dcx + s, dcy)}.items():
            self.items[name] = self.create_rectangle(x - 8, y - 8, x + 8, y + 8, outline=EDGE, fill=OFF)
        fx, fy, r = 400, 105, 13
        for name, (x, y) in {"Y": (fx, fy - 28), "A": (fx, fy + 28), "X": (fx - 28, fy), "B": (fx + 28, fy)}.items():
            self.items[name] = self.create_oval(x - r, y - r, x + r, y + r, outline=EDGE, fill=OFF, width=2)
            self._label(x, y, name, 10)
        self.items["BACK"] = self.create_oval(218, 100, 236, 118, outline=EDGE, fill=OFF)
        self.items["START"] = self.create_oval(284, 100, 302, 118, outline=EDGE, fill=OFF)
        self.items["GUIDE"] = self.create_oval(251, 76, 269, 94, outline=EDGE, fill=OFF)
        self._label(227, 130, "BACK", 7); self._label(293, 130, "START", 7)
    def update_state(self, snap):
        for name, item in self.items.items(): self.itemconfig(item, fill=ON if name in snap["pressed"] else OFF)
        for key, (cx, cy, dot, _) in self.sticks.items():
            x, y = snap["lx" if key == "L" else "rx"], snap["ly" if key == "L" else "ry"]
            self.coords(dot, cx + x*24 - 14, cy - y*24 - 14, cx + x*24 + 14, cy - y*24 + 14)
        for side, key in (("L", "lt"), ("R", "rt")):
            self.coords(self.trig[side][1], self.trig[side][0], 10, self.trig[side][0] + 100 * snap[key], 20)

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title("PSTraductor")
        self.geometry("620x860")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.bridge = SystemBridge()
        self.sel = 0
        self._etiquetas = ()
        self._bat_clave = None
        self._n_conectados = -1
        self._build_ui()
        self._sync_ui()
        self.bridge.start()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(33, self._refresh)

    @property
    def cur(self):
        return self.bridge.mandos[self.sel]

    @staticmethod
    def _etiqueta(m):
        return f"Mando {m.idx + 1} · {m.name if m.conectado else 'sin conectar'}"

    def _slider(self, parent, text, lo, hi, init, command, fmt="{}", w=190):
        row = ctk.CTkFrame(parent, fg_color="transparent"); row.pack(fill="x", padx=18, pady=(6, 0))
        ctk.CTkLabel(row, text=text, text_color=TEXT, font=ctk.CTkFont("Segoe UI", 13)).pack(side="left")
        val = ctk.CTkLabel(row, text=fmt.format(int(init)), text_color=MUTED, width=46); val.pack(side="right")
        def _cb(v):
            val.configure(text=fmt.format(int(float(v))))
            command(float(v))
        s = ctk.CTkSlider(row, from_=lo, to=hi, width=w, fg_color=EDGE, progress_color=ACCENT, button_color=KNOB, button_hover_color=KNOB_HOVER, command=_cb)
        s.set(init); s.pack(side="right", padx=10)
        s._fmt = fmt
        return s, val

    @staticmethod
    def _sync_slider(sl, lbl, v):
        sl.set(v); lbl.configure(text=sl._fmt.format(int(v)))

    def _build_ui(self):
        ctk.CTkLabel(self, text="PSTraductor", font=ctk.CTkFont("Segoe UI", 28, "bold"), text_color=TEXT).pack(anchor="w", padx=28, pady=(22, 0))
        ctk.CTkLabel(self, text="Xbox Virtual + Audio Bluetooth", font=ctk.CTkFont("Segoe UI", 13), text_color=MUTED).pack(anchor="w", padx=28, pady=(0, 12))

        card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=14, border_width=1, border_color=EDGE); card.pack(fill="x", padx=24, pady=(0, 12))
        r1 = ctk.CTkFrame(card, fg_color="transparent"); r1.pack(fill="x", padx=18, pady=(12, 2))
        self.dot_in = ctk.CTkLabel(r1, text="●", text_color=WAIT, font=ctk.CTkFont(size=16), width=22); self.dot_in.pack(side="left")
        ctk.CTkLabel(r1, text="Configurando:", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 12)).pack(side="left", padx=(6, 6))
        self.opt_mando = ctk.CTkOptionMenu(r1, values=[self._etiqueta(self.cur)], width=330, dynamic_resizing=False, fg_color=BTN, button_color=BTN_HOVER, button_hover_color=BTN_HOVER, text_color=TEXT, command=self._elegir_mando)
        self.opt_mando.set(self._etiqueta(self.cur)); self.opt_mando.pack(side="left")
        r2 = ctk.CTkFrame(card, fg_color="transparent"); r2.pack(fill="x", padx=18, pady=(0, 12))
        self.dot_out = ctk.CTkLabel(r2, text="●", text_color=WAIT, font=ctk.CTkFont(size=16), width=22); self.dot_out.pack(side="left")
        self.lbl_out = ctk.CTkLabel(r2, text="En espera...", text_color=TEXT, font=ctk.CTkFont("Segoe UI", 13)); self.lbl_out.pack(side="left", padx=6)

        self.tabs = ctk.CTkTabview(self, fg_color=CARD, border_width=1, border_color=EDGE, corner_radius=14, text_color=TEXT)
        self.tabs.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        self._build_translate_tab(self.tabs.add("Traducción"))
        self._build_audio_tab(self.tabs.add("Audio Dualshock"))
        self._build_led_tab(self.tabs.add("Luz LED"))
        self.lbl_err = ctk.CTkLabel(self, text="", text_color=BAD, wraplength=560, justify="left", font=ctk.CTkFont("Segoe UI", 12))
        self.lbl_err.pack(anchor="w", padx=28, pady=(0, 8))

    def _build_translate_tab(self, tab):
        self.preview = Preview(tab); self.preview.pack(pady=(4, 6))
        top = ctk.CTkFrame(tab, fg_color="transparent"); top.pack(fill="x", padx=18, pady=(4, 2))
        ctk.CTkLabel(top, text="Traducción activa", text_color=TEXT).pack(side="left")
        self.sw = ctk.CTkSwitch(top, text="", command=lambda: setattr(self.cur, "enabled", bool(self.sw.get()))); self.sw.select(); self.sw.pack(side="right")
        self.sl_dead, self.v_dead = self._slider(tab, "Zona muerta sticks", 0, 30, 8, lambda v: setattr(self.cur, "deadzone", v / 100.0), fmt="{} %")
        ll = ctk.CTkFrame(tab, fg_color="transparent"); ll.pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(ll, text="Baja latencia (todos los mandos)", text_color=TEXT).pack(side="left")
        self.sw_ll = ctk.CTkSwitch(ll, text="", command=lambda: setattr(self.bridge, "low_latency", bool(self.sw_ll.get())))
        self.sw_ll.pack(side="right")

        bt = ctk.CTkFrame(tab, fg_color="transparent"); bt.pack(fill="x", padx=18, pady=(12, 0))
        ctk.CTkLabel(bt, text="Batería", text_color=TEXT).pack(side="left")
        self.lbl_bat = ctk.CTkLabel(bt, text="—", text_color=MUTED, width=190, anchor="e", font=ctk.CTkFont("Segoe UI", 12)); self.lbl_bat.pack(side="right")
        self.bar_bat = ctk.CTkProgressBar(bt, width=170, height=10, progress_color=OK, fg_color=EDGE); self.bar_bat.set(0); self.bar_bat.pack(side="right", padx=10)

        av = ctk.CTkFrame(tab, fg_color="transparent"); av.pack(fill="x", padx=18, pady=(10, 0))
        ctk.CTkLabel(av, text="Avisar cuando la batería esté baja", text_color=TEXT).pack(side="left")
        self.sw_aviso = ctk.CTkSwitch(av, text="", command=self._cambiar_aviso); self.sw_aviso.pack(side="right")
        self.sl_umbral, self.v_umbral = self._slider(tab, "Avisar al llegar a", 5, 50, 20, self._cambiar_umbral, fmt="{} %")

        hb = ctk.CTkFrame(tab, fg_color="transparent"); hb.pack(fill="x", padx=18, pady=(12, 0))
        ctk.CTkButton(hb, text="Identificar (parpadea la luz)", height=30, fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT, border_width=1, border_color=EDGE, command=lambda: self.bridge.identificar(self.cur)).pack(side="left")
        self.btn_swap = ctk.CTkButton(hb, text="Intercambiar luz/batería con otro mando", height=30, fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT, border_width=1, border_color=EDGE, command=self._intercambiar, state="disabled")
        self.btn_swap.pack(side="left", padx=(8, 0))
        ctk.CTkLabel(tab, text="Con varios mandos, usa Identificar: si parpadea otro mando, pulsa Intercambiar.", text_color=MUTED, wraplength=540, justify="left", anchor="w", font=ctk.CTkFont("Segoe UI", 11)).pack(fill="x", padx=18, pady=(6, 0))

    def _cambiar_aviso(self):
        self.cur.aviso_bateria = bool(self.sw_aviso.get())
        self.cur.aviso_hecho = False

    def _cambiar_umbral(self, v):
        self.cur.umbral_bateria = int(v)
        self.cur.aviso_hecho = False

    def _build_audio_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent"); top.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(top, text="Transmitir PC al Mando", font=ctk.CTkFont("Segoe UI", 14, "bold"), text_color=TEXT).pack(side="left")
        self.sw_a = ctk.CTkSwitch(top, text="", command=lambda: setattr(self.cur, "audio_enabled", bool(self.sw_a.get()))); self.sw_a.pack(side="right")
        self.sl_vol, self.v_vol = self._slider(tab, "Volumen", 0, VOL_MAX, 60, lambda v: setattr(self.cur, "volumen", int(v)))

        d = ctk.CTkFrame(tab, fg_color="transparent"); d.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(d, text="Salida", text_color=TEXT).pack(side="left")
        self.seg_dest = ctk.CTkSegmentedButton(d, values=["Altavoz Interno", "Jack Auriculares"], command=lambda v: setattr(self.cur, "destino", DEST_JACK if v == "Jack Auriculares" else DEST_ALTAVOZ))
        self.seg_dest.set("Altavoz Interno"); self.seg_dest.pack(side="right")

        vr = ctk.CTkFrame(tab, fg_color="transparent"); vr.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(vr, text="Variante del protocolo", text_color=TEXT).pack(side="left")
        self.nombres_var = [f"{k} · {VARIANTES[k]['nombre']}" for k in VARIANTES]
        self.opt_var = ctk.CTkOptionMenu(vr, values=self.nombres_var, width=330, dynamic_resizing=False, fg_color=BTN, button_color=BTN_HOVER, button_hover_color=BTN_HOVER, text_color=TEXT, command=lambda s: setattr(self.cur, "variante", int(s.split()[0])))
        self.opt_var.set(self.nombres_var[0]); self.opt_var.pack(side="right")

        pb = ctk.CTkFrame(tab, fg_color="transparent"); pb.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkButton(pb, text="Probar tono (3 s)", fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT, border_width=1, border_color=EDGE, height=34, command=lambda: self.cur.probar_tono()).pack(side="left")
        ctk.CTkLabel(pb, text="   Funciona sin activar la transmisión del PC", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 11)).pack(side="left")

        self.lbl_audio = ctk.CTkLabel(tab, text="", text_color=MUTED, wraplength=520, justify="left", font=ctk.CTkFont("Segoe UI", 12))
        self.lbl_audio.pack(anchor="w", padx=18, pady=(14, 0))

    def _build_led_tab(self, tab):
        try:
            self._build_led_tab_inner(tab)
        except Exception:
            import traceback
            ctk.CTkLabel(tab, text="No se pudo crear la pestaña Luz LED:\n" + traceback.format_exc(),
                         text_color=BAD, justify="left", anchor="w", wraplength=540,
                         font=ctk.CTkFont("Consolas", 11)).pack(fill="x", padx=18, pady=12)

    def _build_led_tab_inner(self, tab):
        m = self.cur
        ctk.CTkLabel(tab, text="Modo de luz", font=ctk.CTkFont("Segoe UI", 14, "bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(10, 4))

        grid = ctk.CTkFrame(tab, fg_color="transparent"); grid.pack(fill="x", padx=14)
        self.led_btns = {}
        for i, (key, texto, _desc) in enumerate(LED_MODOS):
            btn = ctk.CTkButton(grid, text=texto, height=34, corner_radius=8, text_color=TEXT,
                                fg_color=BTN, hover_color=BTN_HOVER, border_width=1, border_color=EDGE,
                                command=lambda k=key: self._set_led_mode(k))
            btn.grid(row=i // 3, column=i % 3, padx=4, pady=3, sticky="ew")
            self.led_btns[key] = btn
        for c in range(3): grid.grid_columnconfigure(c, weight=1, uniform="ledmodo")

        self.lbl_led_desc = ctk.CTkLabel(tab, text="", text_color=MUTED, justify="left", anchor="w", wraplength=540, font=ctk.CTkFont("Segoe UI", 12))
        self.lbl_led_desc.pack(fill="x", padx=18, pady=(6, 2))

        ctk.CTkLabel(tab, text="Color", font=ctk.CTkFont("Segoe UI", 14, "bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(8, 0))
        b = ctk.CTkFrame(tab, fg_color="transparent"); b.pack(fill="x")
        left = ctk.CTkFrame(b, fg_color="transparent"); left.pack(side="left", padx=(18, 4), pady=(4, 0), anchor="n")
        self.swatch = ctk.CTkFrame(left, width=64, height=64, corner_radius=12, fg_color=to_hex(m.led_color)); self.swatch.pack()
        ctk.CTkButton(left, text="Elegir...", width=64, height=26, fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT, border_width=1, border_color=EDGE, command=self._pick_color).pack(pady=(6, 0))
        sl = ctk.CTkFrame(b, fg_color="transparent"); sl.pack(side="left", fill="x", expand=True)
        self.sl_r, self.vr = self._slider(sl, "Rojo", 0, 255, m.led_color[0], self._rgb, w=170)
        self.sl_g, self.vg = self._slider(sl, "Verde", 0, 255, m.led_color[1], self._rgb, w=170)
        self.sl_b, self.vb = self._slider(sl, "Azul", 0, 255, m.led_color[2], self._rgb, w=170)

        pr = ctk.CTkFrame(tab, fg_color="transparent"); pr.pack(fill="x", padx=18, pady=(10, 2))
        for c in LED_PRESETS:
            ctk.CTkButton(pr, text="", width=34, height=26, fg_color=to_hex(scale_rgb(c, 0.8)), hover_color=to_hex(c), command=lambda c=c: self._preset(c)).pack(side="left", padx=(0, 6))

        ctk.CTkLabel(tab, text="Ajustes", font=ctk.CTkFont("Segoe UI", 14, "bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(8, 0))
        self.sl_bri, self.v_bri = self._slider(tab, "Brillo", 0, 100, 100, lambda v: setattr(self.cur, "led_brightness", v / 100.0), fmt="{} %")
        self.sl_vel, self.v_vel = self._slider(tab, "Velocidad del arcoíris", 2, 100, int(m.led_speed * 100), lambda v: setattr(self.cur, "led_speed", v / 100.0), fmt="{} %")
        self.sl_sens, self.v_sens = self._slider(tab, "Sensibilidad al sonido", 20, 300, 100, lambda v: setattr(self.cur, "led_sens", v / 100.0), fmt="{} %")

        ctk.CTkLabel(tab, text="En vivo", font=ctk.CTkFont("Segoe UI", 14, "bold"), text_color=TEXT).pack(anchor="w", padx=18, pady=(8, 2))
        lv = ctk.CTkFrame(tab, fg_color="transparent"); lv.pack(fill="x", padx=18)
        self.live = ctk.CTkFrame(lv, width=64, height=14, corner_radius=7, fg_color=OFF); self.live.pack(side="left")
        self.bar_nivel = ctk.CTkProgressBar(lv, height=10, progress_color=ACCENT, fg_color=EDGE); self.bar_nivel.set(0)
        self.bar_nivel.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.lbl_led_info = ctk.CTkLabel(tab, text="", text_color=MUTED, justify="left", anchor="w", wraplength=540, font=ctk.CTkFont("Segoe UI", 12))
        self.lbl_led_info.pack(fill="x", padx=18, pady=(6, 0))

        self._set_led_mode(m.led_mode, aplicar=False)

    def _set_led_mode(self, key, aplicar=True):
        if aplicar: self.cur.led_mode = key
        for k, btn in self.led_btns.items():
            btn.configure(fg_color=ACCENT if k == key else BTN)
        self.lbl_led_desc.configure(text=next((d for kk, _t, d in LED_MODOS if kk == key), ""))

    def _rgb(self, _=None):
        rgb = (int(self.sl_r.get()), int(self.sl_g.get()), int(self.sl_b.get()))
        self.cur.led_color = rgb; self.swatch.configure(fg_color=to_hex(rgb))
        if self.cur.led_mode in ("none", "rainbow", "audio_rainbow"): self._set_led_mode("solid")

    def _preset(self, rgb):
        for sl, lbl, v in ((self.sl_r, self.vr, rgb[0]), (self.sl_g, self.vg, rgb[1]), (self.sl_b, self.vb, rgb[2])):
            self._sync_slider(sl, lbl, v)
        self._rgb()

    def _pick_color(self):
        res = colorchooser.askcolor(color=to_hex(self.cur.led_color), title="Color de la luz", parent=self)
        if res and res[0]: self._preset(tuple(int(v) for v in res[0]))

    def _elegir_mando(self, etiqueta):
        try: self.sel = max(0, min(MAX_MANDOS - 1, int(etiqueta.split()[1]) - 1))
        except Exception: return
        self._sync_ui()

    def _sync_ui(self):
        m = self.cur
        (self.sw.select if m.enabled else self.sw.deselect)()
        self._sync_slider(self.sl_dead, self.v_dead, m.deadzone * 100)
        (self.sw_aviso.select if m.aviso_bateria else self.sw_aviso.deselect)()
        self._sync_slider(self.sl_umbral, self.v_umbral, m.umbral_bateria)
        (self.sw_a.select if m.audio_enabled else self.sw_a.deselect)()
        self._sync_slider(self.sl_vol, self.v_vol, m.volumen)
        self.seg_dest.set("Jack Auriculares" if m.destino == DEST_JACK else "Altavoz Interno")
        self.opt_var.set(next((n for n in self.nombres_var if int(n.split()[0]) == m.variante), self.nombres_var[0]))
        try:
            for sl, lbl, v in ((self.sl_r, self.vr, m.led_color[0]), (self.sl_g, self.vg, m.led_color[1]), (self.sl_b, self.vb, m.led_color[2])):
                self._sync_slider(sl, lbl, v)
            self.swatch.configure(fg_color=to_hex(m.led_color))
            self._sync_slider(self.sl_bri, self.v_bri, m.led_brightness * 100)
            self._sync_slider(self.sl_vel, self.v_vel, m.led_speed * 100)
            self._sync_slider(self.sl_sens, self.v_sens, m.led_sens * 100)
            self._set_led_mode(m.led_mode, aplicar=False)
        except AttributeError:
            pass
        self._bat_clave = None

    def _intercambiar(self):
        otros = [x for x in self.bridge.mandos if x.conectado and x is not self.cur]
        if self.cur.conectado and otros:
            self.bridge.intercambiar_hid(self.cur, otros[0])

    @staticmethod
    def _texto_bateria(b, corto=False):
        if b is None: return "No disponible"
        if b["pct"] is None: return "Por cable"
        txt = f"{'~' if b['aprox'] else ''}{b['pct']} %"
        if corto: return txt + (" ⚡" if b["carga"] and not b["completa"] else "")
        if b["completa"] and b["carga"]: txt += " · carga completa"
        elif b["carga"]: txt += " · cargando"
        return txt

    def _pintar_bateria(self, b, m):
        if not m.conectado or b is None or b["pct"] is None:
            self.bar_bat.set(1.0 if (m.conectado and b is not None and b["cable"]) else 0)
            self.bar_bat.configure(progress_color=ACCENT)
            self.lbl_bat.configure(text="—" if not m.conectado else self._texto_bateria(b))
            return
        pct = b["pct"]
        self.bar_bat.set(pct / 100.0)
        self.bar_bat.configure(progress_color=OK if pct > 40 or b["carga"] else ("#a68a4e" if pct > 15 else BAD))
        self.lbl_bat.configure(text=self._texto_bateria(b))

    def _revisar_bateria(self, m):
        b = m.bateria
        if not m.conectado or not m.aviso_bateria or b is None or b["pct"] is None:
            return
        if b["carga"] or b["pct"] > m.umbral_bateria + 5:
            m.aviso_hecho = False
        elif b["pct"] <= m.umbral_bateria and not m.aviso_hecho:
            m.aviso_hecho = True
            self._avisar_bateria(m, b)

    def _avisar_bateria(self, m, b):
        win = ctk.CTkToplevel(self)
        win.title("Batería baja")
        win.geometry("360x160")
        win.resizable(False, False)
        win.configure(fg_color=BG)
        win.attributes("-topmost", True)
        ctk.CTkLabel(win, text=f"Batería baja en el Mando {m.idx + 1}", text_color=TEXT, font=ctk.CTkFont("Segoe UI", 16, "bold")).pack(pady=(26, 4))
        ctk.CTkLabel(win, text=f"Queda {'~' if b['aprox'] else ''}{b['pct']} %. Conéctalo al cargador.", text_color=MUTED, font=ctk.CTkFont("Segoe UI", 13)).pack()
        ctk.CTkButton(win, text="Aceptar", width=110, fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT, border_width=1, border_color=EDGE, command=win.destroy).pack(pady=(20, 0))
        win.after(100, win.lift)

    def _refresh(self):
        try:
            self._refresh_inner()
        finally:
            self.after(80 if self.bridge.low_latency else 33, self._refresh)

    def _refresh_inner(self):
        b = self.bridge
        conectados = [x for x in b.mandos if x.conectado]
        for x in conectados:
            self._revisar_bateria(x)
        if not self.cur.conectado and conectados:
            self.sel = conectados[0].idx
            self._sync_ui()
        m = self.cur; s = m.snap

        etiquetas = tuple(self._etiqueta(x) for x in b.mandos if x.conectado or x is m)
        if etiquetas != self._etiquetas:
            self.opt_mando.configure(values=list(etiquetas)); self._etiquetas = etiquetas
        et = self._etiqueta(m)
        if self.opt_mando.get() != et: self.opt_mando.set(et)

        enviando = s["connected"] and m.enabled
        self.dot_in.configure(text_color=OK if s["connected"] else WAIT)
        self.dot_out.configure(text_color=BAD if not s["pad_ok"] and b.error else (OK if enviando else WAIT))
        txt = "Xbox 360 virtual: " + ("enviando" if enviando else "en espera")
        if len(conectados) > 1:
            txt += "  ·  Batería: " + "  ".join(f"{x.idx + 1}→{self._texto_bateria(x.bateria, True)}" for x in conectados)
        self.lbl_out.configure(text=txt)
        self.lbl_err.configure(text=b.error)
        self.preview.update_state(s)

        clave = (m.idx, m.conectado, tuple(sorted(m.bateria.items())) if m.bateria else None)
        if clave != self._bat_clave:
            self._bat_clave = clave
            self._pintar_bateria(m.bateria, m)
        if len(conectados) != self._n_conectados:
            self._n_conectados = len(conectados)
            self.btn_swap.configure(state="normal" if len(conectados) > 1 else "disabled")

        self._refresh_led(m, s)

        ok, fallos, rc, err = m.audio_stats
        if not s["led"]:
            diag = "El mando aún no está abierto por HID. Conéctalo por Bluetooth."
        elif m.audio_enabled or time.time() < m.tone_until:
            diag = f"Enviando audio · reportes: {ok} · fallidos: {fallos} · bytes por escritura: {rc}"
        else:
            diag = "Audio apagado."
        if m.audio_error: diag += "\n" + m.audio_error
        if b.audio_error: diag += "\n" + b.audio_error
        self.lbl_audio.configure(text=diag)

    def _refresh_led(self, m, s):
        try:
            if self.tabs.get() != "Luz LED" or not hasattr(self, "live"): return
            b = self.bridge
            if m.led_mode != "none" and s["led_rgb"]: self.live.configure(fg_color=to_hex(s["led_rgb"]))
            else: self.live.configure(fg_color=OFF)
            self.bar_nivel.set(m.led_engine.env if m.led_mode in LED_CON_AUDIO else 0)
            ok, fallos, err = m.led_stats
            if not s["led"]:
                info = "Conecta el mando DualShock por Bluetooth para controlar su luz."
            elif m.led_mode == "none":
                info = "Elige un modo para empezar."
            else:
                info = f"Luz enviada al mando · envíos: {ok} · fallidos: {fallos}"
                if err: info += f" · {err}"
                if m.led_mode in LED_CON_AUDIO:
                    info += "\n" + (b.audio_error if b.audio_error else "Capturando el sonido del PC.")
            self.lbl_led_info.configure(text=info)
        except Exception:
            pass

    def _close(self): self.bridge.stop(); self.destroy()

if __name__ == "__main__": App().mainloop()
