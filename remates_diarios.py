#!/usr/bin/env python3
"""
Vigilante de remates judiciales (Publicaciones Procesales, Rama Judicial).

- Revisa la categoría "Remates" (GET sin login, 10 avisos por página).
- Avisa por Telegram (uno o varios chats) solo de los avisos NUEVOS que cumplen tus filtros.
- Mide su propia puntualidad: guarda cada revisión y cuánto tardó en detectar cada aviso.

Uso local:
    pip install -r requirements.txt
    python remates_diarios.py --deptos ANTIOQUIA --municipios RIONEGRO     # una revisión
    python remates_diarios.py --vigilar --intervalo 5                      # bucle continuo (VPS)
    python remates_diarios.py --probar-telegram                            # mensaje de prueba
    python remates_diarios.py --resumen --horas 168                        # estadísticas de retraso

Variables de entorno (en GitHub van en "Secrets"):
    TELEGRAM_BOT_TOKEN   token del bot (@BotFather)
    TELEGRAM_CHAT_IDS    ids de chat separados por coma (varias personas o un grupo)
    REMATES_DEPTOS       ej: ANTIOQUIA,CUNDINAMARCA,BOLIVAR>CARTAGENA   (opcional, filtra;
                         "DEPTO>MUNICIPIO" limita ese departamento a un municipio)
    REMATES_MUNICIPIOS   ej: RIONEGRO,MEDELLÍN        (opcional, filtra)
Si no defines filtros, recibirás TODOS los remates del país.
"""
import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://publicacionesprocesales.ramajudicial.gov.co"
PATH = "/web/publicaciones-procesales/inicio"
INSTANCE = "co_com_avanti_efectosProcesales_PublicacionesEfectosProcesalesPortletV2_INSTANCE_BIyXQFHVaYaq"
NS = f"_{INSTANCE}_"
ID_REMATES = "6098997"  # idStructure de la categoría "Remates"
HEADERS = {"User-Agent": "Mozilla/5.0 (revision de remates; uso personal)"}
TZ = ZoneInfo("America/Bogota")
MAX_PAGINAS = 5

REV_HDR = ["utc", "brecha_min", "estado", "nuevos_total"]
MED_HDR = ["detectado_utc", "revision_anterior_utc", "brecha_max_min", "fecha_publicacion",
           "departamento", "municipio", "titulo", "id"]


# ------------------------------------------------------------------ portal
def pedir_pagina(pagina: int) -> str:
    params = {
        "p_p_id": INSTANCE, "p_p_lifecycle": "0", "p_p_state": "normal", "p_p_mode": "view",
        NS + "idStructure": ID_REMATES, NS + "action": "filterStructures",
        NS + "idDepto": " ", NS + "verTotales": "true", NS + "cur": str(pagina),
    }
    r = requests.get(BASE + PATH, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.text


def article_id(href: str) -> str:
    try:
        q = parse_qs(urlparse(href).query)
    except Exception:
        return ""
    for k, v in q.items():
        if k.endswith("articleId") and v:
            return v[0]
    return ""


def parsear(html: str) -> list[dict]:
    import re
    soup = BeautifulSoup(html, "html.parser")
    avisos = []
    for card in soup.select("div.efecto"):
        texto = " ".join(card.get_text(" ", strip=True).split())
        titulo_a = card.find("a")
        detalle = card.find("a", string=re.compile("detalle", re.I))
        href = (titulo_a or detalle or {}).get("href", "")
        if href.startswith("/"):
            href = BASE + href

        def campo(nombre, siguiente):
            m = re.search(rf"{nombre}:\s*(.*?)\s*(?:{siguiente}:|Fecha de Publicaci|$)", texto)
            return m.group(1).strip() if m else ""

        fecha = re.search(r"Fecha de Publicaci[oó]n:\s*(\d{4}-\d{2}-\d{2})", texto)
        avisos.append({
            "id": article_id(href),
            "titulo": titulo_a.get_text(strip=True) if titulo_a else "",
            "departamento": campo("Departamento", "Municipio"),
            "municipio": campo("Municipio", "Entidad"),
            "entidad": campo("Entidad", "Especialidad"),
            "despacho": campo("Despacho", "Fecha"),
            "fecha": fecha.group(1) if fecha else "",
            "url": href,
        })
    return avisos


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.upper())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def cumple(a: dict, deptos: list[str], municipios: list[str]) -> bool:
    """Cada entrada de `deptos` puede ser un departamento completo ("ANTIOQUIA")
    o un departamento + municipio ("BOLIVAR>CARTAGENA"). Basta que cumpla una.
    Si además hay `municipios`, el aviso también debe estar en alguno de ellos."""
    if deptos:
        ok = False
        for entrada in deptos:
            dep, _, mun = entrada.partition(">")
            if norm(dep.strip()) not in norm(a["departamento"]):
                continue
            if mun.strip() and norm(mun.strip()) not in norm(a["municipio"]):
                continue
            ok = True
            break
        if not ok:
            return False
    if municipios and not any(norm(m) in norm(a["municipio"]) for m in municipios):
        return False
    return True


def clave(a: dict) -> str:
    return a["id"] or f'{a["titulo"]}|{a["despacho"]}|{a["fecha"]}'


def buscar_nuevos(vistos: set, desde: str) -> list[dict]:
    """Devuelve TODOS los avisos nuevos (sin filtrar) y los agrega a `vistos`.
    Pasa a la página siguiente solo si toda la página actual era nueva."""
    nuevos = []
    for pagina in range(1, MAX_PAGINAS + 1):
        avisos = parsear(pedir_pagina(pagina))
        if not avisos:
            break
        hay_conocido = False
        for a in avisos:
            if a["fecha"] and a["fecha"] < desde:
                hay_conocido = True
                continue
            k = clave(a)
            if k in vistos:
                hay_conocido = True
                continue
            vistos.add(k)
            nuevos.append(a)
        if hay_conocido:
            break
        time.sleep(1.5)
    return nuevos


# ---------------------------------------------------------------- telegram
def chat_ids() -> list[str]:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]


def telegram_configurado() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(chat_ids())


def partir(texto: str, limite: int = 3800) -> list[str]:
    trozos, actual = [], ""
    for bloque in texto.split("\n\n"):
        if actual and len(actual) + len(bloque) + 2 > limite:
            trozos.append(actual)
            actual = bloque
        else:
            actual = f"{actual}\n\n{bloque}" if actual else bloque
    if actual:
        trozos.append(actual)
    return trozos


def enviar_telegram(texto: str) -> bool:
    """Envía a todos los chats. True si al menos uno lo recibió.
    Nunca imprime el token ni la URL de la API (los logs de GitHub son públicos)."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    ok_alguno = False
    for chat in chat_ids():
        ok_chat = True
        for trozo in partir(texto):
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": trozo, "disable_web_page_preview": True},
                    timeout=30,
                )
                if not r.ok:
                    ok_chat = False
                    try:
                        desc = r.json().get("description", "")
                    except Exception:
                        desc = ""
                    print(f"Telegram: fallo al enviar a un chat (HTTP {r.status_code}) {desc}", flush=True)
                    break
            except Exception as e:
                ok_chat = False
                print(f"Telegram: error de red ({type(e).__name__})", flush=True)
                break
        ok_alguno = ok_alguno or ok_chat
    return ok_alguno


def emitir(texto: str) -> bool:
    """Telegram si está configurado; si no, consola (uso local)."""
    if telegram_configurado():
        return enviar_telegram(texto)
    print(texto, flush=True)
    return True


# ------------------------------------------------------------------ medición
def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def hora_col(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


def leer_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def anexar_csv(p: Path, hdr: list[str], filas: list[list]):
    nuevo = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(hdr)
        w.writerows(filas)


def formatear_alerta(avisos, ahora, previa_ok, brecha_det) -> str:
    bloques = [f'• {a["titulo"]}\n  {a["fecha"]} | {a["departamento"]} - {a["municipio"]}\n'
               f'  {a["despacho"]}\n  {a["url"]}' for a in avisos]
    pie = f"⏱ Detectado a las {hora_col(ahora)} (hora Colombia)."
    if previa_ok is not None:
        pie += (f" Revisión anterior: {hora_col(previa_ok)}; el aviso apareció en ese intervalo "
                f"(retraso máx. ≈ {brecha_det:.0f} min).")
    return f"🔔 {len(avisos)} remate(s) nuevo(s)\n\n" + "\n\n".join(bloques) + "\n\n" + pie


def alertar_si_racha_errores(f_rev: Path):
    estados = [r["estado"] for r in leer_csv(f_rev)[-4:]]
    if len(estados) >= 3 and all(e != "ok" for e in estados[-3:]) and (len(estados) < 4 or estados[-4] == "ok"):
        emitir("⚠️ El vigilante de remates lleva 3 revisiones seguidas con fallos "
               "(portal caído, bloqueo o error de Telegram). Revisa la pestaña Actions.")


# ------------------------------------------------------------- una revisión
def filtros(args):
    def lista(valor):
        return [x.strip() for x in valor.split(",") if x.strip()]
    deptos = args.deptos or lista(os.environ.get("REMATES_DEPTOS", ""))
    municipios = args.municipios or lista(os.environ.get("REMATES_MUNICIPIOS", ""))
    return deptos, municipios


def ejecutar_revision(args, data_dir: Path) -> str:
    """Una pasada completa. Devuelve 'ok', 'error' o 'telegram_error'."""
    data_dir.mkdir(parents=True, exist_ok=True)
    f_vistos, f_rev, f_med = data_dir / "vistos.json", data_dir / "revisiones.csv", data_dir / "mediciones.csv"
    ahora = ahora_utc()

    revs = leer_csv(f_rev)
    previa_any = datetime.fromisoformat(revs[-1]["utc"]) if revs else None
    ok_prev = [r for r in revs if r["estado"] == "ok"]
    previa_ok = datetime.fromisoformat(ok_prev[-1]["utc"]) if ok_prev else None
    brecha_rev = round((ahora - previa_any).total_seconds() / 60, 1) if previa_any else ""
    brecha_det = (ahora - previa_ok).total_seconds() / 60 if previa_ok else None

    primera_vez = not f_vistos.exists()
    vistos = set(json.loads(f_vistos.read_text())) if f_vistos.exists() else set()
    deptos, municipios = filtros(args)
    desde = (datetime.now(TZ).date() - timedelta(days=args.dias)).isoformat()

    try:
        vistos_nuevo = set(vistos)
        nuevos = buscar_nuevos(vistos_nuevo, desde)
    except Exception as e:
        print(f"Error leyendo el portal: {type(e).__name__}: {e}", flush=True)
        anexar_csv(f_rev, REV_HDR, [[iso(ahora), brecha_rev, "error", 0]])
        alertar_si_racha_errores(f_rev)
        return "error"

    estado = "ok"
    if primera_vez:
        resumen_f = " | ".join(x for x in (", ".join(deptos), ", ".join(municipios)) if x) or "todo el país"
        emitir(f"✅ Vigilante de remates activo. Filtro: {resumen_f}. "
               f"Línea base de {len(vistos_nuevo)} avisos recientes; desde ahora te aviso solo de los nuevos.")
        nuevos_registrables = []
    else:
        destacados = [a for a in nuevos if cumple(a, deptos, municipios)]
        if destacados and not emitir(formatear_alerta(destacados, ahora, previa_ok, brecha_det or 0)):
            estado = "telegram_error"  # no se marcan como vistos: se reintenta en la próxima pasada
        nuevos_registrables = nuevos

    if estado == "ok":
        f_vistos.write_text(json.dumps(sorted(vistos_nuevo), ensure_ascii=False))
        if nuevos_registrables:
            anexar_csv(f_med, MED_HDR, [[
                iso(ahora), iso(previa_ok) if previa_ok else "",
                round(brecha_det, 1) if brecha_det is not None else "",
                a["fecha"], a["departamento"], a["municipio"], a["titulo"], a["id"],
            ] for a in nuevos_registrables])

    anexar_csv(f_rev, REV_HDR, [[iso(ahora), brecha_rev, estado, 0 if primera_vez else len(nuevos)]])
    if estado != "ok":
        alertar_si_racha_errores(f_rev)
    print(f"[{hora_col(ahora)} Col] revisión {estado}: {0 if primera_vez else len(nuevos)} aviso(s) nuevo(s) en el país", flush=True)
    return estado


# ------------------------------------------------------------------ resumen
def pct(valores, p):
    s = sorted(valores)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def resumen(data_dir: Path, horas: float) -> str:
    corte = ahora_utc() - timedelta(hours=horas)
    revs = [r for r in leer_csv(data_dir / "revisiones.csv") if datetime.fromisoformat(r["utc"]) >= corte]
    meds = [r for r in leer_csv(data_dir / "mediciones.csv") if datetime.fromisoformat(r["detectado_utc"]) >= corte]
    out = [f"Resumen de las últimas {horas:g} horas", ""]
    brechas = [float(r["brecha_min"]) for r in revs if r["brecha_min"] != ""]
    if brechas:
        n = len(brechas)
        sobre = lambda x: 100 * sum(1 for b in brechas if b > x) / n
        errores = sum(1 for r in revs if r["estado"] != "ok")
        out += [
            f"Revisiones: {len(revs)} (con fallo: {errores})",
            f"Tiempo entre revisiones: mediana {statistics.median(brechas):.0f} min, "
            f"p95 {pct(brechas, 95):.0f} min, máximo {max(brechas):.0f} min",
            f"Brechas mayores a 15 min: {sobre(15):.0f}% | a 30 min: {sobre(30):.0f}% | a 60 min: {sobre(60):.0f}%",
        ]
    else:
        out.append("Aún no hay suficientes revisiones.")
    det = [float(r["brecha_max_min"]) for r in meds if r["brecha_max_min"] != ""]
    out.append("")
    if det:
        out += [f"Avisos nuevos detectados: {len(meds)}",
                f"Retraso máximo de detección por aviso: mediana {statistics.median(det):.0f} min, "
                f"p95 {pct(det, 95):.0f} min, peor caso {max(det):.0f} min"]
    else:
        out.append("Aún no hay avisos nuevos registrados en este periodo.")
    out += ["", "Lectura sugerida: si el p95 del tiempo entre revisiones supera ~30 min, o más del 10% de las "
                "brechas pasa de 30 min, conviene pasar al servidor (VPS)."]
    return "\n".join(out)


# ------------------------------------------------------------ modo continuo
def vigilar(args, data_dir: Path):
    h_ini, h_fin = (int(x) for x in args.horario.split("-"))
    fallos = 0
    print(f"Vigilancia continua: cada ~{args.intervalo:g} min, de {h_ini}:00 a {h_fin}:00 (hora Colombia).", flush=True)
    while True:
        if not (h_ini <= datetime.now(TZ).hour < h_fin):
            time.sleep(600)
            continue
        estado = ejecutar_revision(args, data_dir)
        if estado == "ok":
            fallos = 0
            espera = args.intervalo * 60 + random.uniform(-args.variacion, args.variacion) * 60
        else:
            fallos += 1
            espera = min(5 * 60 * 2 ** fallos, 2 * 3600)
            print(f"Reintento en {espera / 60:.0f} min (fallos seguidos: {fallos})", flush=True)
        time.sleep(max(60, espera))


def main():
    ap = argparse.ArgumentParser(description="Vigilante de remates judiciales")
    ap.add_argument("--dias", type=int, default=3, help="ventana de días hacia atrás (default 3)")
    ap.add_argument("--deptos", nargs="*", default=[], help="ej: ANTIOQUIA")
    ap.add_argument("--municipios", nargs="*", default=[], help="ej: RIONEGRO")
    ap.add_argument("--datos", default=os.environ.get("REMATES_DATOS", str(Path(__file__).with_name("datos"))),
                    help="carpeta donde se guarda el estado y las mediciones")
    ap.add_argument("--vigilar", action="store_true", help="bucle continuo (para un servidor)")
    ap.add_argument("--intervalo", type=float, default=5, help="minutos entre revisiones en --vigilar")
    ap.add_argument("--variacion", type=float, default=1, help="± minutos al azar en --vigilar")
    ap.add_argument("--horario", default="0-24", help="horas activas en --vigilar (hora Colombia)")
    ap.add_argument("--resumen", action="store_true", help="estadísticas de retraso")
    ap.add_argument("--horas", type=float, default=168, help="ventana del resumen (default 168 = 7 días)")
    ap.add_argument("--probar-telegram", action="store_true", help="envía un mensaje de prueba")
    args = ap.parse_args()
    data_dir = Path(args.datos)

    if args.probar_telegram:
        if not telegram_configurado():
            sys.exit("Faltan TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_IDS.")
        print("Enviado." if enviar_telegram("✅ Prueba del vigilante de remates: Telegram funciona.")
              else "No se pudo enviar a ningún chat.")
        return
    if args.resumen:
        texto = resumen(data_dir, args.horas)
        print(texto)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write("```\n" + texto + "\n```\n")
        return
    if args.vigilar:
        vigilar(args, data_dir)
        return
    ejecutar_revision(args, data_dir)


if __name__ == "__main__":
    main()
