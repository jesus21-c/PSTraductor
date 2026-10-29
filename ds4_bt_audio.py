import argparse
import math
import queue
import struct
import sys
import threading
import time
import zlib

import numpy as np

BITPOOL = 25
BLOCKS = 16
SUBBANDS = 8
FRAME_SAMPLES = BLOCKS * SUBBANDS
FRAME_BYTES = 112
HEADER_BYTE1 = (0x01 << 6) | (0x03 << 4) | (0x01 << 2) | (0x00 << 1) | 0x01
OFFSET8_32K = (-3, 0, 0, 0, 0, 0, 1, 2)

PROTO = np.array([
    0.00000000E+00, 1.56575398E-04, 3.43256425E-04, 5.54620202E-04,
    8.23919506E-04, 1.13992507E-03, 1.47640169E-03, 1.78371725E-03,
    2.01182542E-03, 2.10371989E-03, 1.99454554E-03, 1.61656283E-03,
    9.02154502E-04, -1.78805361E-04, -1.64973098E-03, -3.49717454E-03,
    5.65949473E-03, 8.02941163E-03, 1.04584443E-02, 1.27472335E-02,
    1.46525263E-02, 1.59045603E-02, 1.62208471E-02, 1.53184106E-02,
    1.29371806E-02, 8.85757540E-03, 2.92408442E-03, -4.91578024E-03,
    -1.46404076E-02, -2.61098752E-02, -3.90751381E-02, -5.31873032E-02,
    6.79989431E-02, 8.29847578E-02, 9.75753918E-02, 1.11196689E-01,
    1.23264548E-01, 1.33264415E-01, 1.40753505E-01, 1.45389847E-01,
    1.46955068E-01, 1.45389847E-01, 1.40753505E-01, 1.33264415E-01,
    1.23264548E-01, 1.11196689E-01, 9.75753918E-02, 8.29847578E-02,
    -6.79989431E-02, -5.31873032E-02, -3.90751381E-02, -2.61098752E-02,
    -1.46404076E-02, -4.91578024E-03, 2.92408442E-03, 8.85757540E-03,
    1.29371806E-02, 1.53184106E-02, 1.62208471E-02, 1.59045603E-02,
    1.46525263E-02, 1.27472335E-02, 1.04584443E-02, 8.02941163E-03,
    -5.65949473E-03, -3.49717454E-03, -1.64973098E-03, -1.78805361E-04,
    9.02154502E-04, 1.61656283E-03, 1.99454554E-03, 2.10371989E-03,
    2.01182542E-03, 1.78371725E-03, 1.47640169E-03, 1.13992507E-03,
    8.23919506E-04, 5.54620202E-04, 3.43256425E-04, 1.56575398E-04,
])
ANA = np.array([[math.cos((i + 0.5) * (k - 4) * math.pi / 8) for k in range(16)]
                for i in range(8)])


def _crc8_table():
    tabla = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = ((c << 1) ^ 0x1D) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        tabla.append(c)
    return tabla


_CRC8 = _crc8_table()


def crc8(datos):
    c = 0x0F
    for b in datos:
        c = _CRC8[c ^ b]
    return c


_cache_bits = {}


def _reparto_bits(sf):
    clave = tuple(sf)
    r = _cache_bits.get(clave)
    if r is not None:
        return r
    bitneed = []
    for s, off in zip(clave, OFFSET8_32K):
        if s == 0:
            bitneed.append(-5)
        else:
            lou = s - off
            bitneed.append(lou // 2 if lou > 0 else lou)
    bitslice = max(0, max(bitneed)) + 1
    bitcount = 0
    slicecount = 0
    while True:
        bitslice -= 1
        bitcount += slicecount
        slicecount = 0
        for bn in bitneed:
            if bitslice + 1 < bn < bitslice + 16:
                slicecount += 1
            elif bn == bitslice + 1:
                slicecount += 2
        if bitcount + slicecount >= BITPOOL:
            break
    if bitcount + slicecount == BITPOOL:
        bitcount += slicecount
        bitslice -= 1
    bits = [0 if bn < bitslice + 2 else min(bn - bitslice, 16) for bn in bitneed]
    sb = 0
    while bitcount < BITPOOL and sb < SUBBANDS:
        if 2 <= bits[sb] < 16:
            bits[sb] += 1
            bitcount += 1
        elif bitneed[sb] == bitslice + 1 and BITPOOL > bitcount + 1:
            bits[sb] = 2
            bitcount += 2
        sb += 1
    sb = 0
    while bitcount < BITPOOL and sb < SUBBANDS:
        if bits[sb] < 16:
            bits[sb] += 1
            bitcount += 1
        sb += 1
    r = tuple(bits)
    _cache_bits[clave] = r
    return r


class CodificadorSBC:
    def __init__(self):
        self.cola = np.zeros((72, 2))

    def codificar(self, pcm):
        n_tramas = len(pcm) // FRAME_SAMPLES
        if n_tramas == 0:
            return []
        pcm = pcm[:n_tramas * FRAME_SAMPLES]
        xp = np.concatenate([self.cola, pcm], axis=0)
        self.cola = xp[-72:].copy()

        sub = []
        for ch in range(2):
            ventanas = np.lib.stride_tricks.sliding_window_view(xp[:, ch], 80)[::8]
            z = ventanas[:, ::-1] * PROTO
            y = z.reshape(-1, 5, 16).sum(axis=1)
            sub.append((y @ ANA.T).reshape(n_tramas, BLOCKS, SUBBANDS))
        arr = np.stack(sub, axis=2)

        mx = np.abs(arr).max(axis=1)
        sf = np.clip(np.ceil(np.log2(np.maximum(mx, 1e-12))) - 1, 0, 15).astype(np.int64)
        sfac = 2.0 ** (sf + 1)

        j16 = np.arange(16)
        tramas = []
        for f in range(n_tramas):
            bits = np.array([_reparto_bits(sf[f, c]) for c in range(2)])
            niveles = (1 << bits) - 1
            q = np.floor((arr[f] / sfac[f] + 1.0) * niveles / 2.0)
            q = np.clip(q, 0, niveles).astype(np.int64).reshape(256)
            nb = np.tile(bits.reshape(16), BLOCKS)
            mascara = j16[None, :] < nb[:, None]
            desp = np.where(mascara, nb[:, None] - 1 - j16[None, :], 0)
            bitsmat = (q[:, None] >> desp) & 1
            audio = np.packbits(bitsmat[mascara].astype(np.uint8)).tobytes()

            nib = sf[f].reshape(16)
            sfbytes = bytes(((nib[0::2] << 4) | nib[1::2]).astype(np.uint8))
            crc = crc8(bytes([HEADER_BYTE1, BITPOOL]) + sfbytes)
            trama = bytes([0x9C, HEADER_BYTE1, BITPOOL, crc]) + sfbytes + audio
            tramas.append(trama.ljust(FRAME_BYTES, b"\x00")[:FRAME_BYTES])
        return tramas


SONY_VID = 0x054C
DS4_PIDS = (0x05C4, 0x09CC)
DEST_JACK = 0x24
DEST_ALTAVOZ = 0x02
VOL_MAX = 79

TAM_REPORTE = {0x11: 78, 0x14: 270, 0x15: 334, 0x17: 462}


def _var(nombre, **kw):
    base = dict(modo=0xA0, extras=True, flags=0xF3, crc=True, patron="17", b1=0x40)
    base.update(kw)
    base["nombre"] = nombre
    return base


VARIANTES = {
    1: _var("Documentada: modo A0, bytes 04/85, reporte 0x17 con CRC"),
    2: _var("Igual que 1, sin CRC", crc=False),
    3: _var("Patrón 0x14,0x14,0x15 del artículo original, sin CRC", patron="14_15", crc=False),
    4: _var("Patrón 0x14,0x14,0x15 con CRC", patron="14_15"),
    5: _var("Sin bytes 04/85 (modo A0, flags F2)", extras=False, flags=0xF2),
    6: _var("Versión anterior (modo 20, flags F2, sin 04/85)", modo=0x20, extras=False, flags=0xF2),
    7: _var("Igual que 1 con byte 1 = C0 en el 0x17", b1=0xC0),
}


def _con_crc(buf):
    buf[-4:] = struct.pack("<I", zlib.crc32(b"\xA2" + bytes(buf[:-4])) & 0xFFFFFFFF)
    return bytes(buf)


def _cierre(buf, v):
    return _con_crc(buf) if v["crc"] else bytes(buf)


def reporte_11(vol_jack, vol_altavoz, v, rgb=None, rumble_ok=False):
    r = bytearray(TAM_REPORTE[0x11])
    r[0], r[1], r[2] = 0x11, 0xC0, v["modo"]
    flags = v["flags"]
    if rgb is None:
        flags &= ~0x02
    if not rumble_ok:
        flags &= ~0x01
    r[3] = flags
    if v["extras"]:
        r[4] = 0x04
        r[25] = 0x85
    if rgb is not None:
        r[8], r[9], r[10] = int(rgb[0]), int(rgb[1]), int(rgb[2])
    r[21] = r[22] = max(0, min(255, int(vol_jack)))
    r[23] = 0
    r[24] = max(0, min(255, int(vol_altavoz)))
    return _cierre(r, v)


def reporte_17(contador, destino, audio, v):
    r = bytearray(TAM_REPORTE[0x17])
    r[0], r[1], r[2] = 0x17, v["b1"], 0xA0
    struct.pack_into("<H", r, 3, contador & 0xFFFF)
    r[5] = destino
    r[6:6 + len(audio)] = audio
    return _cierre(r, v)


def reporte_14(contador, destino, audio, v):
    r = bytearray(TAM_REPORTE[0x14])
    r[0], r[1], r[2] = 0x14, v["b1"], 0xA0
    struct.pack_into("<H", r, 3, contador & 0xFFFF)
    r[5] = destino
    r[6:6 + len(audio)] = audio
    return _cierre(r, v)


def reporte_15(contador, destino, audio, v, joy):
    r = bytearray(TAM_REPORTE[0x15])
    r[0], r[1], r[2] = 0x15, 0xC0, 0xA0
    r[3:3 + len(joy)] = joy[:75]
    struct.pack_into("<H", r, 78, contador & 0xFFFF)
    r[80] = destino
    r[81:81 + len(audio)] = audio
    return _cierre(r, v)


def esperar_hasta(t):
    while True:
        d = t - time.perf_counter()
        if d <= 0:
            return
        if d > 0.003:
            time.sleep(d - 0.002)


class Transmisor:
    def __init__(self, dev, variante=1, destino=DEST_ALTAVOZ):
        self.dev = dev
        self.v = VARIANTES.get(variante, VARIANTES[1])
        self.destino = destino
        self.contador = 0
        self.n_pkt = 0
        self.ok = 0
        self.fallos = 0
        self.seguidos = 0
        self.rc = 0
        self.ultimo_error = ""
        self._joy = bytes(75)

    def _escribir(self, datos):
        try:
            n = self.dev.write(datos)
        except Exception as e:
            n = -1
            self.ultimo_error = repr(e)
        if n is None or n < 0:
            self.fallos += 1
            self.seguidos += 1
            try:
                msg = self.dev.error()
                if msg:
                    self.ultimo_error = str(msg)
            except Exception:
                pass
            return False
        self.ok += 1
        self.seguidos = 0
        self.rc = n
        return True

    def armar(self, vol_jack, vol_altavoz, rgb=None, rumble_ok=False):
        sin_crc = dict(self.v, crc=False)
        self._joy = reporte_11(vol_jack, vol_altavoz, sin_crc, rgb, rumble_ok)[3:78]
        return self._escribir(reporte_11(vol_jack, vol_altavoz, self.v, rgb, rumble_ok))

    def enviar(self, bloque):
        if self.v["patron"] == "17":
            ok = self._escribir(reporte_17(self.contador, self.destino, bloque, self.v))
            self.contador = (self.contador + 4) & 0xFFFF
            return ok
        ok = True
        for k in range(2):
            parte = bloque[k * 224:(k + 1) * 224]
            if self.n_pkt % 3 == 2:
                pkt = reporte_15(self.contador, self.destino, parte, self.v, self._joy)
            else:
                pkt = reporte_14(self.contador, self.destino, parte, self.v)
            ok = self._escribir(pkt) and ok
            self.contador = (self.contador + 2) & 0xFFFF
            self.n_pkt += 1
            if k == 0:
                esperar_hasta(time.perf_counter() + 0.008)
        return ok

    def resumen(self):
        txt = f"reportes escritos: {self.ok}, fallidos: {self.fallos}, bytes por escritura: {self.rc}"
        if self.ultimo_error:
            txt += f", último error: {self.ultimo_error}"
        return txt


def a_32k(datos, sr):
    if sr == 32000:
        return datos
    try:
        from scipy.signal import resample_poly
        g = math.gcd(sr, 32000)
        return resample_poly(datos, 32000 // g, sr // g, axis=0)
    except ImportError:
        pass
    if sr > 32000:
        fc = 15000 / sr
        n = np.arange(-50, 51)
        h = np.sinc(2 * fc * n) * np.hamming(101)
        h /= h.sum()
        datos = np.stack([np.convolve(datos[:, c], h, mode="same")
                          for c in range(datos.shape[1])], axis=1)
    n_out = int(len(datos) * 32000 / sr)
    x = np.linspace(0, len(datos) - 1, n_out)
    return np.stack([np.interp(x, np.arange(len(datos)), datos[:, c])
                     for c in range(datos.shape[1])], axis=1)


def cargar_audio(ruta):
    import soundfile as sf
    datos, sr = sf.read(ruta, dtype="float64", always_2d=True)
    if datos.shape[1] == 1:
        datos = np.tile(datos, (1, 2))
    datos = a_32k(datos[:, :2], sr)
    return np.clip(datos, -1.0, 1.0) * 32767.0


def generar_tono(freq, dur):
    t = np.arange(int(32000 * dur)) / 32000.0
    s = 0.3 * np.sin(2 * np.pi * freq * t) * 32767.0
    return np.stack([s, s], axis=1)


class Remuestreador:
    def __init__(self, sr_in):
        self.paso = sr_in / 32000.0
        self.pos = 0.0
        self.buf = np.zeros((0, 2))
        self.h = None
        self.zi = None
        if sr_in > 32000:
            n = np.arange(-50, 51)
            h = np.sinc(2 * (15000.0 / sr_in) * n) * np.hamming(101)
            self.h = h / h.sum()
            self.zi = np.zeros((100, 2))

    def procesar(self, x):
        if self.h is not None:
            from scipy.signal import lfilter
            x, self.zi = lfilter(self.h, [1.0], x, axis=0, zi=self.zi)
        buf = np.concatenate([self.buf, x], axis=0)
        n_out = int(math.ceil((len(buf) - 1 - self.pos) / self.paso))
        if n_out <= 0:
            self.buf = buf
            return np.zeros((0, 2))
        idx = self.pos + np.arange(n_out) * self.paso
        i0 = np.floor(idx).astype(int)
        fr = (idx - i0)[:, None]
        out = buf[i0] * (1 - fr) + buf[i0 + 1] * fr
        sig = self.pos + n_out * self.paso
        quitar = int(math.floor(sig))
        self.buf = buf[quitar:]
        self.pos = sig - quitar
        return out


class BufferPCM:
    def __init__(self, maximo):
        self.lock = threading.Lock()
        self.datos = np.zeros((0, 2))
        self.maximo = maximo

    def poner(self, x):
        with self.lock:
            self.datos = np.concatenate([self.datos, x], axis=0)[-self.maximo:]

    def tomar(self, n):
        with self.lock:
            if len(self.datos) < n:
                return None
            out, self.datos = self.datos[:n], self.datos[n:]
            return out


def listar_audio():
    import soundcard as sc
    pred = sc.default_speaker().name
    for s in sc.all_speakers():
        print(("* " if s.name == pred else "  ") + s.name)


def captura(buf, parar, sr, nombre):
    try:
        import soundcard as sc
        if nombre:
            elegidas = [s for s in sc.all_speakers() if nombre.lower() in s.name.lower()]
            if not elegidas:
                print("No encontré esa salida de audio. Usa --listar-audio.", file=sys.stderr)
                parar.set()
                return
            alt = elegidas[0]
        else:
            alt = sc.default_speaker()
        mic = sc.get_microphone(id=str(alt.name), include_loopback=True)
        print("Capturando el audio de:", alt.name)
        rem = Remuestreador(sr)
        with mic.recorder(samplerate=sr, channels=2) as rec:
            while not parar.is_set():
                datos = rec.record(numframes=sr // 50)
                out = rem.procesar(np.asarray(datos, dtype=np.float64))
                buf.poner(np.clip(out, -1.0, 1.0) * 32767.0)
    except Exception as e:
        print("Error de captura:", repr(e), file=sys.stderr)
        parar.set()


def preparar(audio):
    bloque = FRAME_SAMPLES * 4
    falta = (-len(audio)) % bloque
    return np.concatenate([audio, np.zeros((falta + bloque, 2))], axis=0)


def productor(audio, cola):
    enc = CodificadorSBC()
    paso = FRAME_SAMPLES * 4 * 8
    for i in range(0, len(audio), paso):
        tramas = enc.codificar(audio[i:i + paso])
        for k in range(0, len(tramas), 4):
            cola.put(b"".join(tramas[k:k + 4]))
    cola.put(None)


def listar():
    import hid
    for d in hid.enumerate(SONY_VID, 0):
        print(f"VID={d['vendor_id']:04X} PID={d['product_id']:04X} "
              f"bus={d.get('bus_type', '?')} uso={d.get('usage_page')}/{d.get('usage')} "
              f"{d.get('product_string')}\n   {d['path']!r}")


def abrir(ruta=None):
    import hid
    dev = hid.device()
    if ruta:
        dev.open_path(ruta.encode())
        return dev
    candidatos = [d for d in hid.enumerate(SONY_VID, 0) if d["product_id"] in DS4_PIDS]
    if not candidatos:
        raise SystemExit("No encontré un DualShock 4. Usa --listar o --ruta.")
    if len(candidatos) > 1:
        print(f"Aviso: hay {len(candidatos)} entradas HID del mando; uso la de gamepad. "
              "Si falla, prueba con --ruta.")
    elegido = next((d for d in candidatos
                    if d.get("usage_page") == 1 and d.get("usage") == 5), candidatos[0])
    if elegido.get("bus_type") == 1:
        print("Aviso: este mando está por USB; para Bluetooth empareja el mando.",
              file=sys.stderr)
    dev.open_path(elegido["path"])
    return dev


def preparar_mando(dev):
    try:
        dev.get_feature_report(0x02, 37)
    except Exception:
        pass
    try:
        dev.set_nonblocking(0)
        primero = dev.read(128, 1500)
    except Exception:
        primero = None
    if not primero:
        print("Aviso: el mando no envió datos en 1,5 s (mueve un botón). ¿Está por Bluetooth?")
    elif primero[0] == 0x11:
        print("Conexión: Bluetooth en modo completo (correcto).")
    elif primero[0] == 0x01 and len(primero) >= 60:
        print("Aviso: parece conexión USB. Para audio por Bluetooth desconecta el cable "
              "y empareja el mando.")
    else:
        print(f"Primer reporte de entrada: 0x{primero[0]:02X}, {len(primero)} bytes.")
    try:
        dev.set_nonblocking(1)
    except Exception:
        pass


def _timer_alto():
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def desde_cola(cola):
    while True:
        d = cola.get()
        if d is None:
            return
        yield d


def bloques_de(audio):
    cola = queue.Queue()
    productor(audio, cola)
    return list(desde_cola(cola))


def transmitir(tx, bloques, parar=None):
    periodo = 0.016
    t0 = time.perf_counter()
    k = 0
    for datos in bloques:
        if parar is not None and parar.is_set():
            break
        esperar_hasta(t0 + k * periodo)
        tx.enviar(datos)
        if tx.seguidos >= 5:
            raise SystemExit("Windows rechaza los reportes de audio: " + tx.resumen() +
                             "\nPrueba otra --variante (usa --barrido) y comprueba que "
                             "el mando esté por Bluetooth.")
        k += 1


def _armar_o_salir(tx, vol):
    if not tx.armar(vol, vol, (0, 0, 48)):
        raise SystemExit("Windows rechazó el reporte de control 0x11: " + tx.resumen() +
                         "\n¿Está el mando por Bluetooth y cerrado DS4Windows/Steam?")
    time.sleep(0.05)


def reproducir(dev, audio, destino, vol, variante):
    _timer_alto()
    preparar_mando(dev)
    tx = Transmisor(dev, variante, destino)
    _armar_o_salir(tx, vol)
    cola = queue.Queue(maxsize=64)
    threading.Thread(target=productor, args=(audio, cola), daemon=True).start()
    try:
        transmitir(tx, desde_cola(cola))
    except KeyboardInterrupt:
        print("\nInterrumpido.")
    finally:
        print("Resultado:", tx.resumen())


def reproducir_vivo(dev, destino, vol, variante, sr_cap, nombre, max_s=None):
    _timer_alto()
    preparar_mando(dev)
    tx = Transmisor(dev, variante, destino)
    _armar_o_salir(tx, vol)

    buf = BufferPCM(512 * 6)
    parar = threading.Event()
    threading.Thread(target=captura, args=(buf, parar, sr_cap, nombre), daemon=True).start()
    time.sleep(0.1)

    enc = CodificadorSBC()
    silencio = np.zeros((512, 2))
    periodo = 0.016
    k = 0
    t0 = time.perf_counter()
    try:
        while not parar.is_set():
            if max_s is not None and k * periodo >= max_s:
                break
            esperar_hasta(t0 + k * periodo)
            pcm = buf.tomar(512)
            if pcm is None:
                pcm = silencio
            tx.enviar(b"".join(enc.codificar(pcm)))
            if tx.seguidos >= 5:
                raise SystemExit("Windows rechaza los reportes de audio: " + tx.resumen())
            k += 1
    except KeyboardInterrupt:
        print("\nInterrumpido.")
    finally:
        parar.set()
        print("Resultado:", tx.resumen())


def barrido(dev, destino, vol, segundos=2.5, pausa=1.5):
    _timer_alto()
    preparar_mando(dev)
    bloques = bloques_de(preparar(generar_tono(440, segundos)))
    nombre = "altavoz" if destino == DEST_ALTAVOZ else "jack"
    print(f"\nBarrido de variantes (salida: {nombre}, volumen {vol}).")
    print("Escucha y anota los números en los que SÍ suena el tono de 440 Hz.\n")
    for num, v in VARIANTES.items():
        print(f">>> VARIANTE {num}: {v['nombre']}")
        tx = Transmisor(dev, num, destino)
        if not tx.armar(vol, vol, (0, 0, 48)):
            print("    Windows rechazó el reporte de control:", tx.resumen())
            continue
        time.sleep(0.1)
        try:
            transmitir(tx, bloques)
        except SystemExit as e:
            print("   ", e)
        print("    ", tx.resumen())
        time.sleep(pausa)
    print("\nListo. Dime en qué variantes se oyó el tono (o 'ninguna') y si salió por "
          "el altavoz o el jack.")


def detener(dev):
    tx = Transmisor(dev, 1, DEST_ALTAVOZ)
    for _ in range(3):
        tx.armar(0, 0, (0, 0, 48), rumble_ok=True)
        time.sleep(0.05)
    print("Enviado: volumen 0 y vibración a cero.", tx.resumen())


def main():
    ap = argparse.ArgumentParser(description="Audio al DualShock 4 por Bluetooth")
    ap.add_argument("archivo", nargs="?", help="WAV/FLAC/OGG a reproducir")
    ap.add_argument("--listar", action="store_true", help="lista dispositivos HID Sony")
    ap.add_argument("--tono", type=float, metavar="HZ", help="tono de prueba")
    ap.add_argument("--duracion", type=float, default=5.0, help="segundos del tono")
    ap.add_argument("--destino", choices=["altavoz", "jack"], default="altavoz")
    ap.add_argument("--volumen", type=int, default=60, help=f"0-{VOL_MAX}")
    ap.add_argument("--variante", type=int, default=1,
                    help="variante del protocolo (1-%d); usa --barrido para probarlas" % len(VARIANTES))
    ap.add_argument("--barrido", action="store_true",
                    help="prueba un tono con cada variante y dime cuáles suenan")
    ap.add_argument("--detener", action="store_true",
                    help="volumen 0 y vibración 0 (si el mando quedó vibrando)")
    ap.add_argument("--ruta", help="ruta HID manual (ver --listar)")
    ap.add_argument("--vivo", action="store_true",
                    help="envía el audio del PC en vivo (loopback de Windows)")
    ap.add_argument("--dispositivo", help="parte del nombre de la salida de audio a capturar")
    ap.add_argument("--listar-audio", action="store_true", help="lista salidas de audio")
    ap.add_argument("--sr-captura", type=int, default=48000,
                    help="frecuencia de captura (48000 o 44100)")
    ap.add_argument("--guardar-sbc", metavar="ARCHIVO",
                    help="solo codifica y guarda SBC crudo (sin mando)")
    a = ap.parse_args()

    if a.variante not in VARIANTES:
        ap.error(f"--variante debe ser 1-{len(VARIANTES)}")
    if a.listar:
        listar()
        return
    if a.listar_audio:
        listar_audio()
        return

    destino = DEST_ALTAVOZ if a.destino == "altavoz" else DEST_JACK
    vol = max(0, min(VOL_MAX, a.volumen))

    if a.detener:
        dev = abrir(a.ruta)
        preparar_mando(dev)
        detener(dev)
        return
    if a.barrido:
        barrido(abrir(a.ruta), destino, vol)
        return
    if a.vivo:
        dev = abrir(a.ruta)
        print(f"Audio del PC en vivo -> mando ({a.destino}, volumen {vol}, "
              f"variante {a.variante}). Ctrl+C para parar")
        reproducir_vivo(dev, destino, vol, a.variante, a.sr_captura, a.dispositivo)
        return

    if a.tono:
        audio = generar_tono(a.tono, a.duracion)
    elif a.archivo:
        audio = cargar_audio(a.archivo)
    else:
        ap.error("indica un archivo, --tono HZ, --vivo, --barrido o --detener")
    audio = preparar(audio)

    if a.guardar_sbc:
        cola = queue.Queue()
        productor(audio, cola)
        with open(a.guardar_sbc, "wb") as f:
            for d in desde_cola(cola):
                f.write(d)
        print("SBC guardado en", a.guardar_sbc)
        return

    dev = abrir(a.ruta)
    print(f"Reproduciendo ({a.destino}, volumen {vol}, variante {a.variante})... Ctrl+C para parar")
    reproducir(dev, audio, destino, vol, a.variante)


if __name__ == "__main__":
    main()
