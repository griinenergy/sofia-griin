"""
SofIA — Asistente de Eficiencia Energética de Griin
Backend principal: FastAPI + Twilio WhatsApp + Claude API + Supabase
"""

import os
import re
import json
import base64
import httpx
from fastapi import FastAPI, Request, Form, HTTPException, Header
from fastapi.responses import PlainTextResponse, Response
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional
import logging
from supabase import create_client, Client as SupabaseClient

from clientes import CLIENTES, CLIENTES_POR_NOMBRE, CLIENTES_POR_NIT
from drive_utils import (
    get_drive_service,
    get_subfolder_id,
    get_all_files,
    download_as_text,
    CARPETA_FACTURA,
    CARPETA_GRIIN,
    CARPETA_GENERACION,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sofia")

# ─── Clientes externos ───────────────────────────────────────────────────────
twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase: SupabaseClient = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"],
)

WHATSAPP_FROM = f"whatsapp:{os.environ['TWILIO_WHATSAPP_NUMBER']}"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

app = FastAPI(title="SofIA — Griin Energy", version="0.2.0")


# ─── Modelos ─────────────────────────────────────────────────────────────────
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


# ─── Memoria en RAM (historial de conversación) ───────────────────────────────
conversaciones: dict[str, list] = {}
MAX_MENSAJES_HISTORIAL = 20

# ─── Sesiones NIT ─────────────────────────────────────────────────────────────
# { telefono: {nit, nombre, timestamp} }
ARCHIVO_SESIONES = "/tmp/usuarios_nit.json"

def cargar_sesiones() -> dict:
    try:
        with open(ARCHIVO_SESIONES) as f:
            return json.load(f)
    except Exception:
        return {}

def guardar_sesiones(sesiones: dict):
    try:
        with open(ARCHIVO_SESIONES, "w") as f:
            json.dump(sesiones, f)
    except Exception as e:
        logger.warning(f"No se pudo guardar sesiones: {e}")

import time
SESION_EXPIRY = 24 * 3600  # 24 horas


# ─── Supabase: guardar y leer documentos ─────────────────────────────────────
MESES_ES = {
    "ene": "01", "feb": "02", "mar": "03", "abr": "04",
    "may": "05", "jun": "06", "jul": "07", "ago": "08",
    "sep": "09", "oct": "10", "nov": "11", "dic": "12",
}

def extraer_fecha_documento(texto: str) -> str | None:
    """
    Extrae la fecha de fin del período desde 'Mes reportado: Nov 28 - Dic 25 2025'.
    Retorna formato 'YYYY-MM' (ej. '2025-12') para ordenamiento correcto.
    Retorna None si no encuentra el patrón.
    """
    match = re.search(
        r"Mes reportado:.*?-\s*(\w{3})\s+\d+\s+(\d{4})",
        texto,
        re.IGNORECASE,
    )
    if match:
        mes_str = match.group(1).lower()[:3]
        año    = match.group(2)
        mes_num = MESES_ES.get(mes_str)
        if mes_num:
            return f"{año}-{mes_num}"
    return None


def guardar_documento_supabase(
    folder_id: str,
    nombre_cliente: str,
    carpeta: str,
    archivo_nombre: str,
    contenido_texto: str,
    drive_file_id: str,
):
    """Guarda o actualiza un documento en Supabase. Usa drive_file_id como clave única."""
    try:
        supabase.table("documentos_energia").upsert({
            "folder_id": folder_id,
            "nombre_cliente": nombre_cliente,
            "carpeta": carpeta,
            "archivo_nombre": archivo_nombre,
            "contenido_texto": contenido_texto,
            "drive_file_id": drive_file_id,
            "fecha_documento": extraer_fecha_documento(contenido_texto),
        }, on_conflict="drive_file_id").execute()
        logger.info(f"✅ Supabase: guardado '{archivo_nombre}'")
    except Exception as e:
        logger.error(f"❌ Supabase error al guardar '{archivo_nombre}': {e}")


def obtener_contexto_cliente(folder_id: str) -> str:
    """
    Lee TODOS los documentos del cliente desde Supabase.
    Retorna un texto consolidado para usar como contexto en el chat.
    """
    try:
        resp = supabase.table("documentos_energia")\
            .select("carpeta, archivo_nombre, contenido_texto")\
            .eq("folder_id", folder_id)\
            .order("carpeta")\
            .execute()

        docs = resp.data
        if not docs:
            return ""

        secciones = []
        for doc in docs:
            if doc.get("contenido_texto"):
                secciones.append(
                    f"--- {doc['carpeta']} | {doc['archivo_nombre']} ---\n"
                    f"{doc['contenido_texto'][:3000]}"  # máx 3000 chars por doc para no explotar tokens
                )

        return "\n\n".join(secciones)
    except Exception as e:
        logger.error(f"❌ Supabase error al leer contexto: {e}")
        return ""


def obtener_contexto_reciente(folder_id: str) -> str:
    """
    Para el resumen mensual: trae solo el documento más reciente por carpeta
    (máximo 3 docs en total), ordenado por fecha_documento DESC.
    Escalable sin importar cuántos meses acumulen.
    """
    try:
        carpetas = [CARPETA_FACTURA, CARPETA_GRIIN, CARPETA_GENERACION]
        secciones = []

        for carpeta in carpetas:
            resp = supabase.table("documentos_energia")\
                .select("carpeta, archivo_nombre, contenido_texto")\
                .eq("folder_id", folder_id)\
                .eq("carpeta", carpeta)\
                .order("fecha_documento", desc=True)\
                .order("procesado_at", desc=True)\
                .limit(1)\
                .execute()

            docs = resp.data
            if docs and docs[0].get("contenido_texto"):
                doc = docs[0]
                secciones.append(
                    f"--- {doc['carpeta']} | {doc['archivo_nombre']} ---\n"
                    f"{doc['contenido_texto'][:4000]}"
                )

        return "\n\n".join(secciones)
    except Exception as e:
        logger.error(f"❌ Supabase error al leer contexto reciente: {e}")
        return ""


# ─── System prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT_BASE = """Eres SofIA, la asistente de eficiencia energética de Griin Energy. Eres una mujer colombiana muy cálida, cercana y amigable — como la amiga experta en energía que todos quisieran tener.

Tu personalidad:
- Hablas como colombiana real: "¡Claro que sí!", "¡Uy, qué buena pregunta!", "¡Vamos con todo!"
- Explicas los temas técnicos de forma sencilla
- Eres positiva y motivadora, nunca regañas
- Usas 1-2 emojis por mensaje, natural, no forzado

Tu conocimiento:
- Experta en consumo energético empresarial en Colombia
- Sabes sobre facturas, kWh, tarifas, operadores de red
- Griin Energy instala paneles solares y reduce la factura de energía

Formato:
- Respuestas cortas: máximo 5-6 líneas
- Usa *negritas* de WhatsApp para números clave — SIEMPRE asterisco SIMPLE (*texto*), NUNCA doble asterisco (**texto**)
- NUNCA mandes a otro canal — todo se resuelve aquí
- Si tienes datos del cliente, SIEMPRE responde con los números exactos
- Para calcular % cobertura solar: (kWh_solar ÷ (kWh_solar + kWh_red)) × 100{datos_seccion}"""


def get_system_prompt(folder_id: str) -> str:
    contexto = obtener_contexto_cliente(folder_id)
    if contexto:
        datos_seccion = f"""

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DATOS REALES DEL CLIENTE (extraídos de sus documentos):
{contexto}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Con estos datos responde EXACTAMENTE cualquier pregunta sobre consumo, ahorro, generación solar, costos por período. Usa los números reales. Nunca digas que no tienes la información si está en los datos."""
    else:
        datos_seccion = ""
    return SYSTEM_PROMPT_BASE.format(datos_seccion=datos_seccion)


# ─── Chat principal ───────────────────────────────────────────────────────────
def generar_respuesta_chat(mensaje: str, telefono: str) -> str:
    """
    Responde al mensaje del cliente.
    1. Identifica al cliente por NIT si no tiene sesión activa
    2. Lee su contexto desde Supabase
    3. Responde con datos reales
    """
    sesiones = cargar_sesiones()
    sesion = sesiones.get(telefono)
    ahora = time.time()

    # ── ¿Tiene sesión activa? ────────────────────────────────────────────────
    if sesion and (ahora - sesion.get("timestamp", 0)) < SESION_EXPIRY:
        folder_id = sesion["folder_id"]
        nombre    = sesion["nombre"]
    else:
        # ── Buscar NIT en el mensaje ─────────────────────────────────────────
        texto_limpio = mensaje.strip().replace(" ", "").replace(".", "").replace("-", "")
        cliente = None

        # Buscar por NIT
        if texto_limpio.isdigit() and len(texto_limpio) >= 8:
            cliente = CLIENTES_POR_NIT.get(texto_limpio)

        # Buscar por nombre
        if not cliente:
            for c in CLIENTES:
                if c["nombre"].lower() in mensaje.lower():
                    cliente = c
                    break

        if cliente:
            folder_id = cliente["folder_id"]
            nombre    = cliente["nombre"]
            sesiones[telefono] = {
                "folder_id": folder_id,
                "nombre": nombre,
                "timestamp": ahora,
            }
            guardar_sesiones(sesiones)
            conversaciones[telefono] = []
            logger.info(f"Sesión iniciada: {nombre} ({telefono})")
            return f"¡Hola, *{nombre}*! 💚 Soy SofIA, tu asistente de energía de Griin. Ya te tengo identificado. ¿En qué te puedo ayudar hoy?"
        else:
            # Pedir identificación
            if telefono not in conversaciones:
                return (
                    "¡Hola! Soy *SofIA*, tu asistente de energía de Griin 💚\n\n"
                    "Para mostrarte tus datos exactos, ¿me puedes dar el *NIT de tu empresa* "
                    "(sin dígito de verificación)?"
                )
            else:
                return (
                    "No encontré ese NIT en nuestros registros. "
                    "¿Me puedes confirmar el NIT de tu empresa? 🙏"
                )

    # ── Construir historial ──────────────────────────────────────────────────
    if telefono not in conversaciones:
        conversaciones[telefono] = []

    historial = conversaciones[telefono]
    historial.append({"role": "user", "content": mensaje})

    system = get_system_prompt(folder_id)

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=system,
        messages=historial,
    )

    respuesta = response.content[0].text.replace("**", "*")
    historial.append({"role": "assistant", "content": respuesta})

    if len(historial) > MAX_MENSAJES_HISTORIAL:
        conversaciones[telefono] = historial[-MAX_MENSAJES_HISTORIAL:]

    return respuesta


# ─── Análisis de factura PDF enviada por el cliente ──────────────────────────
def analizar_factura_pdf(media_url: str, telefono: str) -> str:
    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token  = os.environ["TWILIO_AUTH_TOKEN"]

    resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=30, follow_redirects=True)
    if resp.status_code != 200:
        raise ValueError(f"No pude descargar la factura: HTTP {resp.status_code}")

    pdf_b64 = base64.standard_b64encode(resp.content).decode("utf-8")

    prompt = """Eres SofIA, la asistente de eficiencia energética de Griin Energy — colombiana, cálida y experta.

Un cliente acaba de mandarte su factura de energía. Analízala y responde con un mensaje de WhatsApp que:
1. Extraiga: nombre/empresa, operador, período, kWh consumidos, valor total COP
2. Genere un mensaje amigable con los datos clave
3. Dé 1-2 observaciones útiles
4. Sea máximo 8 líneas, *negritas* para números, 1-2 emojis
5. Firme como "SofIA 💚 · Griin Energy"

Solo devuelve el mensaje."""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": prompt}
        ]}]
    )

    respuesta = response.content[0].text
    if telefono not in conversaciones:
        conversaciones[telefono] = []
    conversaciones[telefono].append({"role": "user", "content": "[Cliente envió factura PDF]"})
    conversaciones[telefono].append({"role": "assistant", "content": respuesta})
    return respuesta


# ─── Endpoints ───────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "SofIA activa ✅", "version": "0.2.0"}

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/webhook/whatsapp", response_class=PlainTextResponse)
async def webhook_whatsapp(
    request: Request,
    Body: str = Form(default=""),
    From: str = Form(default=""),
    NumMedia: int = Form(default=0),
    MediaUrl0: str = Form(default=""),
    MediaContentType0: str = Form(default=""),
):
    logger.info(f"Mensaje de {From} | Body: {Body}")
    try:
        if NumMedia > 0 and "pdf" in MediaContentType0.lower():
            respuesta = analizar_factura_pdf(MediaUrl0, From)
        elif NumMedia > 0 and MediaContentType0.lower().startswith("image/"):
            respuesta = (
                "Vi que me mandaste una imagen de tu factura. 👋\n"
                "Para analizarla con precisión, ¿me la puedes enviar en *PDF*? 💚"
            )
        else:
            respuesta = generar_respuesta_chat(Body, From)
    except Exception as e:
        logger.error(f"Error procesando mensaje de {From}: {e}")
        respuesta = "Soy SofIA de Griin Energy 💚. Tengo un problema técnico — intenta de nuevo en unos minutos."

    twiml = MessagingResponse()
    twiml.message(respuesta)
    return Response(content=str(twiml), media_type="application/xml")


@app.post("/procesar-cliente/{nombre_cliente}")
async def procesar_cliente(nombre_cliente: str):
    """
    Lee TODOS los archivos de las 3 carpetas del cliente en Drive,
    extrae el texto, guarda en Supabase y envía resumen por WhatsApp.
    """
    cliente = CLIENTES_POR_NOMBRE.get(nombre_cliente.lower())
    if not cliente:
        raise HTTPException(status_code=404, detail=f"Cliente '{nombre_cliente}' no encontrado")

    logger.info(f"Procesando TODOS los archivos de: {cliente['nombre']}")
    drive = get_drive_service()

    folder_id = cliente["folder_id"]
    nombre    = cliente["nombre"]
    archivos_guardados = []
    archivos_error = []

    for carpeta_nombre in [CARPETA_FACTURA, CARPETA_GRIIN, CARPETA_GENERACION]:
        subfolder_id = get_subfolder_id(drive, folder_id, carpeta_nombre)
        if not subfolder_id:
            logger.warning(f"⚠️ Carpeta '{carpeta_nombre}' no encontrada para {nombre}")
            continue

        archivos = get_all_files(drive, subfolder_id)
        logger.info(f"  {carpeta_nombre}: {len(archivos)} archivos")

        for archivo in archivos:
            drive_file_id = archivo["id"]
            archivo_nombre = archivo["name"]

            # Saltar carpetas
            if archivo["mimeType"] == "application/vnd.google-apps.folder":
                continue

            try:
                texto = download_as_text(drive, archivo)
                if not texto.strip():
                    logger.warning(f"  ⚠️ Sin texto: '{archivo_nombre}' (¿imagen escaneada?)")
                    archivos_error.append(archivo_nombre)
                    continue

                guardar_documento_supabase(
                    folder_id=folder_id,
                    nombre_cliente=nombre,
                    carpeta=carpeta_nombre,
                    archivo_nombre=archivo_nombre,
                    contenido_texto=texto,
                    drive_file_id=drive_file_id,
                )
                archivos_guardados.append(archivo_nombre)
            except Exception as e:
                logger.error(f"  ❌ Error procesando '{archivo_nombre}': {e}")
                archivos_error.append(archivo_nombre)

    if not archivos_guardados:
        raise HTTPException(status_code=404, detail=f"No se pudo procesar ningún archivo de {nombre}")

    # Enviar resumen por WhatsApp si tiene teléfono
    mensaje_enviado = None
    if cliente.get("telefono"):
        contexto = obtener_contexto_reciente(folder_id)
        prompt = f"""Eres SofIA de Griin Energy. Genera un mensaje de WhatsApp para {nombre} con el resumen energético.

Si hay varios períodos en los documentos, usa SOLO el más reciente (el de fecha más alta en "Mes reportado:").

En el documento "Factura Griin" busca estas frases exactas:
- "Mes reportado:" → período de facturación
- "kWh generados/mes" → generación solar
- "Pesos ahorrados este mes" → ahorro del mes
- "Total Griin:" → pago Griin
- La fila Con Griin de la tabla → consumo Air-e (kWh) y pago Air-e ($)

Formato del mensaje WhatsApp:
- Saludo cálido colombiano a {nombre}
- *Período:* [fecha]
- *Consumo Air-e:* X kWh — *Pago Air-e:* $X
- *Generación solar:* X kWh — *Pago Griin:* $X
- *Ahorro este mes:* $X 💚
- Máximo 8 líneas, 1-2 emojis
- Firma: "SofIA 💚 · Griin Energy"
- NUNCA uses #, ---, ni corchetes []

DOCUMENTOS:
{contexto}"""

        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        mensaje_enviado = resp.content[0].text.replace("**", "*")

        twilio.messages.create(
            body=mensaje_enviado,
            from_=WHATSAPP_FROM,
            to=f"whatsapp:{cliente['telefono']}",
        )
        logger.info(f"📤 Resumen enviado a {nombre}")

    return {
        "ok": True,
        "cliente": nombre,
        "archivos_guardados": len(archivos_guardados),
        "archivos_error": archivos_error,
        "detalle": archivos_guardados,
        "mensaje_enviado": mensaje_enviado,
    }


@app.post("/procesar-todos")
async def procesar_todos():
    """Procesa TODOS los clientes que tienen teléfono configurado."""
    clientes_activos = [c for c in CLIENTES if c.get("telefono")]
    logger.info(f"Procesando {len(clientes_activos)} clientes")

    resultados = []
    errores = []

    for cliente in clientes_activos:
        try:
            resultado = await procesar_cliente(cliente["nombre"])
            resultados.append(resultado)
        except Exception as e:
            logger.error(f"❌ {cliente['nombre']}: {e}")
            errores.append({"cliente": cliente["nombre"], "error": str(e)})

    return {
        "ok": True,
        "procesados": len(resultados),
        "errores": len(errores),
        "detalle_errores": errores,
    }


@app.get("/clientes")
def listar_clientes():
    return {
        "total": len(CLIENTES),
        "con_telefono": sum(1 for c in CLIENTES if c.get("telefono")),
        "clientes": [
            {"nombre": c["nombre"], "telefono": c.get("telefono") or "pendiente"}
            for c in CLIENTES
        ],
    }


@app.post("/reset-sesion/{telefono}")
async def reset_sesion(telefono: str, x_admin_key: str = Header(default="")):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="No autorizado")
    sesiones = cargar_sesiones()
    if telefono in sesiones:
        del sesiones[telefono]
        guardar_sesiones(sesiones)
    if telefono in conversaciones:
        del conversaciones[telefono]
    return {"ok": True, "telefono": telefono}
