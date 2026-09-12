import os
import re
import time
import json
import logging
from datetime import datetime
from threading import Thread, Lock
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

SASMEX_URL = os.getenv("SASMEX_URL", "https://rss.sasmex.net/")
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.getenv("CHAT_ID", "")
CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "15"))
STATUS_SECONDS = int(os.getenv("STATUS_SECONDS", "5"))
TIMEZONE = os.getenv("TIMEZONE", "America/Mexico_City")
STATE_FILE = os.getenv("STATE_FILE", "/tmp/sasmex_state.json")

try:
    TZ = ZoneInfo(TIMEZONE)
except Exception:
    TZ = ZoneInfo("America/Mexico_City")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sasmex-bot")

app = Flask(__name__)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; SASMEX-Telegram-Bot/2.0)"
})
state_lock = Lock()
status_message_id = None
last_check = None
last_event_id = None
connected = False


def now_local():
    return datetime.now(TZ)


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


def row_to_item(row):
    cells = [clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
    if not cells:
        return None
    joined = " | ".join(cells)
    m = re.search(r"\b(20\d{12})\.cap\b", joined, re.I)
    if not m:
        return None
    cap_id = m.group(1)
    cap_url = None
    for a in row.find_all("a", href=True):
        href = a.get("href", "")
        if re.search(re.escape(cap_id) + r"\.cap", href, re.I):
            cap_url = urljoin(SASMEX_URL, href)
            break
    if not cap_url:
        cap_url = urljoin(SASMEX_URL, f"{cap_id}.cap")

    item = {
        "id": cap_id,
        "cap_url": cap_url,
        "context": joined,
        "state": cells[1] if len(cells) >= 2 else "",
        "region": cells[2] if len(cells) >= 3 else "",
        "kind": cells[3] if len(cells) >= 4 else "",
    }
    return item


def get_latest_from_homepage():
    r = session.get(SASMEX_URL, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    candidates = []
    for row in soup.find_all("tr"):
        item = row_to_item(row)
        if item:
            candidates.append(item)

    # Fallback for pages where CAP filenames are not inside table rows.
    if not candidates:
        text = soup.get_text(" ", strip=True)
        ids = sorted(set(re.findall(r"\b(20\d{12})\.cap\b", text, re.I)), reverse=True)
        for cap_id in ids:
            candidates.append({
                "id": cap_id,
                "cap_url": urljoin(SASMEX_URL, f"{cap_id}.cap"),
                "context": text[:3000],
                "state": "",
                "region": "",
                "kind": "",
            })

    if not candidates:
        # Last fallback: bare 14-digit timestamps in page text.
        text = soup.get_text(" ", strip=True)
        ids = sorted(set(re.findall(r"\b(20\d{12})\b", text)), reverse=True)
        if ids:
            cap_id = ids[0]
            return {
                "id": cap_id,
                "cap_url": urljoin(SASMEX_URL, f"{cap_id}.cap"),
                "context": text[:3000],
                "state": "",
                "region": "",
                "kind": "",
            }
        raise RuntimeError("SASMEX respondió, pero no encontré registros CAP en la página.")

    candidates.sort(key=lambda x: x["id"], reverse=True)
    latest = candidates[0]

    # Extract the visible "Último CAP" headline/severity when available.
    page_text = clean(soup.get_text(" ", strip=True))
    latest["page_text"] = page_text[:6000]
    sev = re.search(r"Severidad\s*:\s*(Menor|Mayor|Minor|Major)", page_text, re.I)
    if sev:
        latest["page_severity"] = sev.group(1)

    head = re.search(r"Sismo\s+en\s+(.{2,120}?)(?=\s+Severidad\s*:)", page_text, re.I)
    if head:
        latest["page_headline"] = clean(head.group(0))

    return latest


def parse_cap(cap_url):
    if not cap_url:
        return {}
    try:
        r = session.get(cap_url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "xml")
        data = {}
        for tag in [
            "event", "severity", "urgency", "certainty", "headline", "description",
            "areaDesc", "effective", "onset", "expires", "sent"
        ]:
            el = soup.find(tag)
            if el and el.get_text(strip=True):
                data[tag] = clean(el.get_text(" ", strip=True))

        raw = soup.get_text(" ", strip=True)
        mag = re.search(
            r"(?:magnitud|magnitude)\s*[:=]?\s*M?\s*([0-9]+(?:[.,][0-9]+)?)",
            raw, re.I,
        )
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


def severity_values(item, cap):
    severity = clean(cap.get("severity") or item.get("page_severity") or "")
    low = severity.lower()
    if low in ("menor", "minor"):
        return "⚠️ <b>Sismo en Desarrollo</b> ⚠️", "MODERADO"
    if low in ("mayor", "major"):
        return "🚨 <b>ALERTA SÍSMICA</b> 🚨", "VIOLENTO"
    return "⚠️ <b>Sismo en Desarrollo</b> ⚠️", "NO DETERMINADA"


def format_message(item, cap):
    date, hour = format_date(item["id"])
    title, intensity = severity_values(item, cap)

    location = clean(cap.get("areaDesc") or item.get("region") or "")
    if not location:
        headline = clean(cap.get("headline") or item.get("page_headline") or "")
        location = re.sub(r"^Sismo\s+en\s+", "", headline, flags=re.I).strip()
    if not location:
        location = "Ubicación no indicada por SASMEX"

    magnitude = clean(cap.get("magnitude") or "")
    state = clean(item.get("state") or "")

    lines = [
        "#SismoDetectado #SismosMP",
        "",
        title,
        "",
        f"Iniciando en <b>{location}</b>",
    ]
    if date:
        lines.append(f"Fecha: {date}")
    if hour:
        lines.append(f"Hora: {hour}")
    lines.append(f"Intensidad: <b>{intensity}</b>")
    if magnitude:
        lines.append(f"Magnitud: <b>M {magnitude}</b>")
    if state:
        lines.append(f"Estado: {state}")
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
        try:
            telegram_api("pinChatMessage", {
                "chat_id": CHAT_ID,
                "message_id": status_message_id,
                "disable_notification": True,
            })
        except Exception as e:
            log.info("No se pudo fijar el mensaje de estado: %s", e)
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
            if getattr(e.response, "status_code", None) == 400:
                status_message_id = None
            else:
                raise


def status_text():
    now = now_local().strftime("%d/%m/%Y %H:%M:%S")
    state = "🟢 <b>CONECTADO</b>" if connected else "🔴 <b>SIN CONEXIÓN</b>"
    check = last_check.strftime("%H:%M:%S") if last_check else "--:--:--"
    event = last_event_id or "Aún sin evento"
    return (
        "🟢 <b>MONITOREANDO SISMOS</b>\n\n"
        f"📡 SASMEX: {state}\n"
        f"🕐 Hora actual: <b>{now}</b>\n"
        f"🔄 Última revisión: <b>{check}</b>"
    )


def status_loop():
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
            last_check = now_local()
            last_event_id = current_id
            connected = True
            log.info("SASMEX OK. Último CAP detectado: %s", current_id)

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
    return jsonify({"ok": True, "service": "sasmex-telegram-bot", "connected": connected, "last_event": last_event_id})


def start_monitor():
    Thread(target=monitor, daemon=True).start()
    Thread(target=status_loop, daemon=True).start()


if __name__ == "__main__":
    start_monitor()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
