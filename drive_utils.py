"""
Utilidades para leer archivos de Google Drive.

Autenticación: OAuth 2.0 via variables de entorno:
  - GOOGLE_CLIENT_ID
  - GOOGLE_CLIENT_SECRET
  - GOOGLE_REFRESH_TOKEN

Maneja tanto archivos reales como shortcuts de Drive (los que Farid creó
para no duplicar los archivos originales).
"""

import os
import base64
import logging
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger("sofia.drive")

# Nombres exactos de las subcarpetas en Drive
CARPETA_FACTURA   = "Factura Comercializadora"
CARPETA_GENERACION = "Informe Generación"


def get_drive_service():
    """Crea y retorna el cliente autenticado de Google Drive via OAuth 2.0."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
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
    # Forzar refresh para obtener un access_token válido
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


def get_latest_pdf(drive, folder_id: str) -> dict | None:
    """
    Retorna el PDF más reciente (por fecha de modificación) dentro de una carpeta.
    Incluye shortcuts — los resuelve más adelante en download_as_base64.
    """
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

    # Si hay shortcuts, filtramos solo los que apuntan a PDFs
    for f in files:
        if f["mimeType"] == "application/vnd.google-apps.shortcut":
            # Los shortcuts tienen shortcutDetails con targetMimeType
            target_mime = f.get("shortcutDetails", {}).get("targetMimeType", "")
            if "pdf" in target_mime.lower():
                return f
        elif "pdf" in f["mimeType"].lower() or f["name"].lower().endswith(".pdf"):
            return f

    return files[0]  # Fallback: devolver el primero


def download_as_base64(drive, file: dict) -> tuple[str, str]:
    """
    Descarga un archivo de Drive y lo retorna como (base64_string, mime_type).
    Maneja shortcuts siguiendo al archivo real automáticamente.
    """
    file_id = file["id"]
    mime_type = file["mimeType"]
    file_name = file["name"]

    # Si es un shortcut, seguir al archivo real
    if mime_type == "application/vnd.google-apps.shortcut":
        target_id = file.get("shortcutDetails", {}).get("targetId")
        if not target_id:
            raise ValueError(f"Shortcut '{file_name}' sin targetId — no se puede descargar")
        logger.info(f"Shortcut detectado: '{file_name}' → target {target_id}")
        real_file = drive.files().get(
            fileId=target_id,
            fields="id, name, mimeType",
        ).execute()
        file_id = real_file["id"]
        mime_type = real_file["mimeType"]
        logger.info(f"Archivo real: '{real_file['name']}' ({mime_type})")

    content = drive.files().get_media(fileId=file_id).execute()
    encoded = base64.standard_b64encode(content).decode("utf-8")
    logger.info(f"Archivo descargado: {len(content):,} bytes → base64 OK")
    return encoded, mime_type
