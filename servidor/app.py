"""
Servidor de ingesta + panel en vivo del monitor de examen.

- POST /ingest                    recibe los lotes JSON del programa (requiere token).
- GET  /                          sesiones (vista por defecto del panel).
- GET  /equipos                   todos los PCs con su resumen.
- GET  /equipo/<id>               un PC: sus sesiones y sus últimos lotes en vivo.
- GET  /sesion/<id>               una sesión: estadísticas, ventanas, lotes y texto.
- GET  /api/sesion/<id>/lotes     los lotes de una sesión en JSON (para analizarlos fuera).

Todo lo que no es /ingest pide usuario/clave del panel.
"""

import os
import re
from datetime import datetime

from flask import (Flask, Response, abort, jsonify, render_template, request)
from jinja2 import DictLoader
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
PANEL_USER = os.environ.get("PANEL_USER", "profe")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
PANEL_TZ = os.environ.get("PANEL_TZ", "Europe/Madrid")   # horas del panel
INACTIVO_ALERTA = int(os.environ.get("ALERTA_SEG", "900"))  # 15 min: aviso en amarillo
# Tras este tiempo sin señal, una captura 'activa' pasa a 'caida' (se dio por
# perdida: cierre brusco, apagón, pérdida de red...). Configurable por entorno.
ABANDONO = int(os.environ.get("ABANDONO_SEG", "14400"))   # 4 h por defecto

pool = ConnectionPool(
    DATABASE_URL, min_size=1, max_size=10,
    kwargs={"autocommit": True, "row_factory": dict_row,
            "options": f"-c timezone={PANEL_TZ}"},
)
app = Flask(__name__)


# ----------------------------- Ingesta -----------------------------

@app.post("/ingest")
def ingest():
    auth = request.headers.get("Authorization", "")
    if not INGEST_TOKEN or auth != f"Bearer {INGEST_TOKEN}":
        return jsonify(error="no autorizado"), 401

    d = request.get_json(silent=True) or {}
    for campo in ("equipo_id", "sesion", "seq"):
        if campo not in d:
            return jsonify(error=f"falta el campo '{campo}'"), 400
    texto = d.get("texto", "") or ""
    fin = bool(d.get("fin"))

    if d.get("latido"):
        # Señal de vida: la captura sigue en marcha aunque no se teclee nada.
        # Solo refresca la actividad; no guarda lote ni toca la numeración.
        with pool.connection() as conn, conn.transaction():
            conn.execute(
                """INSERT INTO equipos (id, nombre) VALUES (%s, %s)
                   ON CONFLICT (id) DO UPDATE
                     SET ultima_vez = now(), nombre = EXCLUDED.nombre""",
                (d["equipo_id"], d.get("equipo_nombre")),
            )
            conn.execute(
                """INSERT INTO sesiones (id, equipo_id)
                   VALUES (%s, %s)
                   ON CONFLICT (id) DO UPDATE
                     SET ultima_actividad = now(),
                         estado = CASE WHEN sesiones.estado = 'finalizada'
                                       THEN 'finalizada' ELSE 'activa' END""",
                (d["sesion"], d["equipo_id"]),
            )
        return jsonify(ok=True)

    with pool.connection() as conn, conn.transaction():
        conn.execute(
            """INSERT INTO equipos (id, nombre) VALUES (%s, %s)
               ON CONFLICT (id) DO UPDATE
                 SET ultima_vez = now(), nombre = EXCLUDED.nombre""",
            (d["equipo_id"], d.get("equipo_nombre")),
        )
        conn.execute(
            """INSERT INTO sesiones (id, equipo_id, ultimo_seq, estado)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE
                 SET ultima_actividad = now(),
                     ultimo_seq = GREATEST(sesiones.ultimo_seq, EXCLUDED.ultimo_seq),
                     estado = CASE WHEN %s THEN 'finalizada' ELSE sesiones.estado END""",
            (d["sesion"], d["equipo_id"], d["seq"],
             "finalizada" if fin else "activa", fin),
        )
        # Se guarda siempre, también el lote vacío de "fin": así la numeración
        # queda completa y cualquier hueco en seq es una pérdida real.
        conn.execute(
            """INSERT INTO lotes (sesion_id, seq, ts_cliente, texto)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (sesion_id, seq) DO NOTHING""",
            (d["sesion"], d["seq"], d.get("ts"), texto),
        )
    return jsonify(ok=True)


# ------------------------- Utilidades panel -------------------------

def _auth_ok():
    a = request.authorization
    return a is not None and a.username == PANEL_USER and a.password == PANEL_PASSWORD


@app.before_request
def _proteger_panel():
    if request.endpoint in ("ingest", "static", None):
        return None
    if not PANEL_PASSWORD or not _auth_ok():
        # Sin PANEL_PASSWORD configurada el panel queda cerrado, no abierto.
        return Response("Acceso restringido", 401,
                        {"WWW-Authenticate": 'Basic realm="Panel de examen"'})
    return None


def _cerrar_abandonadas(conn):
    """Marca como 'caida' toda captura 'activa' que lleve demasiado sin señal.
    Es idempotente; se llama al cargar cada vista del panel."""
    conn.execute(
        """UPDATE sesiones SET estado = 'caida'
           WHERE estado = 'activa'
             AND now() - ultima_actividad > make_interval(secs => %s)""",
        (ABANDONO,))


@app.template_filter("hora")
def _hora(dt, con_fecha=False):
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S" if con_fecha else "%H:%M:%S")


@app.template_filter("dur")
def _dur(seg):
    if seg is None:
        return "—"
    seg = int(seg)
    if seg < 60:
        return f"{seg} s"
    if seg < 3600:
        return f"{seg // 60} min {seg % 60:02d} s"
    return f"{seg // 3600} h {seg % 3600 // 60:02d} min"


RE_CABECERA = re.compile(r"^ (\d\d:\d\d:\d\d) · (.+?) · «(.*)»$", re.M)
RE_ATAJO = re.compile(r"\[Ctrl\+([A-Z0-9])\]")


def marcas_de(texto):
    """Cosas llamativas de un lote: copiar/pegar, cambios de ventana, borrados."""
    m = []
    atajos = RE_ATAJO.findall(texto)
    for letra, nombre in (("V", "pegar"), ("C", "copiar"), ("X", "cortar"),
                          ("Z", "deshacer")):
        n = atajos.count(letra)
        if n:
            m.append((f"{nombre} ×{n}" if n > 1 else nombre,
                      "fuerte" if letra == "V" else ""))
    n = len(RE_CABECERA.findall(texto))
    if n:
        m.append((f"cambio de ventana ×{n}" if n > 1 else "cambio de ventana", "aviso"))
    n = texto.count("⌫")
    if n >= 10:
        m.append((f"{n} borrados", ""))
    return m


def ventanas_de(texto):
    """Lista cronológica de ventanas por las que ha pasado el PC."""
    vistas = []
    for hora, app_, titulo in RE_CABECERA.findall(texto):
        vistas.append({"hora": hora, "app": app_, "titulo": titulo})
    # Tiempo aproximado en cada una = hasta la siguiente cabecera
    for i, v in enumerate(vistas):
        if i + 1 < len(vistas):
            a = datetime.strptime(v["hora"], "%H:%M:%S")
            b = datetime.strptime(vistas[i + 1]["hora"], "%H:%M:%S")
            v["seg"] = int((b - a).total_seconds()) % 86400
        else:
            v["seg"] = None
    return vistas


# ------------------------------ Vistas ------------------------------

@app.get("/")
def equipos():
    with pool.connection() as conn:
        _cerrar_abandonadas(conn)
        filas = conn.execute(
            """SELECT e.id, COALESCE(e.nombre, e.id) AS nombre,
                      e.primera_vez, e.ultima_vez,
                      EXTRACT(EPOCH FROM (now() - e.ultima_vez))::int AS inactivo,
                      count(s.id) AS n_capturas,
                      count(s.id) FILTER (WHERE s.estado = 'activa') AS activas,
                      (SELECT count(*) FROM lotes l JOIN sesiones s3 ON s3.id = l.sesion_id
                        WHERE s3.equipo_id = e.id) AS n_lotes
               FROM equipos e LEFT JOIN sesiones s ON s.equipo_id = e.id
               GROUP BY e.id ORDER BY e.ultima_vez DESC""").fetchall()
    return render_template("equipos.html", filas=filas, alerta=INACTIVO_ALERTA,
                           auto=True, pestana="equipos")


@app.get("/capturas")
def capturas():
    with pool.connection() as conn:
        _cerrar_abandonadas(conn)
        filas = conn.execute(
            """SELECT s.id, s.equipo_id, COALESCE(e.nombre, e.id) AS pc,
                      s.inicio, s.ultima_actividad, s.estado, s.ultimo_seq,
                      EXTRACT(EPOCH FROM (now() - s.ultima_actividad))::int AS inactivo
               FROM sesiones s JOIN equipos e ON e.id = s.equipo_id
               ORDER BY s.ultima_actividad DESC""").fetchall()
    return render_template("capturas.html", filas=filas, alerta=INACTIVO_ALERTA,
                           auto=True, pestana="capturas")


@app.get("/equipo/<eid>")
def equipo(eid):
    with pool.connection() as conn:
        _cerrar_abandonadas(conn)
        eq = conn.execute(
            """SELECT id, COALESCE(nombre, id) AS nombre, primera_vez, ultima_vez,
                      EXTRACT(EPOCH FROM (now() - ultima_vez))::int AS inactivo
               FROM equipos WHERE id = %s""", (eid,)).fetchone()
        if not eq:
            abort(404)
        sesiones = conn.execute(
            """SELECT s.id, s.inicio, s.ultima_actividad, s.estado,
                      s.ultimo_seq, count(l.seq) AS n_lotes,
                      COALESCE(sum(length(l.texto)), 0) AS caracteres,
                      EXTRACT(EPOCH FROM (s.ultima_actividad - s.inicio))::int AS duracion
               FROM sesiones s LEFT JOIN lotes l ON l.sesion_id = s.id
               WHERE s.equipo_id = %s
               GROUP BY s.id ORDER BY s.inicio DESC""", (eid,)).fetchall()
        lotes = conn.execute(
            """SELECT l.sesion_id, s.inicio AS sesion_inicio, l.seq, l.ts_cliente, l.recibido, l.texto
               FROM lotes l JOIN sesiones s ON s.id = l.sesion_id
               WHERE s.equipo_id = %s AND l.texto <> ''
               ORDER BY l.recibido DESC LIMIT 40""", (eid,)).fetchall()
    for l in lotes:
        l["marcas"] = marcas_de(l["texto"])
    return render_template("equipo.html", eq=eq, sesiones=sesiones, lotes=lotes,
                           auto=request.args.get("auto", "1") == "1",
                           pestana="equipos")


def _lotes_sesion(conn, sid):
    return conn.execute(
        """SELECT seq, ts_cliente, recibido, texto,
                  EXTRACT(EPOCH FROM (recibido - ts_cliente)) AS retraso
           FROM lotes WHERE sesion_id = %s ORDER BY seq""", (sid,)).fetchall()


@app.get("/sesion/<sid>")
def sesion(sid):
    with pool.connection() as conn:
        _cerrar_abandonadas(conn)
        meta = conn.execute(
            """SELECT s.id, s.equipo_id, COALESCE(e.nombre, e.id) AS pc,
                      s.inicio, s.ultima_actividad, s.estado, s.ultimo_seq,
                      EXTRACT(EPOCH FROM (s.ultima_actividad - s.inicio))::int AS duracion,
                      EXTRACT(EPOCH FROM (now() - s.ultima_actividad))::int AS inactivo
               FROM sesiones s JOIN equipos e ON e.id = s.equipo_id
               WHERE s.id = %s""", (sid,)).fetchone()
        if not meta:
            abort(404)
        lotes = _lotes_sesion(conn, sid)

    presentes = {l["seq"] for l in lotes}
    tope = max([meta["ultimo_seq"], *presentes]) if presentes else meta["ultimo_seq"]
    huecos = [i for i in range(1, tope + 1) if i not in presentes]
    # Sesiones antiguas: el lote vacío de "fin" no se guardaba; no es una pérdida.
    if meta["estado"] == "finalizada" and huecos and huecos[-1] == meta["ultimo_seq"]:
        huecos.pop()

    texto = "".join(l["texto"] for l in lotes)
    for l in lotes:
        l["marcas"] = marcas_de(l["texto"])
    retrasos = [l["retraso"] for l in lotes if l["retraso"] is not None]
    atajos = RE_ATAJO.findall(texto)

    stats = {
        "lotes": len(lotes),
        "caracteres": len(texto),
        "huecos": huecos,
        "pegados": atajos.count("V"),
        "copiados": atajos.count("C"),
        "borrados": texto.count("⌫"),
        "retraso_medio": sum(retrasos) / len(retrasos) if retrasos else None,
        "retraso_max": max(retrasos) if retrasos else None,
    }
    ventanas = ventanas_de(texto)
    apps = {}
    for v in ventanas:
        apps.setdefault(v["app"], [0, 0])
        apps[v["app"]][0] += 1
        apps[v["app"]][1] += v["seg"] or 0
    resumen_apps = sorted(apps.items(), key=lambda kv: -kv[1][1])

    vista = request.args.get("vista", "lotes")
    return render_template(
        "sesion.html", meta=meta, lotes=lotes, texto=texto, stats=stats,
        ventanas=ventanas, resumen_apps=resumen_apps, vista=vista,
        auto=request.args.get("auto") == "1", pestana="capturas")


@app.get("/api/sesion/<sid>/lotes")
def api_lotes(sid):
    with pool.connection() as conn:
        lotes = _lotes_sesion(conn, sid)
    return jsonify([
        {"seq": l["seq"],
         "ts_cliente": l["ts_cliente"].isoformat() if l["ts_cliente"] else None,
         "recibido": l["recibido"].isoformat(),
         "retraso_s": float(l["retraso"]) if l["retraso"] is not None else None,
         "texto": l["texto"]}
        for l in lotes])


# ---------------------------- Plantillas ----------------------------

PLANTILLAS = {
"base.html": """<!doctype html><html lang="es"><head><meta charset="utf-8">
{% if auto %}<meta http-equiv="refresh" content="5">{% endif %}
<title>{% block titulo %}Monitor de examen{% endblock %}</title>
<style>
 body{font-family:Segoe UI,system-ui,sans-serif;margin:1.5rem 2rem;background:#fafafa;color:#222}
 h1{font-size:1.3rem;margin:.6rem 0} h2{font-size:1.05rem;margin:1.6rem 0 .5rem}
 table{border-collapse:collapse;width:100%;background:#fff}
 th,td{border:1px solid #ddd;padding:.45rem .65rem;text-align:left;font-size:.88rem;vertical-align:top}
 th{background:#f0f0f0} a{color:#1565c0;text-decoration:none} a:hover{text-decoration:underline}
 nav{display:flex;gap:1rem;align-items:center;border-bottom:1px solid #ddd;padding-bottom:.6rem}
 nav a.on{font-weight:bold;color:#222} nav .der{margin-left:auto;font-size:.8rem;color:#888}
 .activa{color:#2e7d32;font-weight:bold}.finalizada{color:#777}.caida{color:#b71c1c;font-weight:bold}
 .alerta{background:#fff3cd}
 .muted{color:#888;font-size:.85rem} .num{text-align:right;font-variant-numeric:tabular-nums}
 pre{background:#fff;border:1px solid #ddd;padding:1rem;white-space:pre-wrap;word-break:break-word;
     font-size:.9rem;line-height:1.4;margin:0}
 td pre{border:0;padding:0;background:none;font-size:.85rem;max-height:14rem;overflow:auto}
 .tarjetas{display:flex;flex-wrap:wrap;gap:.6rem;margin:.8rem 0}
 .tarjeta{background:#fff;border:1px solid #ddd;padding:.5rem .8rem;min-width:7.5rem}
 .tarjeta b{display:block;font-size:1.25rem} .tarjeta span{font-size:.78rem;color:#777}
 .tarjeta.mal{border-color:#e0a800;background:#fff8e1}
 .chip{display:inline-block;font-size:.72rem;padding:.05rem .4rem;border-radius:.6rem;
       background:#eceff1;margin:0 .2rem .2rem 0;white-space:nowrap}
 .chip.fuerte{background:#ffcdd2;color:#b71c1c}.chip.aviso{background:#ffe0b2;color:#8d4a00}
 .tabs a{margin-right:1rem}.tabs a.on{font-weight:bold;color:#222}
 tr.hueco td{background:#ffebee;color:#b71c1c;font-style:italic}
</style></head><body>
<nav>
 <a href="/" class="{{ 'on' if pestana=='equipos' }}">Equipos</a>
 <a href="/capturas" class="{{ 'on' if pestana=='capturas' }}">Capturas</a>
 <span class="der">{% if auto %}refresco automático cada 5 s{% else %}sin refresco automático{% endif %}</span>
</nav>
{% block cuerpo %}{% endblock %}
</body></html>""",

"capturas.html": """{% extends "base.html" %}{% block cuerpo %}
<h1>Capturas de examen</h1>
<table><tr><th>PC</th><th>Captura</th><th>Inicio</th><th>Última actividad</th>
<th>Inactivo</th><th class="num">Lotes</th><th>Estado</th></tr>
{% for f in filas %}
<tr class="{{ 'alerta' if f.inactivo > alerta and f.estado=='activa' }}">
 <td><a href="/equipo/{{ f.equipo_id }}">{{ f.pc }}</a></td>
 <td><a href="/sesion/{{ f.id }}">{{ f.id[:8] }}</a></td>
 <td>{{ f.inicio|hora(true) }}</td><td>{{ f.ultima_actividad|hora }}</td>
 <td>{{ f.inactivo|dur }}</td><td class="num">{{ f.ultimo_seq }}</td>
 <td class="{{ f.estado }}">{{ f.estado }}</td>
</tr>
{% endfor %}</table>
{% if not filas %}<p>Aún no hay ninguna captura.</p>{% endif %}
<p class="muted">Estados: <b class="activa">activa</b> en curso ·
 <b class="finalizada">finalizada</b> detenida por el alumno ·
 <b class="caida">caida</b> sin señal demasiado tiempo (cierre brusco, apagón o red).</p>
{% endblock %}""",

"equipos.html": """{% extends "base.html" %}{% block cuerpo %}
<h1>Equipos</h1>
<table><tr><th>PC</th><th>Visto por 1ª vez</th><th>Última señal</th>
<th>Sin señal</th><th class="num">Capturas</th><th class="num">Activas</th><th class="num">Lotes</th></tr>
{% for f in filas %}
<tr class="{{ 'alerta' if f.inactivo > alerta and f.activas }}">
 <td><a href="/equipo/{{ f.id }}">{{ f.nombre }}</a><br><span class="muted">{{ f.id[:8] }}…</span></td>
 <td>{{ f.primera_vez|hora(true) }}</td><td>{{ f.ultima_vez|hora }}</td>
 <td>{{ f.inactivo|dur }}</td><td class="num">{{ f.n_capturas }}</td>
 <td class="num {{ 'activa' if f.activas }}">{{ f.activas }}</td><td class="num">{{ f.n_lotes }}</td>
</tr>
{% endfor %}</table>
{% if not filas %}<p>Aún no se ha conectado ningún equipo.</p>{% endif %}
<p class="muted">Cada equipo es un PC. Entra en uno para ver sus capturas (cada arranque–parada
 del programa, con su propia numeración de lotes) y su texto en vivo.</p>
{% endblock %}""",

"equipo.html": """{% extends "base.html" %}
{% block titulo %}{{ eq.nombre }}{% endblock %}{% block cuerpo %}
<h1>{{ eq.nombre }}</h1>
<p class="muted">ID {{ eq.id }} · visto por 1ª vez {{ eq.primera_vez|hora(true) }}
 · última señal {{ eq.ultima_vez|hora }} (hace {{ eq.inactivo|dur }})
 · {% if auto %}<a href="?auto=0">pausar refresco</a>{% else %}<a href="?auto=1">reanudar refresco</a>{% endif %}</p>

<h2>Capturas en este PC</h2>
<table><tr><th>Captura</th><th>Inicio</th><th>Última actividad</th><th>Duración</th>
<th class="num">Lotes</th><th class="num">Caracteres</th><th>Estado</th></tr>
{% for s in sesiones %}
<tr><td><a href="/sesion/{{ s.id }}">{{ s.id[:8] }}</a></td>
 <td>{{ s.inicio|hora(true) }}</td><td>{{ s.ultima_actividad|hora }}</td>
 <td>{{ s.duracion|dur }}</td><td class="num">{{ s.n_lotes }}</td>
 <td class="num">{{ s.caracteres }}</td><td class="{{ s.estado }}">{{ s.estado }}</td></tr>
{% endfor %}</table>

<h2>Últimos lotes recibidos <span class="muted">(los 40 más recientes, de cualquier captura)</span></h2>
<table><tr><th>Recibido</th><th>Captura</th><th class="num">Seq</th><th>Marcas</th><th>Contenido</th></tr>
{% for l in lotes %}
<tr><td>{{ l.recibido|hora }}</td>
 <td><a href="/sesion/{{ l.sesion_id }}">{{ l.sesion_id[:8] }}</a>
   <br><span class="muted">desde {{ l.sesion_inicio|hora }}</span></td>
 <td class="num"><a href="/sesion/{{ l.sesion_id }}#lote-{{ l.seq }}">{{ l.seq }}</a></td>
 <td>{% for t, c in l.marcas %}<span class="chip {{ c }}">{{ t }}</span>{% endfor %}</td>
 <td><pre>{{ l.texto }}</pre></td></tr>
{% endfor %}</table>
{% if not lotes %}<p>Este PC todavía no ha mandado texto.</p>{% endif %}
{% endblock %}""",

"sesion.html": """{% extends "base.html" %}
{% block titulo %}{{ meta.pc }} · {{ meta.inicio|hora }}{% endblock %}{% block cuerpo %}
<h1><a href="/equipo/{{ meta.equipo_id }}">{{ meta.pc }}</a></h1>
<p class="muted">captura {{ meta.id }}
 · inicio {{ meta.inicio|hora(true) }} · última actividad {{ meta.ultima_actividad|hora }}
 · estado <b class="{{ meta.estado }}">{{ meta.estado }}</b>
 · <a href="/api/sesion/{{ meta.id }}/lotes">JSON</a>
 · {% if auto %}<a href="?vista={{ vista }}">pausar refresco</a>{% else %}<a href="?vista={{ vista }}&auto=1">refresco automático</a>{% endif %}</p>

<div class="tarjetas">
 <div class="tarjeta"><b>{{ meta.duracion|dur }}</b><span>duración</span></div>
 <div class="tarjeta"><b>{{ stats.lotes }}</b><span>lotes</span></div>
 <div class="tarjeta"><b>{{ stats.caracteres }}</b><span>caracteres</span></div>
 <div class="tarjeta {{ 'mal' if stats.huecos }}"><b>{{ stats.huecos|length }}</b>
   <span>lotes perdidos{% if stats.huecos %}: {{ stats.huecos|join(', ') }}{% endif %}</span></div>
 <div class="tarjeta {{ 'mal' if stats.pegados }}"><b>{{ stats.pegados }}</b><span>pegados (Ctrl+V)</span></div>
 <div class="tarjeta"><b>{{ stats.copiados }}</b><span>copiados (Ctrl+C)</span></div>
 <div class="tarjeta {{ 'mal' if ventanas|length > 1 }}"><b>{{ ventanas|length }}</b><span>cambios de ventana</span></div>
 <div class="tarjeta"><b>{{ stats.borrados }}</b><span>borrados</span></div>
 <div class="tarjeta"><b>{{ '%.1f s'|format(stats.retraso_medio) if stats.retraso_medio is not none else '—' }}</b>
   <span>retraso medio (máx {{ '%.1f s'|format(stats.retraso_max) if stats.retraso_max is not none else '—' }})</span></div>
</div>

<div class="tabs">
 <a href="?vista=lotes{{ '&auto=1' if auto }}" class="{{ 'on' if vista=='lotes' }}">Lotes</a>
 <a href="?vista=ventanas{{ '&auto=1' if auto }}" class="{{ 'on' if vista=='ventanas' }}">Ventanas</a>
 <a href="?vista=texto{{ '&auto=1' if auto }}" class="{{ 'on' if vista=='texto' }}">Texto completo</a>
</div>

{% if vista == 'ventanas' %}
<h2>Tiempo por aplicación <span class="muted">(aproximado, según los cambios de ventana)</span></h2>
<table><tr><th>Aplicación</th><th class="num">Veces</th><th class="num">Tiempo</th></tr>
{% for app_, d in resumen_apps %}
<tr><td>{{ app_ }}</td><td class="num">{{ d[0] }}</td><td class="num">{{ d[1]|dur }}</td></tr>
{% endfor %}</table>
<h2>Recorrido</h2>
<table><tr><th>Hora</th><th>Aplicación</th><th>Título de la ventana</th><th class="num">Tiempo</th></tr>
{% for v in ventanas %}
<tr><td>{{ v.hora }}</td><td>{{ v.app }}</td><td>{{ v.titulo }}</td>
 <td class="num">{{ v.seg|dur if v.seg is not none else 'en curso' }}</td></tr>
{% endfor %}</table>
{% if not ventanas %}<p>Sin datos de ventanas todavía.</p>{% endif %}

{% elif vista == 'texto' %}
<h2>Texto reconstruido</h2>
<pre>{{ texto }}</pre>

{% else %}
<h2>Lotes</h2>
<table><tr><th class="num">Seq</th><th>Escrito (PC)</th><th>Recibido</th><th class="num">Retraso</th>
<th class="num">Car.</th><th>Marcas</th><th>Contenido</th></tr>
{% set ns = namespace(prev=0) %}
{% for l in lotes %}
 {% for h in range(ns.prev + 1, l.seq) %}
 <tr class="hueco"><td class="num">{{ h }}</td><td colspan="6">lote perdido: nunca llegó al servidor</td></tr>
 {% endfor %}
 {% set ns.prev = l.seq %}
<tr id="lote-{{ l.seq }}"><td class="num">{{ l.seq }}</td>
 <td>{{ l.ts_cliente|hora }}</td><td>{{ l.recibido|hora }}</td>
 <td class="num">{{ '%.1f s'|format(l.retraso) if l.retraso is not none else '—' }}</td>
 <td class="num">{{ l.texto|length }}</td>
 <td>{% for t, c in l.marcas %}<span class="chip {{ c }}">{{ t }}</span>{% endfor %}</td>
 <td>{% if l.texto %}<pre>{{ l.texto }}</pre>{% else %}<span class="muted">fin de la captura</span>{% endif %}</td></tr>
{% endfor %}</table>
{% if not lotes %}<p>Aún no ha llegado ningún lote.</p>{% endif %}
{% endif %}
{% endblock %}""",
}
app.jinja_loader = DictLoader(PLANTILLAS)


if __name__ == "__main__":
    # Solo para pruebas locales sin gunicorn.
    app.run(host="127.0.0.1", port=8000)
