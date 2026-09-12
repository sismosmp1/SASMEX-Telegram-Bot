import os
import re
import time
import json
import logging
import html as htmlmod
import xml.etree.ElementTree as ET
from datetime import datetime
from threading import Thread
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify

SASMEX_URL = os.getenv("SASMEX_URL", "https://rss.sasmex.net/")
SSN_URL = os.getenv("SSN_URL", "http://www.ssn.unam.mx/rss/ultimos-sismos.xml")
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
log = logging.getLogger("sismosmp-bot")

app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SismosMP-Bot/3.0)"})

status_message_id = None
last_check = None
last_event_id = None
connected = False
source_status = {"SASMEX": False, "SSN": False}


def now_local():
    return datetime.now(TZ)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "sasmex_id": data.get("sasmex_id"),
                "ssn_id": data.get("ssn_id"),
            }
    except Exception:
        return {"sasmex_id": None, "ssn_id": None}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        log.warning("No se pudo guardar estado: %s", e)


def clean(text):
    return re.sub(r"\s+", " ", htmlmod.unescape(text or "")).strip()


def parse_dt_id(text):
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})[ T]?(\d{2})[:.]?(\d{2})[:.]?(\d{2})", text or "")
    if m:
        return "".join(m.groups())
    return None


def format_date_from_id(event_id):
    try:
        dt = datetime.strptime(event_id, "%Y%m%d%H%M%S")
        return dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M:%S")
    except Exception:
        return None, None


def parse_sasmex_page():
    """Reads the SASMEX page/feed without requiring a .cap file."""
    r = session.get(SASMEX_URL, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))

    severity = ""
    sev = re.search(r"Severidad\s*:\s*(Menor|Mayor|Minor|Major)", page_text, re.I)
    if sev:
        severity = sev.group(1)

    candidates = []
    for row in soup.find_all("tr"):
        cells = [clean(c.get_text(" ", strip=True)) for c in row.find_all(["td", "th"])]
        if not cells:
            continue
        joined = " | ".join(cells)
        cap = re.search(r"\b(20\d{12})\.cap\b", joined, re.I)
        event_id = cap.group(1) if cap else parse_dt_id(joined)
        # SASMEX tables normally have: date/time | state | region | kind | file
        if event_id and len(cells) >= 2:
            candidates.append({
                "id": event_id,
                "source": "SASMEX",
                "state": cells[1] if len(cells) > 1 else "",
                "location": cells[2] if len(cells) > 2 else "",
                "kind": cells[3] if len(cells) > 3 else "",
                "context": joined,
                "severity": severity,
                "cap_url": urljoin(SASMEX_URL, f"{event_id}.cap") if cap else None,
            })

    # If no table event can be extracted, still consider SASMEX connected.
    # We return a heartbeat-only object so the monitor does not show SIN CONEXIÓN.
    if candidates:
        candidates.sort(key=lambda x: x["id"], reverse=True)
        item = candidates[0]
        item["page_text"] = page_text[:6000]
        return item

    return {
        "id": None,
        "source": "SASMEX",
        "state": "",
        "location": "",
        "kind": "",
        "context": page_text[:2000],
        "severity": severity,
        "cap_url": None,
        "page_text": page_text[:6000],
    }


def parse_ssn_feed():
    """Reads the official SSN RSS and returns the newest earthquake item."""
    r = session.get(SSN_URL, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    items = root.findall(".//item")
    if not items:
        # Handle RSS namespaces, if present.
        items = [el for el in root.iter() if el.tag.lower().endswith("}item") or el.tag.lower() == "item"]
    if not items:
        raise RuntimeError("El RSS del SSN respondió pero no contiene elementos item.")

    def val(item, name):
        for child in list(item):
            tag = child.tag.split("}")[-1].lower()
            if tag == name.lower():
                return clean(child.text or "")
        return ""

    parsed = []
    for item in items:
        title = val(item, "title")
        description = val(item, "description")
        link = val(item, "link")
        guid = val(item, "guid")
        pub = val(item, "pubDate")
        raw = " | ".join(x for x in [title, description, pub, guid] if x)
        event_id = parse_dt_id(raw)

        # SSN titles commonly contain: Magnitud ... - ...
        mag = ""
        m = re.search(r"magnitud\s*([0-9]+(?:[.,][0-9]+)?)", raw, re.I)
        if not m:
            m = re.search(r"\bM\s*([0-9]+(?:[.,][0-9]+)?)", raw, re.I)
        if m:
            mag = m.group(1).replace(",", ".")

        # Extract common SSN location phrase from description/title.
        location = ""
        lm = re.search(r"(?:localizad[oa]|epicentro|en)\s*[:\-]?\s*(.{5,160})", raw, re.I)
        if lm:
            location = clean(lm.group(1))
            location = re.split(r"\s+(?:a\s+)?\d+(?:\.\d+)?\s*km\b", location, flags=re.I)[0].strip(" -")

        if not event_id:
            event_id = clean(guid or link or title)
        parsed.append({
            "id": event_id,
            "source": "SSN",
            "title": title,
            "description": description,
            "link": link,
            "pubDate": pub,
            "location": location,
            "magnitude": mag,
            "raw": raw,
        })

    # Prefer the most recent timestamp-like ID.
    parsed.sort(key=lambda x: x["id"] if re.fullmatch(r"20\d{12}", x["id"] or "") else "", reverse=True)
    return parsed[0]


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
            log.info("No se pudo fijar el estado: %s", e)
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
    sasmex = "🟢 <b>CONECTADO</b>" if source_status["SASMEX"] else "🔴 <b>SIN CONEXIÓN</b>"
    check = last_check.strftime("%H:%M:%S") if last_check else "--:--:--"
    return (
        "🟢 <b>MONITOREANDO SISMOS</b>\n\n"
        f"📡 SASMEX: {sasmex}\n"
        f"🕐 Hora actual: <b>{now}</b>\n"
        f"🔄 Última revisión: <b>{check}</b>"
    )


def sasmex_message(item):
    event_id = item.get("id")
    date, hour = format_date_from_id(event_id)
    sev = (item.get("severity") or "").lower()
    if sev in ("mayor", "major"):
        title = "🚨 <b>ALERTA SÍSMICA</b> 🚨"
        intensity = "VIOLENTO"
    elif sev in ("menor", "minor"):
        title = "⚠️ <b>Sismo en Desarrollo</b> ⚠️"
        intensity = "MODERADO"
    else:
        title = "⚠️ <b>Sismo en Desarrollo</b> ⚠️"
        intensity = "NO DETERMINADA"

    location = clean(item.get("location") or "Ubicación no indicada por SASMEX")
    lines = ["#SismoDetectado #SismosMP", "", title, "", f"Iniciando en <b>{location}</b>"]
    if date:
        lines.append(f"Fecha: {date}")
    if hour:
        lines.append(f"Hora: {hour}")
    lines.append(f"Intensidad: <b>{intensity}</b>")
    if item.get("state"):
        lines.append(f"Estado: {clean(item['state'])}")
    lines += ["", "📡 Fuente: SASMEX"]
    if item.get("cap_url"):
        lines.append(f'<a href="{htmlmod.escape(item["cap_url"], quote=True)}">Ver CAP</a>')
    return "\n".join(lines)


def ssn_message(item):
    event_id = item.get("id")
    date, hour = format_date_from_id(event_id)
    location = clean(item.get("location") or item.get("title") or "Ubicación no indicada por SSN")
    # SSN is the earthquake catalog source; it does not determine SASMEX alert severity.
    lines = [
        "#SismoDetectado #SismosMP",
        "",
        "🌎 <b>Sismo detectado</b>",
        "",
        f"Ubicación: <b>{location}</b>",
    ]
    if date:
        lines.append(f"Fecha: {date}")
    if hour:
        lines.append(f"Hora: {hour}")
    if item.get("magnitude"):
        lines.append(f"Magnitud: <b>M {htmlmod.escape(item['magnitude'])}</b>")
    lines += ["", "📡 Fuente: SSN"]
    if item.get("link"):
        lines.append(f'<a href="{htmlmod.escape(item["link"], quote=True)}">Ver registro SSN</a>')
    return "\n".join(lines)


def monitor():
    global last_check, last_event_id, connected
    log.info("Monitor iniciado: SASMEX + SSN; revisión cada %ss", CHECK_SECONDS)
    state = load_state()
    initialized_sasmex = state.get("sasmex_id") is not None
    initialized_ssn = state.get("ssn_id") is not None

    while True:
        sasmex_item = None
        ssn_item = None
        try:
            try:
                sasmex_item = parse_sasmex_page()
                source_status["SASMEX"] = True
                if sasmex_item.get("id"):
                    last_event_id = sasmex_item["id"]
                    log.info("SASMEX OK. Último registro: %s", sasmex_item["id"])
            except Exception as e:
                source_status["SASMEX"] = False
                log.warning("SASMEX error: %s", e)

            try:
                ssn_item = parse_ssn_feed()
                source_status["SSN"] = True
                log.info("SSN OK. Último sismo: %s", ssn_item.get("id"))
            except Exception as e:
                source_status["SSN"] = False
                log.warning("SSN error: %s", e)

            connected = source_status["SASMEX"] or source_status["SSN"]
            last_check = now_local()

            # First successful read establishes the baseline and does not publish old events.
            if sasmex_item and sasmex_item.get("id"):
                sid = sasmex_item["id"]
                if not initialized_sasmex:
                    state["sasmex_id"] = sid
                    initialized_sasmex = True
                    save_state(state)
                    log.info("SASMEX inicializado con %s (sin publicar histórico).", sid)
                elif sid != state.get("sasmex_id"):
                    telegram_send(sasmex_message(sasmex_item))
                    state["sasmex_id"] = sid
                    save_state(state)
                    log.info("Publicado nuevo evento SASMEX %s", sid)

            if ssn_item and ssn_item.get("id"):
                iid = ssn_item["id"]
                if not initialized_ssn:
                    state["ssn_id"] = iid
                    initialized_ssn = True
                    save_state(state)
                    log.info("SSN inicializado con %s (sin publicar histórico).", iid)
                elif iid != state.get("ssn_id"):
                    telegram_send(ssn_message(ssn_item))
                    state["ssn_id"] = iid
                    save_state(state)
                    log.info("Publicado nuevo sismo SSN %s", iid)

        except Exception:
            log.exception("Error durante la comprobación")
        time.sleep(CHECK_SECONDS)


def status_loop():
    while True:
        try:
            telegram_edit_status(status_text())
        except Exception:
            log.exception("No se pudo actualizar el mensaje de estado")
        time.sleep(STATUS_SECONDS)


@app.get("/")
def root():
    return "SismosMP bot activo.", 200


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "sasmex_connected": source_status["SASMEX"],
        "ssn_connected": source_status["SSN"],
        "connected": connected,
        "last_event": last_event_id,
    })


if __name__ == "__main__":
    Thread(target=monitor, daemon=True).start()
    Thread(target=status_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
