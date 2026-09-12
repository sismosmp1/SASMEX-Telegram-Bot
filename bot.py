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
SASMEX_TELEGRAM_URL = os.getenv("SASMEX_TELEGRAM_URL", "https://t.me/s/SASMEX_Oficial")
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
session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; SismosMP-Bot/4.0)"})

status_message_id = None
last_check = None
last_event_id = None
connected = False
source_status = {"SASMEX": False, "SSN": False, "TELEGRAM": False}


def now_local():
    return datetime.now(TZ)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "sasmex_id": data.get("sasmex_id"),
                "ssn_id": data.get("ssn_id"),
                "telegram_post_id": data.get("telegram_post_id"),
                "seen_keys": data.get("seen_keys", [])[-100:],
            }
    except Exception:
        return {"sasmex_id": None, "ssn_id": None, "telegram_post_id": None, "seen_keys": []}


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
    r = session.get(SSN_URL, timeout=25)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = [el for el in root.iter() if el.tag.split("}")[-1].lower() == "item"]
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
        mag = ""
        m = re.search(r"magnitud\s*([0-9]+(?:[.,][0-9]+)?)", raw, re.I) or re.search(r"\bM\s*([0-9]+(?:[.,][0-9]+)?)", raw, re.I)
        if m:
            mag = m.group(1).replace(",", ".")
        location = ""
        lm = re.search(r"(?:localizad[oa]|epicentro|en)\s*[:\-]?\s*(.{5,160})", raw, re.I)
        if lm:
            location = clean(lm.group(1))
            location = re.split(r"\s+(?:a\s+)?\d+(?:\.\d+)?\s*km\b", location, flags=re.I)[0].strip(" -")
        if not event_id:
            event_id = clean(guid or link or title)
        parsed.append({"id": event_id, "source": "SSN", "title": title, "description": description, "link": link, "pubDate": pub, "location": location, "magnitude": mag, "raw": raw})

    parsed.sort(key=lambda x: x["id"] if re.fullmatch(r"20\d{12}", x["id"] or "") else "", reverse=True)
    return parsed[0]


def parse_sasmex_telegram():
    """Reads the public SASMEX Telegram channel page (t.me/s/...). No Telegram bot access to the channel is required."""
    r = session.get(SASMEX_TELEGRAM_URL, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = soup.select("div.tgme_widget_message[data-post]")
    if not posts:
        raise RuntimeError("El canal público de SASMEX no devolvió publicaciones.")

    parsed = []
    for post in posts:
        data_post = post.get("data-post", "")
        mpost = re.search(r"/([0-9]+)$", data_post)
        if not mpost:
            continue
        post_id = int(mpost.group(1))
        text_el = post.select_one(".tgme_widget_message_text")
        text = clean(text_el.get_text(" ", strip=True) if text_el else "")
        if not text:
            continue
        # Only treat posts that look like seismic reports as earthquake events.
        earthquake = bool(re.search(r"\b(sismo|sismos|magnitud|epicentro|intensidad|alerta sísmica|alerta s[íi]smica)\b", text, re.I))
        if not earthquake:
            continue
        dt_id = parse_dt_id(text)
        time_el = post.select_one("time[datetime]")
        published = time_el.get("datetime", "") if time_el else ""
        parsed.append({
            "id": str(post_id),
            "post_id": post_id,
            "source": "TELEGRAM",
            "text": text,
            "event_id": dt_id,
            "published": published,
            "url": f"https://t.me/SASMEX_Oficial/{post_id}",
        })

    if not parsed:
        raise RuntimeError("No se encontraron publicaciones sísmicas recientes en el canal de SASMEX.")
    parsed.sort(key=lambda x: x["post_id"], reverse=True)
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
    return telegram_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False})


def telegram_edit_status(text):
    global status_message_id
    if not CHAT_ID:
        raise RuntimeError("Falta CHAT_ID.")
    if status_message_id is None:
        result = telegram_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        status_message_id = result["result"]["message_id"]
        try:
            telegram_api("pinChatMessage", {"chat_id": CHAT_ID, "message_id": status_message_id, "disable_notification": True})
        except Exception as e:
            log.info("No se pudo fijar el estado: %s", e)
    else:
        try:
            telegram_api("editMessageText", {"chat_id": CHAT_ID, "message_id": status_message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) == 400:
                status_message_id = None
            else:
                raise


def status_text():
    now = now_local().strftime("%d/%m/%Y %H:%M:%S")
    sasmex = "🟢 <b>CONECTADO</b>" if source_status["SASMEX"] else "🔴 <b>SIN CONEXIÓN</b>"
    check = last_check.strftime("%H:%M:%S") if last_check else "--:--:--"
    return "🟢 <b>MONITOREANDO SISMOS</b>\n\n" f"📡 SASMEX: {sasmex}\n" f"🕐 Hora actual: <b>{now}</b>\n" f"🔄 Última revisión: <b>{check}</b>"


def sasmex_message(item):
    event_id = item.get("id")
    date, hour = format_date_from_id(event_id)
    sev = (item.get("severity") or "").lower()
    if sev in ("mayor", "major"):
        title, intensity = "🚨 <b>ALERTA SÍSMICA</b> 🚨", "VIOLENTO"
    elif sev in ("menor", "minor"):
        title, intensity = "⚠️ <b>Sismo en Desarrollo</b> ⚠️", "MODERADO"
    else:
        title, intensity = "⚠️ <b>Sismo en Desarrollo</b> ⚠️", "NO DETERMINADA"
    location = clean(item.get("location") or "Ubicación no indicada por SASMEX")
    lines = ["#SismoDetectado #SismosMP", "", title, "", f"Iniciando en <b>{location}</b>"]
    if date: lines.append(f"Fecha: {date}")
    if hour: lines.append(f"Hora: {hour}")
    lines.append(f"Intensidad: <b>{intensity}</b>")
    if item.get("state"): lines.append(f"Estado: {clean(item['state'])}")
    lines += ["", "📡 Fuente: SASMEX"]
    if item.get("cap_url"): lines.append(f'<a href="{htmlmod.escape(item["cap_url"], quote=True)}">Ver CAP</a>')
    return "\n".join(lines)


def ssn_message(item):
    event_id = item.get("id")
    date, hour = format_date_from_id(event_id)
    location = clean(item.get("location") or item.get("title") or "Ubicación no indicada por SSN")
    lines = ["#SismoDetectado #SismosMP", "", "🌎 <b>Sismo detectado</b>", "", f"Ubicación: <b>{location}</b>"]
    if date: lines.append(f"Fecha: {date}")
    if hour: lines.append(f"Hora: {hour}")
    if item.get("magnitude"): lines.append(f"Magnitud: <b>M {htmlmod.escape(item['magnitude'])}</b>")
    lines += ["", "📡 Fuente: SSN"]
    if item.get("link"): lines.append(f'<a href="{htmlmod.escape(item["link"], quote=True)}">Ver registro SSN</a>')
    return "\n".join(lines)


def sasmex_telegram_message(item):
    text = clean(item.get("text", ""))
    # Keep the official wording visible, but wrap it in our channel format.
    return "#SismoDetectado #SismosMP\n\n📱 <b>SASMEX Oficial</b>\n\n" + htmlmod.escape(text) + f'\n\n📡 Fuente: <a href="{item["url"]}">SASMEX Oficial en Telegram</a>'


def event_key(item, source):
    eid = item.get("event_id") or item.get("id")
    if source == "TELEGRAM" and eid:
        return "event:" + eid
    if source == "SSN":
        return "event:" + str(eid)
    return source + ":" + str(eid)


def remember_key(state, key):
    keys = state.get("seen_keys", [])
    if key in keys:
        return
    keys.append(key)
    state["seen_keys"] = keys[-100:]


def publish_if_new(state, key, message, label):
    if key in state.get("seen_keys", []):
        return False
    telegram_send(message)
    remember_key(state, key)
    save_state(state)
    log.info("Publicado nuevo evento %s (%s)", label, key)
    return True


def monitor():
    global last_check, last_event_id, connected
    log.info("Monitor iniciado: SASMEX + SSN + Telegram SASMEX; revisión cada %ss", CHECK_SECONDS)
    state = load_state()
    initialized_sasmex = state.get("sasmex_id") is not None
    initialized_ssn = state.get("ssn_id") is not None
    initialized_tg = state.get("telegram_post_id") is not None

    while True:
        sasmex_item = ssn_item = tg_item = None
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

            try:
                tg_item = parse_sasmex_telegram()
                source_status["TELEGRAM"] = True
                log.info("Telegram SASMEX OK. Última publicación: %s", tg_item.get("post_id"))
            except Exception as e:
                source_status["TELEGRAM"] = False
                log.warning("Telegram SASMEX error: %s", e)

            connected = source_status["SASMEX"] or source_status["SSN"] or source_status["TELEGRAM"]
            last_check = now_local()

            if sasmex_item and sasmex_item.get("id"):
                sid = sasmex_item["id"]
                if not initialized_sasmex:
                    state["sasmex_id"] = sid; initialized_sasmex = True; save_state(state)
                    log.info("SASMEX inicializado con %s (sin publicar histórico).", sid)
                elif sid != state.get("sasmex_id"):
                    publish_if_new(state, event_key(sasmex_item, "SASMEX"), sasmex_message(sasmex_item), "SASMEX")
                    state["sasmex_id"] = sid; save_state(state)

            if ssn_item and ssn_item.get("id"):
                iid = ssn_item["id"]
                if not initialized_ssn:
                    state["ssn_id"] = iid; initialized_ssn = True; save_state(state)
                    log.info("SSN inicializado con %s (sin publicar histórico).", iid)
                elif iid != state.get("ssn_id"):
                    publish_if_new(state, event_key(ssn_item, "SSN"), ssn_message(ssn_item), "SSN")
                    state["ssn_id"] = iid; save_state(state)

            if tg_item and tg_item.get("post_id"):
                pid = tg_item["post_id"]
                if not initialized_tg:
                    state["telegram_post_id"] = pid; initialized_tg = True; save_state(state)
                    log.info("Telegram SASMEX inicializado con %s (sin publicar histórico).", pid)
                elif pid != state.get("telegram_post_id"):
                    key = event_key(tg_item, "TELEGRAM")
                    # If the Telegram post contains the same timestamp already seen from SSN/SASMEX,
                    # remember it but do not send a duplicate.
                    if key in state.get("seen_keys", []):
                        log.info("Telegram SASMEX %s duplicado; no se publica.", pid)
                    else:
                        publish_if_new(state, key, sasmex_telegram_message(tg_item), "Telegram SASMEX")
                    state["telegram_post_id"] = pid; save_state(state)

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
    return jsonify({"ok": True, "sasmex_connected": source_status["SASMEX"], "ssn_connected": source_status["SSN"], "telegram_connected": source_status["TELEGRAM"], "connected": connected, "last_event": last_event_id})


if __name__ == "__main__":
    Thread(target=monitor, daemon=True).start()
    Thread(target=status_loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
