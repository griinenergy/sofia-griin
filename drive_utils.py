"""
Utilidades para leer archivos de Google Drive.

Autenticación: OAuth 2.0 via variables de entorno:
  - GOOGLE_CLIENT_ID
  - GOOGLE_CLIENT_SECRET
  - GOOGLE_REFRESH_TOKEN

Maneja tanto archivos reales como shortcuts de Drive.
Soporta PDF (pdfplumber) y Excel (openpyxl).
"""

import os
import io
import base64
import logging
import pdfplumber
import openpyxl
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger("sofia.drive")

# Nombres exactos de las subcarpetas en Drive
CARPETA_FACTURA    = "Factura Comercializadora"
CARPETA_GRIIN      = "Factura Griin"
CARPETA_GENERACION = "Informe Generación"


def get_drive_service():
    """Crea y retorna el cliente autenticado de Google Drive via OAuth 2.0."""
    client_id     = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "Faltan variables de entorno: GOOGLE_CLIENT_ID, "
            "GOOGLE_CLIENT_SECRET y/o GOOGLE_REFRESH_TOKEN"
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def get_subfolder_id(drive, parent_id: str, subfolder_name: str) -> str | None:
    """Busca una subcarpeta por nombre dentro de un folder padre. Retorna su ID o None."""
    results = drive.files().list(
        q=(
            f"'{parent_id}' in parents"
            f" and name = '{subfolder_name}'"
            f" and mimeType = 'application/vnd.google-apps.folder'"
            f" and trashed = false"
        ),
        fields="files(id, name)",
        pageSize=1,
    ).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def get_all_files(drive, folder_id: str) -> list[dict]:
    """
    Retorna TODOS los archivos (PDF, Excel, shortcuts) dentro de una carpeta.
    Ordenados por fecha de modificación descendente.
    """
    results = drive.files().list(
        q=(
            f"'{folder_id}' in parents"
            f" and trashed = false"
        ),
        orderBy="modifiedTime desc",
        pageSize=50,
        fields="files(id, name, mimeType, modifiedTime, shortcutDetails)",
    ).execute()
    return results.get("files", [])


def get_latest_pdf(drive, folder_id: str) -> dict | None:
    """Retorna el archivo más reciente dentro de una carpeta. (Compatibilidad)"""
    files = get_all_files(drive, folder_id)
    return files[0] if files else None


def _descargar_bytes(drive, file: dict) -> tuple[bytes, str]:
    """
    Descarga un archivo y retorna (bytes, nombre_real).
    Resuelve shortcuts automáticamente.
    """
    file_id   = file["id"]
    mime_type = file["mimeType"]
    file_name = file["name"]

    if mime_type == "application/vnd.google-apps.shortcut":
        target_id = file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            raise ValueError(f"Shortcut '{file_name}' sin targetId")
        real_file = drive.files().get(
            fileId=target_id,
            fields="id, name, mimeType",
        ).execute()
        file_id   = real_file["id"]
        file_name = real_file["name"]
        logger.info(f"Shortcut → archivo real: '{file_name}'")

    content = drive.files().get_media(fileId=file_id).execute()
    return content, file_name


def download_as_text(drive, file: dict) -> str:
    """
    Descarga un archivo de Drive y extrae su texto.
    Soporta PDF (pdfplumber) y Excel (openpyxl).
    Retorna texto plano o "" si no se puede extraer.
    """
    content, file_name = _descargar_bytes(drive, file)
    nombre_lower = file_name.lower()

    # ── Excel ────────────────────────────────────────────────────────────────
    if nombre_lower.endswith(".xlsx") or nombre_lower.endswith(".xls"):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            lineas = []
            for sheet in wb.worksheets:
                lineas.append(f"[Hoja: {sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    fila = [str(c) if c is not None else "" for c in row]
                    linea = "\t".join(fila).strip()
                    if linea:
                        lineas.append(linea)
            texto = "\n".join(lineas)
            logger.info(f"Excel extraído: '{file_name}' → {len(texto)} chars")
            return texto
        except Exception as e:
            logger.warning(f"No se pudo extraer Excel '{file_name}': {e}")
            return ""

    # ── PDF ──────────────────────────────────────────────────────────────────
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            paginas = [p.extract_text() or "" for p in pdf.pages]
        texto = "\n".join(paginas).strip()
        logger.info(f"PDF extraído: '{file_name}' → {len(texto)} chars")
        return texto
    except Exception as e:
        logger.warning(f"No se pudo extraer PDF '{file_name}': {e}")
        return ""


def download_as_base64(drive, file: dict) -> tuple[str, str]:
    """
    Descarga un archivo y retorna (base64_string, mime_type). (Compatibilidad)
    """
    file_id   = file["id"]
    mime_type = file["mimeType"]
    file_name = file["name"]

    if mime_type == "application/vnd.google-apps.shortcut":
        target_id = file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            raise ValueError(f"Shortcut '{file_name}' sin targetId")
        real_file = drive.files().get(
            fileId=target_id,
            fields="id, name, mimeType",
        ).execute()
        file_id   = real_file["id"]
        mime_type = real_file["mimeType"]

    content = drive.files().get_media(fileId=file_id).execute()
    encoded = base64.standard_b64encode(content).decode("utf-8")
    return encoded, mime_type
