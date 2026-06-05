"""
SofIA â Asistente de Eficiencia EnergÃ©tica de Griin
Backend principal: FastAPI + Twilio WhatsApp + Claude API

Autor: Malik (Claude) para Farid Hadad / Griin Energy
"""

import os
import json
import base64
import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import PlainTextResponse, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional
import logging

from clientes import CLIENTES, CLIENTES_POR_NOMBRE
from drive_utils import (
    get_drive_service,
    get_subfolder_id,
    get_latest_pdf,
    download_as_base64,
    CARPETA_FACTURA,
    CARPETA_GRIIN,
    CARPETA_GENERACION,
)

load_dotenv()

# âââ Logging ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sofia")

# âââ Clientes ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

WHATSAPP_FROM = f"whatsapp:{os.environ['TWILIO_WHATSAPP_NUMBER']}"  # +19787966556

# âââ App âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
app = FastAPI(title="SofIA â Griin Energy", version="0.1.0")


# âââ Modelos âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
class ClienteEnergia(BaseModel):
    nombre: str          # Nombre del cliente / empresa
    telefono: str        # En formato +573XXXXXXXXX
    kwh_mes: float       # Consumo del mes actual en kWh
    kwh_mes_anterior: float  # Consumo del mes anterior en kWh
    costo_mes: float     # Costo total de la factura en COP
    mes: str             # Ej: "Mayo 2026"
    tarifa_kwh: Optional[float] = None  # $/kWh pagado


class EnvioMasivoRequest(BaseModel):
    clientes: list[ClienteEnergia]


# âââ Helper: Generar resumen con Claude ââââââââââââââââââââââââââââââââââââââ
def generar_resumen_energia(cliente: ClienteEnergia) -> str:
    """Usa Claude para generar un resumen amigable del consumo energÃ©tico."""

    variacion = cliente.kwh_mes - cliente.kwh_mes_anterior
    pct = (variacion / cliente.kwh_mes_anterior * 100) if cliente.kwh_mes_anterior > 0 else 0
    tendencia = "subiÃ³" if variacion > 0 else "bajÃ³"

    prompt = f"""Eres SofIA, la asistente de eficiencia energÃ©tica de Griin Energy. Eres una mujer colombiana muy cÃ¡lida, cercana y amigable â como la amiga que todos quisieran tener para entender temas de energÃ­a. Hablas como una colombiana real: usas expresiones como "Â¡QuÃ© buenas noticias!", "Â¡Eso es un logro!", "Â¡Vamos con todo!", "Â¡Uy, hay oportunidad aquÃ­!". Explicas las cosas de forma sencilla, como si le hablaras a alguien que no sabe nada de energÃ­a. Nunca eres frÃ­a ni corporativa â siempre cercana y positiva.

Genera un mensaje de WhatsApp para el cliente {cliente.nombre}.
El mensaje debe:
- Ser mÃ¡ximo 6 lÃ­neas
- Usar emojis con moderaciÃ³n (2-3 mÃ¡ximo), siempre alegres y apropiados
- Saludar con calidez colombiana
- Incluir el consumo del mes: {cliente.kwh_mes:,.0f} kWh
- Mencionar que {tendencia} un {abs(pct):.1f}% vs el mes anterior ({cliente.kwh_mes_anterior:,.0f} kWh)
- Si bajÃ³: celebrarlo como un logro personal del cliente
- Si subiÃ³: mencionarlo con tono positivo y motivador, sin regaÃ±ar
- Incluir el costo: ${cliente.costo_mes:,.0f} COP
- Terminar con una frase motivadora y cercana sobre el poder del ahorro energÃ©tico
- Usar *negritas* de WhatsApp solo para los nÃºmeros importantes
- Firmar como "SofIA ð Â· Griin Energy"

Solo devuelve el mensaje, sin explicaciones adicionales."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# âââ Memoria de conversaciones por nÃºmero de telÃ©fono ââââââââââââââââââââââââ
# { "+573001234567": [ {role, content}, ... ] }
conversaciones: dict[str, list] = {}
MAX_MENSAJES_HISTORIAL = 20

# âââ Memoria de datos energÃ©ticos por nÃºmero de telÃ©fono âââââââââââââââââââââ
# Guardamos el resumen de datos del cliente para que SofIA pueda responder preguntas
# { "+573001234567": "texto con datos del cliente" }
datos_cliente: dict[str, str] = {}

# Ãndice rÃ¡pido de telÃ©fono â cliente
TELEFONO_A_CLIENTE = {c["telefono"]: c for c in CLIENTES if c["telefono"]}

SYSTEM_PROMPT_SOFIA_BASE = """Eres SofIA, la asistente de eficiencia energÃ©tica de Griin Energy. Eres una mujer colombiana muy cÃ¡lida, cercana y amigable â como la amiga experta en energÃ­a que todos quisieran tener.

Tu personalidad:
- Hablas como colombiana real: usas expresiones como "Â¡Claro que sÃ­!", "Â¡Uy, quÃ© buena pregunta!", "Â¡Vamos con todo!", "Â¡Con mucho gusto!"
- Explicas los temas tÃ©cnicos de energÃ­a de forma sencilla, con ejemplos de la vida cotidiana
- Eres positiva y motivadora, nunca regaÃ±as ni eres frÃ­a
- Usas 1-2 emojis por mensaje, no mÃ¡s â natural, no forzado
- Recuerdas lo que el usuario te ha contado en la conversaciÃ³n y lo usas naturalmente

Tu conocimiento:
- Eres experta en consumo energÃ©tico empresarial en Colombia
- Sabes sobre facturas de energÃ­a, kWh, tarifas, costo unitario (CU), operadores de red
- Conoces estrategias de ahorro energÃ©tico para empresas
- Sabes sobre energÃ­as renovables, paneles solares, eficiencia
- Griin Energy es tu empresa: instala paneles solares y ayuda a empresas colombianas a reducir su factura de energÃ­a

Reglas del formato:
- Respuestas cortas y directas: mÃ¡ximo 5-6 lÃ­neas
- Usa *negritas* de WhatsApp solo para tÃ©rminos clave
- NUNCA mandes a nadie a un correo ni a otro canal â todo se resuelve aquÃ­ en WhatsApp
- NUNCA digas que el cliente recibirÃ¡ el resumen pronto â ya lo tienes, Ãºsalo para responder{datos_seccion}"""


def get_system_prompt(telefono: str) -> str:
    """Construye el system prompt con datos del cliente si estÃ¡n disponibles."""
    datos = datos_cliente.get(telefono, "")
    if datos:
        datos_seccion = f"""

ââââââââââââââââââââââââââââââââââ
DATOS REALES DEL CLIENTE (Ãºltimo mes):
{datos}
ââââââââââââââââââââââââââââââââââ

Con estos datos puedes responder EXACTAMENTE preguntas como:
- Â¿CuÃ¡nto consumÃ­? â usa los kWh del informe de generaciÃ³n solar
- Â¿CuÃ¡nto ahorrÃ©? â usa la diferencia entre la factura comercializadora y la factura Griin
- Â¿CuÃ¡nto me cobrÃ³ la comercializadora? â dato directo
- Â¿CuÃ¡nto me cobrÃ³ Griin? â dato directo
Responde SIEMPRE con los nÃºmeros reales. Nunca digas que no tienes la informaciÃ³n."""
    else:
        datos_seccion = ""
    return SYSTEM_PROMPT_SOFIA_BASE.replace("{datos_seccion}", datos_seccion)


# âââ Helper: Respuesta inteligente al chat (con memoria) âââââââââââââââââââââ
def generar_respuesta_chat(mensaje: str, telefono: str) -> str:
    """Usa Claude para responder cualquier mensaje de WhatsApp como SofIA.
    Guarda y usa el historial + datos energÃ©ticos del cliente."""

    if telefono not in conversaciones:
        conversaciones[telefono] = []

    historial = conversaciones[telefono]
    historial.append({"role": "user", "content": mensaje})

    # System prompt con datos del cliente inyectados si existen
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


# âââ Helper: Leer factura PDF y generar anÃ¡lisis âââââââââââââââââââââââââââââ
def analizar_factura_pdf(media_url: str, telefono: str) -> str:
    """Descarga el PDF de la factura desde Twilio y pide a Claude que lo analice."""

    # Descargar el PDF usando las credenciales de Twilio (requiere auth bÃ¡sica)
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token  = os.environ["TWILIO_AUTH_TOKEN"]

    resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True)
    if resp.status_code != 200:
        raise ValueError(f"No pude descargar la factura: HTTP {resp.status_code}")

    pdf_b64 = base64.standard_b64encode(resp.content).decode("utf-8")

    prompt = """Eres SofIA, la asistente de eficiencia energÃ©tica de Griin Energy â colombiana, cÃ¡lida y experta.

Un cliente acaba de mandarte su factura de energÃ­a. AnalÃ­zala y responde con un mensaje de WhatsApp que:

1. Extraiga estos datos clave del PDF:
   - Nombre del cliente o empresa
   - Operador (Enel, Air-e, Vatia, EPM, Afinia, etc.)
   - PerÃ­odo facturado
   - kWh consumidos (energÃ­a activa)
   - Valor a pagar en COP

2. Luego genera un mensaje amigable que:
   - Salude por el nombre si lo encontraste
   - Resuma los datos clave en lenguaje sencillo
   - Si es cliente pequeÃ±o (< 10,000 kWh): usa lenguaje cotidiano, compara con bombillos o neveras
   - Si es cliente industrial grande (> 10,000 kWh): usa lenguaje mÃ¡s tÃ©cnico pero igual de cercano
   - DÃ© 1-2 observaciones o tips Ãºtiles basados en lo que ves en la factura
   - Sea mÃ¡ximo 8 lÃ­neas
   - Use *negritas* de WhatsApp para los nÃºmeros importantes
   - Use 1-2 emojis mÃ¡ximo
   - Firme como "SofIA ð Â· Griin Energy"

Solo devuelve el mensaje, sin explicaciones adicionales."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",  # Sonnet: misma calidad para PDFs, 5x mÃ¡s barato que Opus
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {
                    "type": "text",
                    "text": prompt
                }
            ],
        }]
    )

    respuesta = response.content[0].text

    # Guardar en historial para que SofIA recuerde el contexto
    if telefono not in conversaciones:
        conversaciones[telefono] = []
    conversaciones[telefono].append({"role": "user", "content": "[Cliente enviÃ³ su factura de energÃ­a en PDF]"})
    conversaciones[telefono].append({"role": "assistant", "content": respuesta})

    return respuesta


# âââ Endpoints âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.get("/")
def root():
    return {"status": "SofIA activa â", "version": "0.1.0"}


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
    """
    Webhook que recibe mensajes entrantes de WhatsApp via Twilio.
    Detecta si viene un PDF (factura) o un mensaje de texto normal.
    """
    logger.info(f"Mensaje de {From} | Media: {NumMedia} | Body: {Body}")

    try:
        # ââ Â¿Viene un PDF? âââââââââââââââââââââââââââââââââââââââââââââââââââ
        if NumMedia > 0 and "pdf" in MediaContentType0.lower():
            logger.info(f"PDF recibido: {MediaUrl0}")
            respuesta = analizar_factura_pdf(MediaUrl0, From)

        # ââ Â¿Viene una imagen (foto de la factura)? ââââââââââââââââââââââââââ
        elif NumMedia > 0 and MediaContentType0.lower().startswith("image/"):
            respuesta = (
                "Â¡Hola! ð Vi que me mandaste una imagen de tu factura.\n\n"
                "Para analizarla mejor, Â¿me la puedes enviar en formato *PDF*? "
                "AsÃ­ puedo leer todos los datos con precisiÃ³n. ð"
            )

        # ââ Mensaje de texto normal ââââââââââââââââââââââââââââââââââââââââââ
        else:
            respuesta = generar_respuesta_chat(Body, From)

    except Exception as e:
        logger.error(f"Error procesando mensaje de {From}: {e}")
        respuesta = "Â¡Hola! Soy SofIA de Griin Energy ð. En este momento tengo un problema tÃ©cnico â intÃ©ntalo de nuevo en unos minutos."

    twiml = MessagingResponse()
    twiml.message(respuesta)
    return Response(content=str(twiml), media_type="application/xml")


@app.post("/enviar-resumen")
async def enviar_resumen(cliente: ClienteEnergia):
    """
    EnvÃ­a el resumen energÃ©tico mensual a UN cliente.
    Usa Claude para generar el mensaje personalizado.
    """
    logger.info(f"Enviando resumen a {cliente.nombre} ({cliente.telefono})")

    mensaje = generar_resumen_energia(cliente)

    result = twilio.messages.create(
        body=mensaje,
        from_=WHATSAPP_FROM,
        to=f"whatsapp:{cliente.telefono}",
    )

    logger.info(f"Mensaje enviado. SID: {result.sid} | Estado: {result.status}")

    return {
        "ok": True,
        "twilio_sid": result.sid,
        "estado": result.status,
        "mensaje_enviado": mensaje,
        "cliente": cliente.nombre,
    }


@app.post("/enviar-masivo")
async def enviar_masivo(payload: EnvioMasivoRequest):
    """
    EnvÃ­a resÃºmenes energÃ©ticos a TODOS los clientes del mes.
    Endpoint principal para el flujo mensual de Griin (17 clientes).
    """
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
    """
    EnvÃ­a un mensaje de prueba para verificar que WhatsApp funciona.
    Uso: POST /test-mensaje?telefono=+573XXXXXXXXX&nombre=Farid
    """
    mensaje = (
        f"Â¡Hola {nombre}! ð\n\n"
        "Este es un mensaje de prueba de *SofIA*, el asistente de eficiencia energÃ©tica de Griin Energy.\n\n"
        "Si recibes esto, Â¡todo estÃ¡ funcionando correctamente! â\n\n"
        "_SofIA Â· Griin Energy_"
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


# âââ Helper: Analizar las 3 carpetas Drive y generar resumen completo âââââââââ
def analizar_tres_carpetas(
    factura_b64: str | None,
    griin_b64: str | None,
    generacion_b64: str | None,
    nombre_cliente: str,
) -> tuple[str, str]:
    """
    Lee las 3 fuentes de datos del cliente y genera:
    1. Un mensaje de WhatsApp para enviar al cliente
    2. Un bloque de datos estructurados para guardar en memoria (datos_cliente)

    Retorna (mensaje_whatsapp, datos_para_memoria)
    """
    # Construir el contenido del mensaje con los PDFs disponibles
    content = []

    if factura_b64:
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": factura_b64},
            "title": "Factura Comercializadora (Air-e, Enel, EPM, etc.)",
        })
    if griin_b64:
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": griin_b64},
            "title": "Factura Griin Energy",
        })
    if generacion_b64:
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": generacion_b64},
            "title": "Informe de GeneraciÃ³n Solar",
        })

    prompt = f"""Eres SofIA, la asistente de eficiencia energÃ©tica de Griin Energy â colombiana, cÃ¡lida y experta.

Tienes {len(content)} documento(s) del cliente *{nombre_cliente}*. Analiza TODOS y haz DOS cosas:

"ââââââââââââââââââââââââââââââââââââ
PARTE 1 â DATOS ESTRUCTURADOS (para memoria interna)
âââââââââââââââââââââââââââââââââââââ
Extrae exactamente esto (sin inventar, si no estÃ¡ en los docs escribe "No disponible"):

PERIODO: [mes y aÃ±o]
COMERCIALIZADORA: [nombre operador]
KWH_CONSUMIDOS: [nÃºmero kWh facturados por la comercializadora]
COSTO_COMERCIALIZADORA: [valor total en COP]
COSTO_GRIIN: [valor total factura Griin en COP]
AHORRO_MES: [diferencia entre comercializadora y Griin en COP â si Griin es menor, el ahorro es positivo]
KWH_GENERADOS_SOLAR: [kWh generados por el sistema solar segÃºn informe]
AUTOCONSUMO_KWH: [kWh de autoconsumo solar si aparece]
INYECCION_RED: [kWh inyectados a la red si aparece]
NOTA: [cualquier dato relevante adicional]

âââââââââââââââââââââââââââââââââââââ
PARTE 2 â MENSAJE WHATSAPP PARA EL CLIENTE
âââââââââââââââââââââââââââââââââââââ
Genera el mensaje asÃ­:
- Saluda a {nombre_cliente} con calidez colombiana
- Muestra la factura de la comercializadora (kWh + costo)
- Muestra lo que cobrÃ³ Griin
- Calcula y celebra el ahorro real en COP
- Muestra la generaciÃ³n solar del mes (kWh generados)
- Si el consumo bajÃ³ vs anterior: celÃ©bralo
- Da 1 tip Ãºtil y cercano
- Invita a escribir si tienen preguntas
- MÃ¡ximo 10 lÃ­neas, *negritas* para nÃºmeros, 1-2 emojis
- Firma: "SofIA ð Â· Griin Energy"

Formato de respuesta â EXACTAMENTE asÃ­, con los separadores:
===DATOS===
[datos estructurados de la PARTE 1]
===MENSAJE===
[mensaje de WhatsApp de la PARTE 2]"""

    content.append({"type": "text", "text": prompt})

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": content}]
    )

    raw = response.content[0].text

    # Separar datos del mensaje
    if "===DATOS===" in raw and "===MENSAJE===" in raw:
        partes = raw.split("===MENSAJE===")
        datos = partes[0].replace("===DATOS===", "").strip()
        mensaje = partes[1].strip()
    else:
        # Fallback si Claude no siguiÃ³ el formato
        datos = raw
        mensaje = raw

    return mensaje, datos


# âââ Flujo B: Endpoints Drive â WhatsApp âââââââââââââââââââââââââââââââââââââ

@app.get("/clientes")
def listar_clientes():
    """Lista todos los clientes y si tienen nÃºmero configurado."""
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
    """
    Lee las 3 carpetas del cliente en Drive (Factura Comercializadora +
    Factura Griin + Informe GeneraciÃ³n), genera el resumen completo con
    Claude y lo envÃ­a por WhatsApp. TambiÃ©n guarda los datos en memoria
    para que SofIA pueda responder preguntas despuÃ©s.

    Uso: POST /procesar-cliente/Ferreflex
    """
    cliente = CLIENTES_POR_NOMBRE.get(nombre_cliente.lower())
    if not cliente:
        raise HTTPException(status_code=404, detail=f"Cliente '{nombre_cliente}' no encontrado")

    if not cliente["telefono"]:
        raise HTTPException(status_code=400, detail=f"'{nombre_cliente}' no tiene telÃ©fono configurado")

    logger.info(f"Procesando cliente: {cliente['nombre']}")
    drive = get_drive_service()

    archivos_procesados = []
    factura_b64 = griin_b64 = generacion_b64 = None

    # ââ 1. Factura Comercializadora ââââââââââââââââââââââââââââââââââââââââââ
    folder_id = get_subfolder_id(drive, cliente["folder_id"], CARPETA_FACTURA)
    if folder_id:
        archivo = get_latest_pdf(drive, folder_id)
        if archivo:
            factura_b64, _ = download_as_base64(drive, archivo)
            archivos_procesados.append(archivo["name"])
            logger.info(f"â Factura Comercializadora: {archivo['name']}")
        else:
            logger.warning(f"â ï¸ Sin PDFs en '{CARPETA_FACTURA}' para {cliente['nombre']}")
    else:
        logger.warning(f"â ï¸ Carpeta '{CARPETA_FACTURA}' no encontrada para {cliente['nombre']}")

    # ââ 2. Factura Griin âââââââââââââââââââââââââââââââââââââââââââââââââââââ
    folder_id = get_subfolder_id(drive, cliente["folder_id"], CARPETA_GRIIN)
    if folder_id:
        archivo = get_latest_pdf(drive, folder_id)
        if archivo:
            griin_b64, _ = download_as_base64(drive, archivo)
            archivos_procesados.append(archivo["name"])
            logger.info(f"â Factura Griin: {archivo['name']}")
        else:
            logger.warning(f"â ï¸ Sin PDFs en '{CARPETA_GRIIN}' para {cliente['nombre']}")
    else:
        logger.warning(f"â ï¸ Carpeta '{CARPETA_GRIIN}' no encontrada para {cliente['nombre']}")

    # ââ 3. Informe GeneraciÃ³n ââââââââââââââââââââââââââââââââââââââââââââââââ
    folder_id = get_subfolder_id(drive, cliente["folder_id"], CARPETA_GENERACION)
    if folder_id:
        archivo = get_latest_pdf(drive, folder_id)
        if archivo:
            generacion_b64, _ = download_as_base64(drive, archivo)
            archivos_procesados.append(archivo["name"])
            logger.info(f"â Informe GeneraciÃ³n: {archivo['name']}")
        else:
            logger.warning(f"â ï¸ Sin PDFs en '{CARPETA_GENERACION}' para {cliente['nombre']}")
    else:
        logger.warning(f"â ï¸ Carpeta '{CARPETA_GENERACION}' no encontrada para {cliente['nombre']}")

    if not factura_b64 and not griin_b64 and not generacion_b64:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontrÃ³ ningÃºn documento para {cliente['nombre']}"
        )

    # ââ Generar mensaje con Claude (3 documentos) ââââââââââââââââââââââââââââ
    mensaje, datos = analizar_tres_carpetas(
        factura_b64, griin_b64, generacion_b64, cliente["nombre"]
    )

    # ââ Guardar datos en memoria para responder preguntas del cliente ââââââââ
    telefono = cliente["telefono"]
    datos_cliente[telefono] = datos
    # TambiÃ©n inicializar/limpiar el historial de conversaciÃ³n con contexto fresco
    if telefono not in conversaciones:
        conversaciones[telefono] = []
    # AÃ±adir el resumen al historial para que haya contexto inmediato
    conversaciones[telefono].append({
        "role": "assistant",
        "content": f"[Resumen mensual enviado a {cliente['nombre']}]\n{mensaje}"
    })
    logger.info(f"ð¾ Datos de {cliente['nombre']} guardados en memoria para respuestas de chat")

    # ââ Enviar por WhatsApp ââââââââââââââââââââââââââââââââââââââââââââââââââ
    result = twilio.messages.create(
        body=mensaje,
        from_=WHATSAPP_FROM,
        to=f"whatsapp:{telefono}",
    )

    logger.info(f"ð¤ Mensaje enviado a {cliente['nombre']} ({telefono}) | SID: {result.sid}")

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
    """
    Procesa todos los clientes que tienen telÃ©fono configurado.
    Lee su factura mÃ¡s reciente de Drive y envÃ­a el resumen por WhatsApp.

    Uso: POST /procesar-todos
    """
    clientes_activos = [c for c in CLIENTES if c["telefono"]]
    logger.info(f"Procesando {len(clientes_activos)} clientes activos")

    resultados = []
    errores = []

    for cliente in clientes_activos:
        try:
            resultado = await procesar_cliente(cliente["nombre"])
            resultados.append(resultado)
            logger.info(f"â {cliente['nombre']} â OK")
        except Exception as e:
            logger.error(f"â {cliente['nombre']} â Error: {e}")
            errores.append({"cliente": cliente["nombre"], "error": str(e)})

    return {
        "ok": True,
        "procesados": len(resultados),
        "errores": len(errores),
        "detalle_errores": errores,
        "resultados": resultados,
    }
