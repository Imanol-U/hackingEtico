"""Servidor ligero de descarga de archivos (solo librería estándar de Python).

Endpoints:
  GET /health                -> {"status": "ok"}
  GET /files                 -> lista de archivos disponibles (JSON)
  GET /download/<ruta>       -> descarga el archivo indicado

Variables de entorno:
  FILES_DIR  carpeta con los archivos a servir (por defecto ./archivos)
  HOST       interfaz de escucha (por defecto 0.0.0.0)
  PORT       puerto (por defecto 8000)
  API_KEY    opcional; si se define, se exige la cabecera X-API-Key o ?key=
"""
import json
import mimetypes
import os
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

FILES_DIR = Path(os.getenv("FILES_DIR", Path(__file__).parent / "archivos")).resolve()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
API_KEY = os.getenv("API_KEY", "")


class Handler(BaseHTTPRequestHandler):
    server_version = "FileServer/1.0"

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query):
        if not API_KEY:
            return True
        return self.headers.get("X-API-Key") == API_KEY or query.get("key", [""])[0] == API_KEY

    def _safe_path(self, rel):
        target = (FILES_DIR / unquote(rel)).resolve()
        if target != FILES_DIR and FILES_DIR in target.parents and target.is_file():
            return target
        return None

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path == "/health":
            return self._json(200, {"status": "ok"})

        if not self._authorized(query):
            return self._json(401, {"error": "no autorizado"})

        if url.path == "/files":
            files = [
                {"name": p.relative_to(FILES_DIR).as_posix(), "size": p.stat().st_size}
                for p in sorted(FILES_DIR.rglob("*")) if p.is_file()
            ]
            return self._json(200, files)

        if url.path.startswith("/download/"):
            path = self._safe_path(url.path[len("/download/"):])
            if not path:
                return self._json(404, {"error": "archivo no encontrado"})
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile)
            return

        self._json(404, {"error": "ruta no encontrada"})


if __name__ == "__main__":
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Sirviendo {FILES_DIR} en http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
