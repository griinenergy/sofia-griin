"""
Utilidades para leer archivos de Google Drive.
Autenticación: OAuth 2.0 via variables de entorno:
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
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger("sofia.drive")

CARPETA_FACTURA    = "Factura Comercializadora"
CARPETA_GRIIN      = "Factura Griin"
CARPETA_GENERACION = "Informe Generación"


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


def get_latest_pdf(drive, folder_id: str) -> dict | None:
    results = drive.files().list(
        q=(
            f"'{folder_id}' in parents"
            f" and (name contains '.pdf' or mimeType = 'application/pdf'"
            f"      or mimeType = 'application/vnd.google-apps.shortcut')"
            f" and trashed = false"
        ),
        orderBy="modifiedTime desc",
        pageSize=10,
        fields="files(id, name, mimeType, modifiedTime, shortcutDetails)",
    ).execute()
    files = results.get("files", [])
    if not files:
        return None
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.shortcut":
            target_mime = f.get("shortcutDetails", {}).get("targetMimeType", "")
            if "pdf" in target_mime.lower():
                return f
        elif "pdf" in f["mimeType"].lower() or f["name"].lower().endswith(".pdf"):
            return f
    return files[0]


def _descargar_bytes(drive, file: dict) -> bytes:
    """Descarga un archivo de Drive (resuelve shortcuts) y retorna los bytes."""
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
    content = drive.files().get_media(fileId=file_id).execute()
    logger.info(f"Descargado: {len(content):,} bytes")
    return content


def download_as_base64(drive, file: dict) -> tuple[str, str]:
    content = _descargar_bytes(drive, file)
    encoded = base64.standard_b64encode(content).decode("utf-8")
    return encoded, file["mimeType"]


def download_as_text(drive, file: dict) -> str:
    """
    Descarga un PDF de Drive y extrae su texto con pdfplumber.
    Retorna el texto plano — mucho más barato que mandar el PDF completo a Claude.
    """
    content = _descargar_bytes(drive, file)
    texto = ""
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            texto += (page.extract_text() or "") + "\n"
    logger.info(f"Texto extraído: {len(texto):,} caracteres")
    return texto.strip()
