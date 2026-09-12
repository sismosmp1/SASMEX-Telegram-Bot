import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Thread, Lock

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

SASMEX_URL = os.getenv("SASMEX_URL", "https://rss.sasmex.net/")
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.getenv("CHAT_ID", "")
CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "15"))
STATUS_SECONDS = int(os.getenv("STATUS_SECONDS", "5"))
STATE_FILE = os.getenv("STATE_FILE", "/tmp/sasmex_state.json")
LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Mexico_City"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sasmex-bot")

app = Flask(__name__)
session = requests.Session()
session.headers.update({
    "User-Agent": "SASMEX-Telegram-Bot/1.0 (+automatic monitoring)"
})
state_lock = Lock()
status_message_id = None
last_check = None
last_event_id = None
connected = False

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_id": None}

def save_state(last_id):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_id": last_id}, f)
    except Exception as e:
        log.warning("No se pudo guardar estado: %s", e)

def clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()

def get_latest_from_homepage():
    r = session.get(SASMEX_URL, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    candidates = []
    # Prefer links to CAP files. SASMEX pages commonly expose filenames like 20260903053425.cap.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        full = requests.compat.urljoin(SASMEX_URL, href)
        m = re.search(r"(\d{14})\.cap(?:$|[?#])", href, re.I)
        if m:
            candidates.append({
                "id": m.group(1),
                "cap_url": full,
                "text": clean(a.get_text(" ", strip=True)),
            })

    if candidates:
        # Newest CAP timestamp wins.
        candidates.sort(key=lambda x: x["id"], reverse=True)
        latest = candidates[0]
        # Get nearby row/card text for human-readable fields.
        anchor = next((a for a in soup.find_all("a", href=True)
                       if re.search(re.escape(latest["id"]) + r"\.cap", a.get("href",""), re.I)), None)
        context = ""
        if anchor:
            node = anchor
            for _ in range(4):
                if getattr(node, "parent", None):
                    node = node.parent
                    txt = clean(node.get_text(" ", strip=True))
                    if len(txt) > len(context) and len(txt) < 1000:
                        context = txt
        latest["context"] = context
        return latest

    # Fallback: find a 14-digit CAP-like timestamp anywhere in the page.
    text = soup.get_text(" ", strip=True)
    ids = re.findall(r"\b(20\d{12})\b", text)
    if ids:
        latest_id = max(ids)
        return {"id": latest_id, "cap_url": None, "text": "", "context": text[:1200]}

    raise RuntimeError("No encontré un archivo CAP en la página de SASMEX.")

def parse_cap(cap_url):
    if not cap_url:
        return {}
    try:
        r = session.get(cap_url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "xml")
        data = {}
        # CAP standard fields; tolerate namespaces.
        for tag in ["event", "severity", "urgency", "certainty", "headline", "description",
                    "areaDesc", "geocode", "effective", "onset", "expires", "sent"]:
            el = soup.find(tag)
            if el and el.get_text(strip=True):
                data[tag] = clean(el.get_text(" ", strip=True))
        # Look for common magnitude representations.
        raw = soup.get_text(" ", strip=True)
        mag = re.search(r"(?:magnitud|magnitude)\s*[:=]?\s*M?\s*([0-9]+(?:[.,][0-9]+)?)", raw, re.I)
        if not mag:
            mag = re.search(r"\bM\s*([0-9](?:[.,][0-9])?)\b", raw, re.I)
        if mag:
            data["magnitude"] = mag.group(1).replace(",", ".")
        return data
    except Exception as e:
        log.warning("No se pudo leer CAP %s: %s", cap_url, e)
        return {}

def format_date(cap_id):
    try:
        dt = datetime.strptime(cap_id, "%Y%m%d%H%M%S")
        return dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M:%S")
    except Exception:
        return None, None

def format_message(item, cap):
    date, hour = format_date(item["id"])
    context = item.get("context", "")
    # Try to extract common fields from the visible row/card.
    state = ""
    region = ""
    kind = ""
    m = re.search(r"\bEstado\s*[:\-]\s*([^|]+?)(?=\s+(?:Región|Tipo)\s*[:\-]|$)", context, re.I)
    if m: state = clean(m.group(1))
    m = re.search(r"\bRegi[oó]n\s*[:\-]\s*([^|]+?)(?=\s+(?:Estado|Tipo)\s*[:\-]|$)", context, re.I)
    if m: region = clean(m.group(1))
    m = re.search(r"\bTipo\s*[:\-]\s*([^|]+?)(?=\s+(?:Estado|Regi[oó]n)\s*[:\-]|$)", context, re.I)
    if m: kind = clean(m.group(1))

    headline = cap.get("headline") or cap.get("event") or ""
    area = cap.get("areaDesc") or ""
    severity_raw = cap.get("severity") or ""
    magnitude = cap.get("magnitude") or ""

    # SASMEX usa dos severidades. Adaptamos el texto al diseño de Sismos MP.
    sev = clean(severity_raw).lower()
    if sev in ("menor", "minor"):
        alert_title = "⚠️ <b>Sismo en Desarrollo</b> ⚠️"
        intensity = "MODERADO"
    elif sev in ("mayor", "major"):
        alert_title = "🚨 <b>ALERTA SÍSMICA</b> 🚨"
        intensity = "VIOLENTO"
    else:
        alert_title = "⚠️ <b>Sismo en Desarrollo</b> ⚠️"
        intensity = "NO DETERMINADA"

    # Prefer CAP area description; otherwise use region/headline.
    location = area or region or clean(re.sub(r"(?i)sismo\s*(en)?", "", headline)).strip()

    lines = ["#SismoDetectado #SismosMP", "", alert_title, ""]
    if location:
        lines.append(f"Iniciando en <b>{location}</b>")
    if date: lines.append(f"Fecha: {date}")
    if hour: lines.append(f"Hora: {hour}")
    lines.append(f"Intensidad: <b>{intensity}</b>")
    if magnitude: lines.append(f"Magnitud: <b>M {magnitude}</b>")
    if state: lines.append(f"Estado: {state}")
    lines += ["", "📡 Fuente: SASMEX"]
    if item.get("cap_url"):
        lines.append(f'<a href="{item["cap_url"]}">Ver CAP</a>')
    return "\n".join(lines)

def telegram_api(method, data=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = session.post(url, data=data or {}, timeout=20)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload

def telegram_send(text):
    if not CHAT_ID:
        raise RuntimeError("Falta CHAT_ID.")
    return telegram_api("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })

def telegram_edit_status(text):
    global status_message_id
    if not CHAT_ID:
        raise RuntimeError("Falta CHAT_ID.")
    if status_message_id is None:
        result = telegram_api("sendMessage", {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        status_message_id = result["result"]["message_id"]
        # Try to pin it; this requires the bot to have pin permission. Failure is harmless.
        try:
            telegram_api("pinChatMessage", {
                "chat_id": CHAT_ID,
                "message_id": status_message_id,
                "disable_notification": True,
            })
        except Exception as e:
            log.info("No se pudo fijar el mensaje de estado (se puede hacer manualmente): %s", e)
    else:
        try:
            telegram_api("editMessageText", {
                "chat_id": CHAT_ID,
                "message_id": status_message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except requests.HTTPError as e:
            # If the message was deleted, recreate it on next cycle.
            if getattr(e.response, "status_code", None) == 400:
                status_message_id = None
            else:
                raise

def status_text():
    now = datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M:%S")
    if connected:
        state = "🟢 <b>CONECTADO</b>"
    else:
        state = "🔴 <b>SIN CONEXIÓN</b>"
    check = last_check.astimezone(LOCAL_TZ).strftime("%H:%M:%S") if last_check else "--:--:--"
    event = last_event_id or "Aún sin evento"
    return (
        "🟢 <b>MONITOREANDO SISMOS</b>\n\n"
        f"📡 SASMEX: {state}\n"
        f"🕐 Hora actual: <b>{now}</b>\n"
        f"🔄 Última revisión: <b>{check}</b>\n"
        f"⚡ Monitoreo: cada <b>{CHECK_SECONDS} segundos</b>\n"
        f"🚨 Último CAP: <code>{event}</code>"
    )

def status_loop():
    global connected
    while True:
        try:
            telegram_edit_status(status_text())
        except Exception:
            log.exception("No se pudo actualizar el mensaje de estado")
        time.sleep(STATUS_SECONDS)

def monitor():
    global last_check, last_event_id, connected
    log.info("Monitor iniciado: cada %ss; estado cada %ss", CHECK_SECONDS, STATUS_SECONDS)
    state = load_state()
    initialized = state.get("last_id") is not None

    while True:
        try:
            item = get_latest_from_homepage()
            current_id = item["id"]
            last_check = datetime.now().astimezone()
            last_event_id = current_id
            connected = True

            if not initialized:
                save_state(current_id)
                state["last_id"] = current_id
                initialized = True
                log.info("Inicializado con CAP %s (no se publica el histórico).", current_id)
            elif current_id != state.get("last_id"):
                cap = parse_cap(item.get("cap_url"))
                msg = format_message(item, cap)
                telegram_send(msg)
                save_state(current_id)
                state["last_id"] = current_id
                log.info("Publicado CAP %s", current_id)
        except Exception:
            connected = False
            log.exception("Error durante la comprobación")
        time.sleep(CHECK_SECONDS)

@app.get("/")
def root():
    return "SASMEX Telegram bot activo.", 200

@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "sasmex-telegram-bot"})

def start_monitor():
    Thread(target=monitor, daemon=True).start()
    Thread(target=status_loop, daemon=True).start()

if __name__ == "__main__":
    start_monitor()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
