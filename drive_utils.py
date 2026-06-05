"""
Utilidades para leer archivos de Google Drive.
Autenticacion: OAuth 2.0 via variables de entorno:
  - GOOGLE_CLIENT_ID
  - GOOGLE_CLIENT_SECRET
  - GOOGLE_REFRESH_TOKEN
Maneja tanto archivos reales como shortcuts de Drive.
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

CARPETA_FACTURA    = "Factura Comercializadora"
CARPETA_GRIIN      = "Factura Griin"
CARPETA_GENERACION = "Informe Generacion"


def get_drive_service():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Faltan variables de entorno Google")
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


def get_latest_file(drive, folder_id: str) -> dict | None:
    """Retorna el archivo mas reciente en la carpeta (PDF, Excel, o shortcut)."""
    results = drive.files().list(
        q=(
            f"'{folder_id}' in parents"
            f" and trashed = false"
        ),
        orderBy="modifiedTime desc",
        pageSize=10,
        fields="files(id, name, mimeType, modifiedTime, shortcutDetails)",
    ).execute()
    files = results.get("files", [])
    if not files:
        return None
    return files[0]


# Mantener get_latest_pdf como alias para compatibilidad
def get_latest_pdf(drive, folder_id: str) -> dict | None:
    return get_latest_file(drive, folder_id)


def _descargar_bytes(drive, file: dict) -> tuple[bytes, str]:
    """Descarga un archivo de Drive (resuelve shortcuts) y retorna (bytes, nombre_real)."""
    file_id = file["id"]
    mime_type = file["mimeType"]
    file_name = file["name"]

    if mime_type == "application/vnd.google-apps.shortcut":
        target_id = file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            raise ValueError(f"Shortcut '{file_name}' sin targetId")
        logger.info(f"Shortcut: '{file_name}' -> {target_id}")
        real_file = drive.files().get(fileId=target_id, fields="id, name, mimeType").execute()
        file_id = real_file["id"]
        file_name = real_file["name"]

    content = drive.files().get_media(fileId=file_id).execute()
    logger.info(f"Descargado: {len(content):,} bytes")
    return content, file_name


def download_as_base64(drive, file: dict) -> tuple[str, str]:
    content, _ = _descargar_bytes(drive, file)
    encoded = base64.standard_b64encode(content).decode("utf-8")
    return encoded, file["mimeType"]


def download_as_text(drive, file: dict) -> str:
    """
    Descarga un archivo de Drive y extrae su texto.
    Soporta PDF (pdfplumber) y Excel (openpyxl).
    """
    content, file_name = _descargar_bytes(drive, file)
    nombre = file_name.lower()

    # Excel
    if nombre.endswith(".xlsx") or nombre.endswith(".xls"):
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            texto = ""
            for sheet in wb.worksheets:
                texto += f"[Hoja: {sheet.title}]\n"
                for row in sheet.iter_rows(values_only=True):
                    fila = [str(v) for v in row if v is not None]
                    if fila:
                        texto += " | ".join(fila) + "\n"
            logger.info(f"Texto Excel extraido: {len(texto):,} caracteres")
            return texto.strip()
        except Exception as e:
            logger.error(f"Error leyendo Excel: {e}")
            return ""

    # PDF (default)
    try:
        texto = ""
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                texto += (page.extract_text() or "") + "\n"
        logger.info(f"Texto PDF extraido: {len(texto):,} caracteres")
        return texto.strip()
    except Exception as e:
        logger.error(f"Error leyendo PDF: {e}")
        return ""
