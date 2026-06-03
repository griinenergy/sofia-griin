"""
SofIA — Asistente de Eficiencia Energética de Griin
Backend principal: FastAPI + Twilio WhatsApp + Claude API

Autor: Malik (Claude) para Farid Hadad / Griin Energy
"""

import os
import json
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
):
    """
    Webhook que recibe mensajes entrantes de WhatsApp via Twilio.
    Responde automáticamente con SofIA.
    """
    logger.info(f"Mensaje recibido de {From}: {Body}")

    # Nota: validación de firma desactivada para Sandbox (se reactiva en producción)
    # En producción con número propio, descomentar esto:
    # validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    # signature = request.headers.get("X-Twilio-Signature", "")
    # url = str(request.url)
    # form_data = dict(await request.form())
    # if not validator.validate(url, form_data, signature):
    #     raise HTTPException(status_code=403, detail="Firma inválida")

    mensaje_lower = Body.lower().strip()

    # Respuesta básica a mensajes entrantes
    if any(word in mensaje_lower for word in ["hola", "hello", "hi", "buenas", "buenos", "quiubo", "info", "ayuda", "help"]):
        respuesta = (
            "¡Hola, hola! 👋 ¡Qué bueno que escribiste!\n\n"
            "Soy *SofIA*, tu amiga de energía de *Griin Energy* 💚\n\n"
            "Estoy aquí para contarte cómo va el consumo eléctrico de tu empresa cada mes — "
            "de forma sencilla, sin enredos y con todo el cariño del mundo.\n\n"
            "Si tienes preguntas sobre tu factura o quieres saber cómo ahorrar, "
            "escríbenos a info@griin.com.co 📧 ¡Con gusto te ayudamos!"
        )
    elif any(word in mensaje_lower for word in ["gracias", "thank", "genial", "excelente", "perfecto"]):
        respuesta = (
            "¡Ay, qué alegría leer eso! 😄💚\n\n"
            "Para eso estamos, para ayudarte a entender tu energía y ahorrar. "
            "Cualquier cosa que necesites, aquí estoy. ¡Hasta pronto!"
        )
    else:
        respuesta = (
            "¡Hola! 😊 Soy *SofIA* de Griin Energy.\n\n"
            "Recibo mensajes automáticos del consumo energético de tu empresa, "
            "pero si tienes una pregunta o necesitas algo, "
            "escríbele a nuestro equipo a info@griin.com.co — ¡ellos te atienden con todo el gusto! 💚"
        )

    # Responder con TwiML — Twilio se encarga de enviar la respuesta
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
