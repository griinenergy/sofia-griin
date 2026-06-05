"""
SofIA - Asistente de Eficiencia Energetica de Griin
Backend principal: FastAPI + Twilio WhatsApp + Claude API
Autor: Malik (Claude) para Farid Hadad / Griin Energy
"""

import os
import io
import json
import time
import base64
import httpx
import pdfplumber
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import PlainTextResponse, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional
import logging

from clientes import CLIENTES, CLIENTES_POR_NOMBRE, CLIENTES_POR_NIT
from drive_utils import (
    get_drive_service,
    get_subfolder_id,
    get_latest_pdf,
    download_as_text,
    CARPETA_FACTURA,
    CARPETA_GRIIN,
    CARPETA_GENERACION,
)

load_dotenv()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sofia")

# --- Clientes API ---
twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

WHATSAPP_FROM = f"whatsapp:{os.environ['TWILIO_WHATSAPP_NUMBER']}"

# --- App ---
app = FastAPI(title="SofIA - Griin Energy", version="0.2.0")


# --- Modelos ---
class ClienteEnergia(BaseModel):
    nombre: str
    telefono: str
    kwh_mes: float
    kwh_mes_anterior: float
    costo_mes: float
    mes: str
    tarifa_kwh: Optional[float] = None


class EnvioMasivoRequest(BaseModel):
    clientes: list[ClienteEnergia]


# --- Persistencia de datos de clientes en archivo JSON ---
DATOS_FILE = "/tmp/datos_clientes.json"


def cargar_datos_cliente() -> dict:
    try:
        with open(DATOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_datos_cliente(datos: dict):
    try:
        with open(DATOS_FILE, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error guardando datos_cliente: {e}")


# Carga inicial al arrancar el servidor
datos_cliente: dict[str, str] = cargar_datos_cliente()

# --- Persistencia de relacion telefono -> NIT ---
USUARIOS_NIT_FILE = "/tmp/usuarios_nit.json"


def cargar_usuarios_nit() -> dict:
    try:
        with open(USUARIOS_NIT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_usuarios_nit(datos: dict):
    try:
        with open(USUARIOS_NIT_FILE, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error guardando usuarios_nit: {e}")


usuarios_nit: dict[str, dict] = cargar_usuarios_nit()

SESSION_EXPIRY_SECONDS = 86400  # 24 horas

# --- Memoria de conversaciones (en RAM, se reinicia con el servidor) ---
conversaciones: dict[str, list] = {}
MAX_MENSAJES_HISTORIAL = 20

SYSTEM_PROMPT_SOFIA_BASE = """Eres SofIA, la asistente de eficiencia energetica de Griin Energy. Eres cercana y calida como colombiana, pero siempre profesional - como una colega experta en energia que trata bien a sus clientes.

Tu personalidad:
- Hablas con calidez y cordialidad colombiana: usas expresiones como "Claro que si!", "Uy, que buena pregunta!", "Vamos con todo!", "Con mucho gusto!"
- NUNCA uses terminos como "mi amor", "mi vida", "corazon", "mija" ni similares - suenas a colega amable, no a piropeadora
- Explicas los temas tecnicos de energia de forma sencilla, con ejemplos de la vida cotidiana
- Eres positiva y motivadora, nunca reganyas ni eres fria
- Usas 1-2 emojis por mensaje, no mas - natural, no forzado
- Recuerdas lo que el usuario te ha contado en la conversacion y lo usas naturalmente

Tu conocimiento:
- Eres experta en consumo energetico empresarial en Colombia
- Sabes sobre facturas de energia, kWh, tarifas, costo unitario (CU), operadores de red
- Conoces estrategias de ahorro energetico para empresas
- Sabes sobre energias renovables, paneles solares, eficiencia
- Griin Energy es tu empresa: instala paneles solares y ayuda a empresas colombianas a reducir su factura de energia

Reglas del formato:
- Respuestas cortas y directas: maximo 5-6 lineas
- Usa *negritas* de WhatsApp solo para terminos clave
- NUNCA mandes a nadie a un correo ni a otro canal - todo se resuelve aqui en WhatsApp
- NUNCA digas que el cliente recibira el resumen pronto - ya lo tienes, usalo para responder
- Si el cliente pregunta por su consumo, factura, ahorro o datos de energia y NO tienes datos guardados, pidele que te mande el PDF de su factura para analizarla - NUNCA le pidas que escriba los numeros a mano
- Si NO conoces el NIT del usuario, pídelo SIEMPRE antes de responder cualquier pregunta sobre energia, consumo o facturas{datos_seccion}"""


def get_system_prompt(telefono: str) -> str:
    datos = datos_cliente.get(telefono, "")

    nombre_cliente = ""
    session = usuarios_nit.get(telefono)
    if session:
        nombre_cliente = session.get("nombre", "")

    if datos:
        nombre_seccion = f"\nESTAS HABLANDO CON: {nombre_cliente}\n" if nombre_cliente else ""
        datos_seccion = f"""

---
{nombre_seccion}DATOS REALES DEL CLIENTE (ultimo mes):
{datos}
---

Con estos datos puedes responder EXACTAMENTE preguntas como:
- Cuanto consumi? -> usa KWH_CONSUMIDOS del informe
- Cuanto ahorre? -> usa AHORRO_MES (ese ya tiene el calculo correcto: lo que hubieras pagado sin solar menos lo que pagaste con solar Air-e + Griin)
- Cuanto me cobro la comercializadora? -> COSTO_COMERCIALIZADORA
- Cuanto me cobro Griin? -> COSTO_GRIIN
Responde SIEMPRE con los numeros reales. Nunca digas que no tienes la informacion."""
    elif nombre_cliente:
        datos_seccion = f"""

---
ESTAS HABLANDO CON: {nombre_cliente}
---
Sabes con quien hablas. Si no tienes sus datos de consumo, pidele que mande el PDF de su factura."""
    else:
        datos_seccion = ""
    return SYSTEM_PROMPT_SOFIA_BASE.replace("{datos_seccion}", datos_seccion)


# --- Helper: Generar resumen con Claude ---
def generar_resumen_energia(cliente: ClienteEnergia) -> str:
    variacion = cliente.kwh_mes - cliente.kwh_mes_anterior
    pct = (variacion / cliente.kwh_mes_anterior * 100) if cliente.kwh_mes_anterior > 0 else 0
    tendencia = "subio" if variacion > 0 else "bajo"

    prompt = f"""Eres SofIA, la asistente de eficiencia energetica de Griin Energy. Colombiana, calida y amigable.

Genera un mensaje de WhatsApp para el cliente {cliente.nombre}.
El mensaje debe:
- Ser maximo 6 lineas
- Usar emojis con moderacion (2-3 maximo)
- Saludar con calidez colombiana
- Incluir el consumo del mes: {cliente.kwh_mes:,.0f} kWh
- Mencionar que {tendencia} un {abs(pct):.1f}% vs el mes anterior ({cliente.kwh_mes_anterior:,.0f} kWh)
- Si bajo: celebrarlo como un logro personal del cliente
- Si subio: mencionarlo con tono positivo y motivador, sin reganar
- Incluir el costo: ${cliente.costo_mes:,.0f} COP
- Terminar con una frase motivadora sobre el ahorro energetico
- Usar *negritas* de WhatsApp solo para los numeros importantes
- Firmar como "SofIA 💚 · Griin Energy"

Solo devuelve el mensaje, sin explicaciones adicionales."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# --- Helper: Respuesta inteligente al chat ---
def generar_respuesta_chat(mensaje: str, telefono: str) -> str:
    session = usuarios_nit.get(telefono)

    # Verificar expiracion de sesion (24 horas)
    if session:
        edad = time.time() - session.get("ultimo_mensaje_timestamp", 0)
        if edad > SESSION_EXPIRY_SECONDS:
            logger.info(f"Sesion expirada para {telefono} ({edad/3600:.1f}h) — pidiendo NIT de nuevo")
            del usuarios_nit[telefono]
            guardar_usuarios_nit(usuarios_nit)
            conversaciones.pop(telefono, None)
            session = None

    if not session:
        # Revisar si el mensaje parece un NIT (solo digitos, 7-10 caracteres)
        posible_nit = mensaje.strip().replace(" ", "").replace(".", "").replace("-", "")
        if posible_nit.isdigit() and 7 <= len(posible_nit) <= 10:
            cliente = CLIENTES_POR_NIT.get(posible_nit)
            if cliente:
                usuarios_nit[telefono] = {
                    "nit": posible_nit,
                    "nombre": cliente["nombre"],
                    "ultimo_mensaje_timestamp": time.time(),
                }
                guardar_usuarios_nit(usuarios_nit)
                logger.info(f"NIT {posible_nit} asociado a {telefono} ({cliente['nombre']})")
                return (
                    f"Hola! Te identifique como *{cliente['nombre']}* 💚\n\n"
                    "Con mucho gusto te ayudo con tu informacion de energia. "
                    "Como te llamas para tratarte bien?"
                )
            else:
                logger.info(f"NIT {posible_nit} no encontrado (telefono {telefono})")
                return (
                    "Hola! Soy SofIA de Griin Energy 💚\n\n"
                    "No encontre ese NIT en nuestro sistema. "
                    "Por favor verificalo e intentalo de nuevo, o comunicate con tu asesor Griin."
                )
        else:
            return (
                "Hola! Soy *SofIA*, tu asistente de eficiencia energetica de Griin Energy 💚\n\n"
                "Para ayudarte con tu informacion de energia, necesito identificarte primero.\n"
                "Por favor escribe tu *NIT* (solo los numeros, sin puntos ni guiones)."
            )

    # Sesion activa: actualizar timestamp
    session["ultimo_mensaje_timestamp"] = time.time()
    guardar_usuarios_nit(usuarios_nit)

    if telefono not in conversaciones:
        conversaciones[telefono] = []

    historial = conversaciones[telefono]
    historial.append({"role": "user", "content": mensaje})

    system = get_system_prompt(telefono)

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=historial
    )

    respuesta = response.content[0].text
    historial.append({"role": "assistant", "content": respuesta})

    if len(historial) > MAX_MENSAJES_HISTORIAL:
        conversaciones[telefono] = historial[-MAX_MENSAJES_HISTORIAL:]

    return respuesta


# --- Helper: Leer factura PDF enviada por WhatsApp ---
def analizar_factura_pdf(media_url: str, telefono: str) -> str:
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]

    resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True)
    if resp.status_code != 200:
        raise ValueError(f"No pude descargar la factura: HTTP {resp.status_code}")

    # Extraer texto con pdfplumber (gratis, sin tokens de Claude)
    texto = ""
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages:
            texto += (page.extract_text() or "") + "\n"
    texto = texto.strip()

    if not texto:
        raise ValueError("No se pudo extraer texto del PDF")

    contexto_previo = datos_cliente.get(telefono, "")
    seccion_contexto = ""
    if contexto_previo:
        seccion_contexto = f"""
Ademas, tienes guardados los datos del mes anterior de este cliente:
--- DATOS MES ANTERIOR ---
{contexto_previo}
--- FIN DATOS ANTERIORES ---

Usa estos datos para comparar y dar una respuesta mas completa: menciona si el consumo subio o bajo vs el mes anterior, si el ahorro mejoro, etc.
"""

    prompt = f"""Eres SofIA, la asistente de eficiencia energetica de Griin Energy - colombiana, calida y experta.

Un cliente te mando su factura de energia. Aqui esta el texto extraido:

{texto}
{seccion_contexto}
Analiza esta factura y genera un mensaje de WhatsApp que:
1. Salude al cliente por nombre si lo encuentras
2. Resuma: operador, periodo, kWh consumidos, valor total en COP
3. Si es cliente pequenyo (menos de 10,000 kWh): usa lenguaje cotidiano
4. Si es cliente industrial grande (mas de 10,000 kWh): usa lenguaje tecnico pero cercano
5. Si tienes datos del mes anterior, compara el consumo y menciona la tendencia
6. De 1-2 tips utiles basados en la factura
7. Sea maximo 8 lineas
8. Use *negritas* para los numeros importantes
9. Use 1-2 emojis maximo
10. Firme como "SofIA 💚 · Griin Energy"

Solo devuelve el mensaje, sin explicaciones adicionales."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    respuesta = response.content[0].text

    if telefono not in conversaciones:
        conversaciones[telefono] = []
    conversaciones[telefono].append({"role": "user", "content": "[Cliente envio su factura de energia en PDF]"})
    conversaciones[telefono].append({"role": "assistant", "content": respuesta})

    return respuesta


# --- Helper: Analizar las 3 carpetas Drive ---
def analizar_tres_carpetas(
    factura_texto: str | None,
    griin_texto: str | None,
    generacion_texto: str | None,
    nombre_cliente: str,
) -> tuple[str, str]:

    secciones = []
    if factura_texto:
        secciones.append(f"=== FACTURA COMERCIALIZADORA ===\n{factura_texto}")
    if griin_texto:
        secciones.append(f"=== FACTURA GRIIN ENERGY ===\n{griin_texto}")
    if generacion_texto:
        secciones.append(f"=== INFORME GENERACION SOLAR ===\n{generacion_texto}")

    documentos = "\n\n".join(secciones)

    prompt = f"""Eres SofIA, la asistente de eficiencia energetica de Griin Energy - colombiana, calida y experta.

Tienes los documentos del cliente {nombre_cliente}. Analiza TODOS y haz DOS cosas:

===DATOS===
Extrae exactamente esto (si no esta en los docs escribe "No disponible"):
PERIODO: [mes y anyo]
COMERCIALIZADORA: [nombre operador]
KWH_CONSUMIDOS: [numero kWh facturados por la comercializadora - solo los que vinieron de la red]
AUTOCONSUMO_KWH: [kWh de autoconsumo solar - energia solar consumida directamente sin pasar por la red]
KWH_GENERADOS_SOLAR: [total kWh generados por el sistema solar]
INYECCION_RED: [kWh inyectados a la red si aparece]
COSTO_COMERCIALIZADORA: [valor total factura Air-e / comercializadora en COP]
COSTO_GRIIN: [valor total factura Griin Energy en COP]
TARIFA_KWH: [costo por kWh de la comercializadora en COP. Calculalo como COSTO_COMERCIALIZADORA / KWH_CONSUMIDOS si no aparece explicito]
COSTO_SIN_SOLAR: [lo que hubiera cobrado la comercializadora si NO existiera el sistema solar = (KWH_CONSUMIDOS + AUTOCONSUMO_KWH) x TARIFA_KWH. Si AUTOCONSUMO_KWH no esta disponible, usa solo KWH_CONSUMIDOS x TARIFA_KWH como aproximacion conservadora]
AHORRO_MES: [ahorro real en COP = COSTO_SIN_SOLAR - (COSTO_COMERCIALIZADORA + COSTO_GRIIN). Este es el verdadero beneficio del sistema solar]
NOTA: [cualquier dato relevante adicional]

===MENSAJE===
Genera un mensaje de WhatsApp que:
- Salude a {nombre_cliente} con calidez colombiana
- Muestre la factura de la comercializadora (kWh de red + costo)
- Muestre lo que cobro Griin
- Muestre el ahorro real: "Sin el sistema solar hubieras pagado X, con Griin pagaste Y (Air-e + Griin), ahorraste Z"
- Muestre la generacion solar del mes (kWh generados)
- De 1 tip util y cercano
- Invite a escribir si tienen preguntas
- Maximo 10 lineas, *negritas* para numeros, 1-2 emojis
- Firme: "SofIA 💚 · Griin Energy"

Usa EXACTAMENTE los separadores ===DATOS=== y ===MENSAJE=== en tu respuesta.

--- DOCUMENTOS ---
{documentos}"""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text

    if "===DATOS===" in raw and "===MENSAJE===" in raw:
        partes = raw.split("===MENSAJE===")
        datos = partes[0].replace("===DATOS===", "").strip()
        mensaje = partes[1].strip()
    else:
        datos = raw
        mensaje = raw

    return mensaje, datos


# --- Endpoints ---

@app.get("/")
def root():
    return {"status": "SofIA activa", "version": "0.2.0"}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/whatsapp", response_class=PlainTextResponse)
async def webhook_whatsapp(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
    NumMedia: int = Form(default=0),
    MediaUrl0: str = Form(default=""),
    MediaContentType0: str = Form(default=""),
):
    logger.info(f"Mensaje de {From} | Media: {NumMedia} | Body: {Body}")

    try:
        if NumMedia > 0 and "pdf" in MediaContentType0.lower():
            logger.info(f"PDF recibido: {MediaUrl0}")
            respuesta = analizar_factura_pdf(MediaUrl0, From)
        elif NumMedia > 0 and MediaContentType0.lower().startswith("image/"):
            respuesta = (
                "Hola! 👋 Vi que me mandaste una imagen de tu factura.\n\n"
                "Para analizarla mejor, me la puedes enviar en formato *PDF*? "
                "Asi puedo leer todos los datos con precision. 💚"
            )
        else:
            respuesta = generar_respuesta_chat(Body, From)

    except Exception as e:
        logger.error(f"Error procesando mensaje de {From}: {e}")
        respuesta = "Hola! Soy SofIA de Griin Energy 💚. En este momento tengo un problema tecnico - intentalo de nuevo en unos minutos."

    twiml = MessagingResponse()
    twiml.message(respuesta)
    return Response(content=str(twiml), media_type="application/xml")


@app.post("/enviar-resumen")
async def enviar_resumen(cliente: ClienteEnergia):
    logger.info(f"Enviando resumen a {cliente.nombre} ({cliente.telefono})")
    mensaje = generar_resumen_energia(cliente)
    result = twilio.messages.create(
        body=mensaje,
        from_=WHATSAPP_FROM,
        to=f"whatsapp:{cliente.telefono}",
    )
    logger.info(f"Mensaje enviado. SID: {result.sid}")
    return {
        "ok": True,
        "twilio_sid": result.sid,
        "estado": result.status,
        "mensaje_enviado": mensaje,
        "cliente": cliente.nombre,
    }


@app.post("/enviar-masivo")
async def enviar_masivo(payload: EnvioMasivoRequest):
    resultados = []
    errores = []
    for cliente in payload.clientes:
        try:
            resultado = await enviar_resumen(cliente)
            resultados.append(resultado)
        except Exception as e:
            logger.error(f"Error enviando a {cliente.nombre}: {e}")
            errores.append({"cliente": cliente.nombre, "error": str(e)})
    return {
        "ok": True,
        "enviados": len(resultados),
        "errores": len(errores),
        "detalle_errores": errores,
        "resultados": resultados,
    }


@app.post("/test-mensaje")
async def test_mensaje(telefono: str, nombre: str = "Cliente Test"):
    mensaje = (
        f"Hola {nombre}! 👋\n\n"
        "Este es un mensaje de prueba de *SofIA*, el asistente de eficiencia energetica de Griin Energy.\n\n"
        "Si recibes esto, todo esta funcionando correctamente! 💚\n\n"
        "_SofIA · Griin Energy_"
    )
    result = twilio.messages.create(
        body=mensaje,
        from_=WHATSAPP_FROM,
        to=f"whatsapp:{telefono}",
    )
    return {
        "ok": True,
        "twilio_sid": result.sid,
        "estado": result.status,
        "mensaje": mensaje,
    }


@app.get("/clientes")
def listar_clientes():
    return {
        "total": len(CLIENTES),
        "con_telefono": sum(1 for c in CLIENTES if c["telefono"]),
        "clientes": [
            {
                "nombre": c["nombre"],
                "telefono": c["telefono"] or "pendiente",
                "activo": c["telefono"] is not None,
            }
            for c in CLIENTES
        ],
    }


@app.post("/procesar-cliente/{nombre_cliente}")
async def procesar_cliente(nombre_cliente: str):
    cliente = CLIENTES_POR_NOMBRE.get(nombre_cliente.lower())
    if not cliente:
        raise HTTPException(status_code=404, detail=f"Cliente '{nombre_cliente}' no encontrado")
    if not cliente["telefono"]:
        raise HTTPException(status_code=400, detail=f"'{nombre_cliente}' no tiene telefono configurado")

    logger.info(f"Procesando cliente: {cliente['nombre']}")
    drive = get_drive_service()

    archivos_procesados = []
    factura_texto = griin_texto = generacion_texto = None

    # 1. Factura Comercializadora
    folder_id = get_subfolder_id(drive, cliente["folder_id"], CARPETA_FACTURA)
    if folder_id:
        archivo = get_latest_pdf(drive, folder_id)
        if archivo:
            factura_texto = download_as_text(drive, archivo)
            archivos_procesados.append(archivo["name"])
            logger.info(f"Factura Comercializadora: {archivo['name']}")

    # 2. Factura Griin
    folder_id = get_subfolder_id(drive, cliente["folder_id"], CARPETA_GRIIN)
    if folder_id:
        archivo = get_latest_pdf(drive, folder_id)
        if archivo:
            griin_texto = download_as_text(drive, archivo)
            archivos_procesados.append(archivo["name"])
            logger.info(f"Factura Griin: {archivo['name']}")

    # 3. Informe Generacion
    folder_id = get_subfolder_id(drive, cliente["folder_id"], CARPETA_GENERACION)
    if folder_id:
        archivo = get_latest_pdf(drive, folder_id)
        if archivo:
            generacion_texto = download_as_text(drive, archivo)
            archivos_procesados.append(archivo["name"])
            logger.info(f"Informe Generacion: {archivo['name']}")

    if not factura_texto and not griin_texto and not generacion_texto:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontro ningun documento para {cliente['nombre']}"
        )

    mensaje, datos = analizar_tres_carpetas(
        factura_texto, griin_texto, generacion_texto, cliente["nombre"]
    )

    # Guardar datos en archivo JSON (persiste entre requests)
    telefono = cliente["telefono"]
    datos_cliente[telefono] = datos
    guardar_datos_cliente(datos_cliente)

    if telefono not in conversaciones:
        conversaciones[telefono] = []
    conversaciones[telefono].append({
        "role": "assistant",
        "content": f"[Resumen mensual enviado a {cliente['nombre']}]\n{mensaje}"
    })
    logger.info(f"Datos de {cliente['nombre']} guardados en archivo JSON")

    result = twilio.messages.create(
        body=mensaje,
        from_=WHATSAPP_FROM,
        to=f"whatsapp:{telefono}",
    )
    logger.info(f"Mensaje enviado a {cliente['nombre']} ({telefono}) | SID: {result.sid}")

    return {
        "ok": True,
        "cliente": cliente["nombre"],
        "archivos_procesados": archivos_procesados,
        "telefono": telefono,
        "twilio_sid": result.sid,
        "estado": result.status,
        "mensaje_enviado": mensaje,
        "datos_memoria": datos,
    }


@app.post("/procesar-todos")
async def procesar_todos():
    clientes_activos = [c for c in CLIENTES if c["telefono"]]
    logger.info(f"Procesando {len(clientes_activos)} clientes activos")

    resultados = []
    errores = []

    for cliente in clientes_activos:
        try:
            resultado = await procesar_cliente(cliente["nombre"])
            resultados.append(resultado)
            logger.info(f"OK: {cliente['nombre']}")
        except Exception as e:
            logger.error(f"Error: {cliente['nombre']} - {e}")
            errores.append({"cliente": cliente["nombre"], "error": str(e)})

    return {
        "ok": True,
        "procesados": len(resultados),
        "errores": len(errores),
        "detalle_errores": errores,
        "resultados": resultados,
    }
