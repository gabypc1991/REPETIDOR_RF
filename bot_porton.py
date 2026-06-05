import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import serial
from serial import SerialException
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


# ==========================
# CONFIGURACION
# ==========================

BASE_DIR = Path(__file__).resolve().parent
USERS_FILE = BASE_DIR / "usuarios.json"
ENV_FILE = BASE_DIR / ".env"
CONTROL_TIMEOUT_SECONDS = 30


def cargar_env():
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


cargar_env()

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

SERIAL_PORT = os.getenv("PORTON_SERIAL_PORT", "COM7")
BAUDRATE = int(os.getenv("PORTON_BAUDRATE", "115200"))
EVENT_TIMEOUT_MIN = int(os.getenv("PORTON_EVENT_TIMEOUT_MIN", "5"))
INTERMEDIATE_WAIT_SECONDS = float(os.getenv("PORTON_INTERMEDIATE_WAIT_SECONDS", "3"))
HOME_REFRESH_SECONDS = int(os.getenv("PORTON_HOME_REFRESH_SECONDS", "60"))
SERIAL_MONITOR_ENABLED = os.getenv("PORTON_SERIAL_MONITOR", "1") == "1"

CLAVE_OBJETIVO = os.getenv("PORTON_PIN", "SMK").upper()

# El valor visible es el texto del boton; el valor interno es la letra real.
TECLADO = {
    "FAH": "A",
    "TJS": "S",
    "BDV": "B",
    "MCZ": "M",
    "QYK": "K",
    "POL": "P",
    "QWC": "Q",
    "NWB": "N",
    "BED": "D",
}


# ==========================
# ESTADO
# ==========================

usuarios_registrados = {}
usuarios_activos = set()
buffers_por_usuario = {}
eventos = []
arduino = None

control_usuario = None
control_nombre = None
control_expira = None

state_lock = threading.Lock()
serial_lock = threading.Lock()


# ==========================
# LOGGING
# ==========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bot_porton")


# ==========================
# VALIDACION INICIAL
# ==========================

if not BOT_TOKEN:
    raise RuntimeError("Falta la variable de entorno SLACK_BOT_TOKEN")

if not APP_TOKEN:
    raise RuntimeError("Falta la variable de entorno SLACK_APP_TOKEN")

app = App(token=BOT_TOKEN)


# ==========================
# ARCHIVO DE USUARIOS
# ==========================

def cargar_usuarios():
    global usuarios_registrados

    if not USERS_FILE.exists():
        usuarios_registrados = {}
        return

    try:
        with USERS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            raise ValueError("usuarios.json debe contener un objeto JSON")

        usuarios_registrados = data

    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.error("No se pudo cargar usuarios.json: %s", e)
        usuarios_registrados = {}


def guardar_usuarios():
    temp_file = USERS_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(
            usuarios_registrados,
            f,
            indent=4,
            ensure_ascii=False,
        )

    temp_file.replace(USERS_FILE)


# ==========================
# SERIAL
# ==========================

def obtener_serial():
    global arduino

    with serial_lock:
        if arduino and arduino.is_open:
            return arduino

        try:
            arduino = serial.Serial(
                SERIAL_PORT,
                BAUDRATE,
                timeout=0.2,
            )
            logger.info("Puerto serial conectado en %s", SERIAL_PORT)
            return arduino

        except SerialException as e:
            arduino = None
            logger.error("No se pudo abrir el puerto serial %s: %s", SERIAL_PORT, e)
            return None


def send_pulse():
    puerto = obtener_serial()

    if not puerto:
        registrar_evento("ERROR: puerto serial no disponible")
        return False

    try:
        with serial_lock:
            puerto.write(b"CMD:PULSE\n")
            puerto.flush()

        logger.info("CMD:PULSE enviado")
        return True

    except SerialException as e:
        logger.error("Error enviando pulso: %s", e)
        registrar_evento("ERROR: no se pudo enviar el pulso")
        return False


def monitorear_serial():
    logger.info("Monitor serial RF activo en %s a %s baudios", SERIAL_PORT, BAUDRATE)

    while True:
        try:
            puerto = obtener_serial()

            if not puerto:
                time.sleep(5)
                continue

            with serial_lock:
                line = puerto.readline()

            if not line:
                continue

            texto = line.decode("utf-8", errors="replace").strip()

            if texto:
                logger.info("RF_NANO: %s", texto)

        except SerialException as e:
            logger.error("Error leyendo serial RF: %s", e)
            time.sleep(5)

        except Exception:
            logger.exception("Error inesperado en monitor serial RF")
            time.sleep(5)


def send_intermediate_pulse():
    if not send_pulse():
        return False

    time.sleep(INTERMEDIATE_WAIT_SECONDS)

    return send_pulse()


# ==========================
# EVENTOS / HOME
# ==========================

def verificar_control_activo():
    global control_usuario, control_nombre, control_expira

    ahora = datetime.now()

    if control_expira and ahora >= control_expira:
        control_usuario = None
        control_nombre = None
        control_expira = None

    return control_usuario


def tomar_control(user_id, nombre):
    global control_usuario, control_nombre, control_expira

    control_usuario = user_id
    control_nombre = nombre
    control_expira = datetime.now() + timedelta(
        seconds=CONTROL_TIMEOUT_SECONDS
    )

def usuario_puede_operar(user_id, nombre):
    with state_lock:
        dueño = verificar_control_activo()

        if dueño is None:
            tomar_control(user_id, nombre)
            return True

        if dueño == user_id:
            return True

        registrar_evento(
            f"{nombre}: {control_nombre} ya envio una orden, revisar que el porton esta abriendo/cerrando."
        )

        return False

def registrar_evento(texto):
    with state_lock:
        eventos.insert(0, {
            "hora": datetime.now(),
            "texto": texto,
        })
        limpiar_eventos_locked()


def limpiar_eventos_locked():
    ahora = datetime.now()
    cantidad_anterior = len(eventos)

    eventos[:] = [
        e for e in eventos
        if ahora - e["hora"] < timedelta(minutes=EVENT_TIMEOUT_MIN)
    ]

    cantidad_actual = len(eventos)

    return cantidad_anterior - cantidad_actual


def limpiar_eventos_expirados():
    with state_lock:
        return limpiar_eventos_locked()


def obtener_historial():
    with state_lock:
        limpiar_eventos_locked()

        if not eventos:
            return "Sin eventos registrados"

        return "\n".join(
            f"{e['hora']:%H:%M:%S} - {e['texto']}"
            for e in eventos
        )


def obtener_nombre_usuario(client, user_id):
    try:
        response = client.users_info(user=user_id)
        user = response.get("user", {})
        return user.get("real_name") or user.get("name") or user_id

    except Exception as e:
        logger.warning("No se pudo obtener usuario %s: %s", user_id, e)
        return user_id


def usuario_autorizado(user_id):
    with state_lock:
        return user_id in usuarios_registrados


def obtener_usuarios_registrados(excluir_user_id=None):
    with state_lock:
        return [
            user_id
            for user_id in usuarios_registrados
            if user_id != excluir_user_id
        ]


def notificar_orden_a_registrados(client, autor_user_id, autor_nombre, orden):
    destinatarios = obtener_usuarios_registrados(excluir_user_id=autor_user_id)

    if not destinatarios:
        return

    mensaje = f"{autor_nombre} ejecuto el comando: {orden}"

    for user_id in destinatarios:
        try:
            client.chat_postMessage(
                channel=user_id,
                text=mensaje,
            )
        except Exception as e:
            logger.warning("No se pudo notificar a %s: %s", user_id, e)


def actualizar_home_para_todos(client):
    with state_lock:
        user_ids = list(usuarios_activos)

    for user_id in user_ids:
        try:
            client.views_publish(
                user_id=user_id,
                view=build_home_view(user_id),
            )
        except Exception as e:
            logger.warning("No se pudo actualizar Home de %s: %s", user_id, e)


def refrescar_home_periodicamente():
    logger.info(
        "Refresco automatico de Home activo cada %s segundos; eventos visibles por %s minutos",
        HOME_REFRESH_SECONDS,
        EVENT_TIMEOUT_MIN,
    )

    while True:
        try:
            time.sleep(HOME_REFRESH_SECONDS)

            eventos_borrados = limpiar_eventos_expirados()

            if eventos_borrados:
                logger.info(
                    "%s evento(s) expirado(s) limpiado(s); actualizando Home",
                    eventos_borrados,
                )
                actualizar_home_para_todos(app.client)

        except Exception:
            logger.exception("Error en refresco automatico de Home")


def build_home_view(user_id):
    autorizado = usuario_autorizado(user_id)

    with state_lock:
        buffer_usuario = buffers_por_usuario.get(user_id, "")

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Control apertura porton Simplemak",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Estado:* ONLINE",
            },
        },
    ]

    if autorizado:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Pulso",
                    },
                    "style": "primary",
                    "action_id": "relay_on",
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Peaton",
                    },
                    "action_id": "relay_intermediate",
                }
            ],
        })

    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Registrarse:* `{buffer_usuario}`",
            },
        })

        teclas = list(TECLADO.keys())
        for i in range(0, len(teclas), 3):
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": tecla,
                        },
                        "action_id": f"key_{tecla}",
                    }
                    for tecla in teclas[i:i + 3]
                ],
            })

    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": obtener_historial(),
        },
    })

    return {
        "type": "home",
        "blocks": blocks,
    }


# ==========================
# REGISTRO
# ==========================

def procesar_tecla(token_tecla, client, user_id):
    letra = TECLADO.get(token_tecla)

    if not letra:
        registrar_evento("Tecla invalida")
        actualizar_home_para_todos(client)
        return

    nombre = obtener_nombre_usuario(client, user_id)

    with state_lock:
        buffer_actual = buffers_por_usuario.get(user_id, "")
        buffer_actual = (buffer_actual + letra)[-len(CLAVE_OBJETIVO):]
        buffers_por_usuario[user_id] = buffer_actual

        if buffer_actual == CLAVE_OBJETIVO:
            buffers_por_usuario.pop(user_id, None)
            usuarios_registrados[user_id] = {
                "nombre": nombre,
                "registrado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            guardar_usuarios()
            evento = f"{nombre} - Registro OK"

        elif len(buffer_actual) == len(CLAVE_OBJETIVO):
            buffers_por_usuario.pop(user_id, None)
            evento = "PIN incorrecto"

        else:
            evento = None

    if evento:
        registrar_evento(evento)

    actualizar_home_para_todos(client)


# ==========================
# SLACK EVENTS / ACTIONS
# ==========================

@app.event("app_home_opened")
def update_home_tab(event, client):
    user_id = event["user"]

    with state_lock:
        usuarios_activos.add(user_id)

    client.views_publish(
        user_id=user_id,
        view=build_home_view(user_id),
    )


@app.action(re.compile(r"^key_"))
def handle_key(ack, body, client):
    ack()

    action_id = body["actions"][0]["action_id"]
    token_tecla = action_id.replace("key_", "", 1)

    procesar_tecla(token_tecla, client, body["user"]["id"])


@app.action("relay_on")
def relay_on(ack, body, client):
    ack()

    user_id = body["user"]["id"]
    nombre = obtener_nombre_usuario(client, user_id)

    if not usuario_autorizado(user_id):
        registrar_evento(f"{nombre} - Usuario no registrado")
        actualizar_home_para_todos(client)
        return
    
    if not usuario_puede_operar(user_id, nombre):
        actualizar_home_para_todos(client)
        return

    if send_pulse():
        registrar_evento(f"{nombre} - PULSO")
        # notificar_orden_a_registrados(client, user_id, nombre, "Pulso")

    actualizar_home_para_todos(client)


@app.action("relay_intermediate")
def relay_intermediate(ack, body, client):
    ack()

    user_id = body["user"]["id"]
    nombre = obtener_nombre_usuario(client, user_id)

    if not usuario_autorizado(user_id):
        registrar_evento(f"{nombre} - Usuario no registrado")
        actualizar_home_para_todos(client)
        return

    if not usuario_puede_operar(user_id, nombre):
        actualizar_home_para_todos(client)
        return

    registrar_evento(f"{nombre} - Paso peaton iniciado")
    actualizar_home_para_todos(client)

    if send_intermediate_pulse():
        registrar_evento(f"{nombre} - Paso peaton OK")
        # notificar_orden_a_registrados(client, user_id, nombre, "Peaton")
    else:
        registrar_evento(f"{nombre} - Paso peaton ERROR")

    actualizar_home_para_todos(client)


@app.event("app_mention")
def handle_mention(body, say, client):
    try:
        event = body["event"]
        user_id = event["user"]
        nombre = obtener_nombre_usuario(client, user_id)

        text = event.get("text", "")
        cmd = text.split(">", 1)[1].strip() if ">" in text else text.strip()
        cmd = cmd.lower()

        logger.info("Comando recibido de %s: %s", nombre, cmd)

        if cmd in ("help", "ayuda"):
            say(
                "*Control Porton Simplemak*\n\n"
                "`@botapp on` - Envia un pulso si el usuario esta autorizado\n"
                "Tambien podes usar el boton *Pulso* desde la pestana Home."
            )
            return

        if cmd == "on":
            if not usuario_autorizado(user_id):
                registrar_evento(f"{nombre} - Intento no autorizado")
                say("No estas autorizado para abrir el porton.")
                actualizar_home_para_todos(client)
                return

            if send_pulse():
                registrar_evento(f"{nombre} - PULSO")
                # notificar_orden_a_registrados(client, user_id, nombre, "@bot on")
                say("Pulso enviado.")
            else:
                say("No se pudo enviar el pulso. Revisar conexion serial.")

            actualizar_home_para_todos(client)
            return

        say(
            f"Comando desconocido: `{cmd}`\n"
            "Usar `@botapp help`"
        )

    except Exception as e:
        logger.exception("Error procesando mencion")
        say(f"Error:\n```{str(e)}```")


# ==========================
# MAIN
# ==========================

if __name__ == "__main__":
    cargar_usuarios()

    logger.info("Bot iniciado")
    logger.info("Conectando a Slack...")

    threading.Thread(
        target=refrescar_home_periodicamente,
        daemon=True,
    ).start()

    if SERIAL_MONITOR_ENABLED:
        threading.Thread(
            target=monitorear_serial,
            daemon=True,
        ).start()

    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()
