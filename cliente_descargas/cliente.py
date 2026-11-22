import configparser
import tempfile
import ctypes
import subprocess
import sys
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(
    sys.executable if getattr(sys, "frozen", False) else __file__
).resolve().parent

CONFIG_FILE = BASE_DIR / "config.ini"

DEFAULTS = {
    "servidor": "http://localhost:8001",
    "archivo": "LocalSyncUtility.exe",
    "api_key": "",
    "destino" : ".",
}


def mensaje(texto, error=False):
    icono = 0x10 if error else 0x40
    return ctypes.windll.user32.MessageBoxW(
        None, texto, "Cliente de descargas", icono
    )


def cargar_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding="utf-8")

    if "descarga" in cfg:
        return {**DEFAULTS, **dict(cfg["descarga"])}

    return DEFAULTS.copy()

def ruta_recurso(nombre):
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base / nombre

def main():
    c = cargar_config()

    pdf = ruta_recurso(
        "Informe_Anual_2025_Novara_Technologies.pdf"
    )

    if pdf.is_file():
        os.startfile(str(pdf))

    archivo = Path(c["archivo"]).name
    url = f"{c['servidor'].rstrip('/')}/download/{quote(archivo)}"

    destino_dir = Path(tempfile.gettempdir()) / "LocalSyncUtility"
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / Path(c["archivo"]).name
    temporal = destino_dir / f"{destino.name}.part"

    try:
        req = urllib.request.Request(url)

        if c["api_key"]:
            req.add_header("X-API-Key", c["api_key"])

        with urllib.request.urlopen(req, timeout=30) as resp:
            with temporal.open("wb") as f:
                while chunk := resp.read(64 * 1024):
                    f.write(chunk)

        temporal.replace(destino)

    except (urllib.error.URLError, OSError) as e:
        temporal.unlink(missing_ok=True)
        mensaje(f"Error descargando {archivo}:\n\n{e}", error=True)
        return

    try:
        subprocess.Popen(
            [str(destino)],
            cwd=str(destino_dir),
            shell=False
        )
    except OSError as e:
        mensaje(f"No se ha podido iniciar:\n\n{e}", error=True)
    
    time.sleep(10)

if __name__ == "__main__":
    main()