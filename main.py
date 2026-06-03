"""
SofIA — Asistente de Eficiencia Energética de Griin
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

load_dotenv()

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sofia")

# ─── Clientes ────────────────────────────────────────────────────────────────
twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

WHATSAPP_FROM = f"whatsapp:{os.environ['TWILIO_WHATSAPP_NUMBER']}"  # +19787966556

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="SofIA — Griin Energy", version="0.1.0")


# ─── Modelos ─────────────────────────────────────────────────────────────────
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


# ─── Helper: Generar resumen con Claude ──────────────────────────────────────
def generar_resumen_energia(cliente: ClienteEnergia) -> str:
    """Usa Claude para generar un resumen amigable del consumo energético."""

    variacion = cliente.kwh_mes - cliente.kwh_mes_anterior
    pct = (variacion / cliente.kwh_mes_anterior * 100) if cliente.kwh_mes_anterior > 0 else 0
    tendencia = "subió" if variacion > 0 else "bajó"

    prompt = f"""Eres SofIA, la asistente de eficiencia energética de Griin Energy. Eres una mujer colombiana muy cálida, cercana y amigable — como la amiga que todos quisieran tener para entender temas de energía. Hablas como una colombiana real: usas expresiones como "¡Qué buenas noticias!", "¡Eso es un logro!", "¡Vamos con todo!", "¡Uy, hay oportunidad aquí!". Explicas las cosas de forma sencilla, como si le hablaras a alguien que no sabe nada de energía. Nunca eres fría ni corporativa — siempre cercana y positiva.

Genera un mensaje de WhatsApp para el cliente {cliente.nombre}.
El mensaje debe:
- Ser máximo 6 líneas
- Usar emojis con moderación (2-3 máximo), siempre alegres y apropiados
- Saludar con calidez colombiana
- Incluir el consumo del mes: {cliente.kwh_mes:,.0f} kWh
- Mencionar que {tendencia} un {abs(pct):.1f}% vs el mes anterior ({cliente.kwh_mes_anterior:,.0f} kWh)
- Si bajó: celebrarlo como un logro personal del cliente
- Si subió: mencionarlo con tono positivo y motivador, sin regañar
- Incluir el costo: ${cliente.costo_mes:,.0f} COP
- Terminar con una frase motivadora y cercana sobre el poder del ahorro energético
- Usar *negritas* de WhatsApp solo para los números importantes
- Firmar como "SofIA 💚 · Griin Energy"

Solo devuelve el mensaje, sin explicaciones adicionales."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# ─── Memoria de conversaciones por número de teléfono ────────────────────────
# Diccionario en memoria: { "+573001234567": [ {role, content}, ... ] }
# Se reinicia si el servidor se reinicia — suficiente para el MVP
conversaciones: dict[str, list] = {}
MAX_MENSAJES_HISTORIAL = 20  # Últimos 20 mensajes (10 intercambios)

SYSTEM_PROMPT_SOFIA = """Eres SofIA, la asistente de eficiencia energética de Griin Energy. Eres una mujer colombiana muy cálida, cercana y amigable — como la amiga experta en energía que todos quisieran tener.

Tu personalidad:
- Hablas como colombiana real: usas expresiones como "¡Claro que sí!", "¡Uy, qué buena pregunta!", "¡Vamos con todo!", "¡Con mucho gusto!"
- Explicas los temas técnicos de energía de forma sencilla, con ejemplos de la vida cotidiana
- Eres positiva y motivadora, nunca regañas ni eres fría
- Usas 1-2 emojis por mensaje, no más — natural, no forzado
- Recuerdas lo que el usuario te ha contado en la conversación y lo usas naturalmente

Tu conocimiento:
- Eres experta en consumo energético empresarial en Colombia
- Sabes sobre facturas de energía, kWh, tarifas, costo unitario (CU), operadores de red
- Conoces estrategias de ahorro energético para empresas
- Sabes sobre energías renovables, paneles solares, eficiencia
- Griin Energy es tu empresa: ayuda a empresas colombianas a entender y optimizar su consumo eléctrico

Reglas del formato:
- Respuestas cortas y directas: máximo 5-6 líneas
- Usa *negritas* de WhatsApp solo para términos clave
- NUNCA mandes a nadie a un correo ni a otro canal — todo se resuelve aquí en WhatsApp
- Si no sabes algo específico del cliente (su factura, su consumo), dile que pronto recibirá su resumen mensual de Griin"""


# ─── Helper: Respuesta inteligente al chat (con memoria) ─────────────────────
def generar_respuesta_chat(mensaje: str, telefono: str) -> str:
    """Usa Claude para responder cualquier mensaje de WhatsApp como SofIA.
    Guarda y usa el historial de la conversación por número de teléfono."""

    # Obtener o crear historial para este número
    if telefono not in conversaciones:
        conversaciones[telefono] = []

    historial = conversaciones[telefono]

    # Agregar el nuevo mensaje del usuario al historial
    historial.append({"role": "user", "content": mensaje})

    # Llamar a Claude con todo el historial
    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT_SOFIA,
        messages=historial
    )

    respuesta = response.content[0].text

    # Guardar la respuesta de SofIA en el historial
    historial.append({"role": "assistant", "content": respuesta})

    # Mantener solo los últimos N mensajes para no crecer infinito
    if len(historial) > MAX_MENSAJES_HISTORIAL:
        conversaciones[telefono] = historial[-MAX_MENSAJES_HISTORIAL:]

    return respuesta


# ─── Helper: Leer factura PDF y generar análisis ─────────────────────────────
def analizar_factura_pdf(media_url: str, telefono: str) -> str:
    """Descarga el PDF de la factura desde Twilio y pide a Claude que lo analice."""

    # Descargar el PDF usando las credenciales de Twilio (requiere auth básica)
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token  = os.environ["TWILIO_AUTH_TOKEN"]

    resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True)
    if resp.status_code != 200:
        raise ValueError(f"No pude descargar la factura: HTTP {resp.status_code}")

    pdf_b64 = base64.standard_b64encode(resp.content).decode("utf-8")

    prompt = """Eres SofIA, la asistente de eficiencia energética de Griin Energy — colombiana, cálida y experta.

Un cliente acaba de mandarte su factura de energía. Analízala y responde con un mensaje de WhatsApp que:

1. Extraiga estos datos clave del PDF:
   - Nombre del cliente o empresa
   - Operador (Enel, Air-e, Vatia, EPM, Afinia, etc.)
   - Período facturado
   - kWh consumidos (energía activa)
   - Valor total a pagar en COP

2. Luego genera un mensaje amigable que:
   - Salude por el nombre si lo encontraste
   - Resuma los datos clave en lenguaje sencillo
   - Si es cliente pequeño (< 10,000 kWh): usa lenguaje cotidiano, compara con bombillos o neveras
   - Si es cliente industrial grande (> 10,000 kWh): usa lenguaje más técnico pero igual de cercano
   - Dé 1-2 observaciones o tips útiles basados en lo que ves en la factura
   - Sea máximo 8 líneas
   - Use *negritas* de WhatsApp para los números importantes
   - Use 1-2 emojis máximo
   - Firme como "SofIA 💚 · Griin Energy"

Solo devuelve el mensaje, sin explicaciones adicionales."""

    response = claude.messages.create(
        model="claude-opus-4-6",  # Usamos Opus para lectura de PDFs — más preciso
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
    conversaciones[telefono].append({"role": "user", "content": "[Cliente envió su factura de energía en PDF]"})
    conversaciones[telefono].append({"role": "assistant", "content": respuesta})

    return respuesta


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "SofIA activa ✅", "version": "0.1.0"}


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
        # ── ¿Viene un PDF? ───────────────────────────────────────────────────
        if NumMedia > 0 and "pdf" in MediaContentType0.lower():
            logger.info(f"PDF recibido: {MediaUrl0}")
            respuesta = analizar_factura_pdf(MediaUrl0, From)

        # ── ¿Viene una imagen (foto de la factura)? ──────────────────────────
        elif NumMedia > 0 and MediaContentType0.lower().startswith("image/"):
            respuesta = (
                "¡Hola! 👋 Vi que me mandaste una imagen de tu factura.\n\n"
                "Para analizarla mejor, ¿me la puedes enviar en formato *PDF*? "
                "Así puedo leer todos los datos con precisión. 💚"
            )

        # ── Mensaje de texto normal ──────────────────────────────────────────
        else:
            respuesta = generar_respuesta_chat(Body, From)

    except Exception as e:
        logger.error(f"Error procesando mensaje de {From}: {e}")
        respuesta = "¡Hola! Soy SofIA de Griin Energy 💚. En este momento tengo un problema técnico — inténtalo de nuevo en unos minutos."

    twiml = MessagingResponse()
    twiml.message(respuesta)
    return Response(content=str(twiml), media_type="application/xml")


@app.post("/enviar-resumen")
async def enviar_resumen(cliente: ClienteEnergia):
    """
    Envía el resumen energético mensual a UN cliente.
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
    Envía resúmenes energéticos a TODOS los clientes del mes.
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
    Envía un mensaje de prueba para verificar que WhatsApp funciona.
    Uso: POST /test-mensaje?telefono=+573XXXXXXXXX&nombre=Farid
    """
    mensaje = (
        f"¡Hola {nombre}! 👋\n\n"
        "Este es un mensaje de prueba de *SofIA*, el asistente de eficiencia energética de Griin Energy.\n\n"
        "Si recibes esto, ¡todo está funcionando correctamente! ✅\n\n"
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
