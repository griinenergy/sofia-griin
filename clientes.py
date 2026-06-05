"""
Registro de clientes de Griin Energy.

telefono = None      → pendiente número real, NO se envían mensajes
telefono = "+57..."  → activo, SofIA envía mensajes a este número

MODO TEST:
  - Ferreflex  → Farid    (+573122036674)
  - Bodega Indal → Valentina (+573235841469)

Cuando salgamos en vivo: reemplaza los teléfonos con los números reales de cada cliente.
"""

CLIENTES = [
    {
        "nombre": "American Steel",
        "folder_id": "1QeqObHz_RkdmBf4j_Krhz5ry-0AkAA3e",
        "telefono": None,
    },
    {
        "nombre": "Bodega Indal",
        "folder_id": "11mXGrEKCDtYILAJS7H1ZpArw1Og_dk3z",
        "telefono": "+573235841469",  # TEST: Valentina
    },
    {
        "nombre": "Colegio Bautista",
        "folder_id": "1lehJXtaTPW9NSFlDnGmL9wu6fbwyyi1Q",
        "telefono": None,
    },
    {
        "nombre": "Decomaderas",
        "folder_id": "1273Ych62oUckybAIZxbLHzRbLsaWzEfg",
        "telefono": None,
    },
    {
        "nombre": "Ferreflex",
        "folder_id": "1U7MRR6QKoWO5m6eK54twaLilCAvJqz09",
        "telefono": "+573122036674",  # TEST: Farid
    },
    {
        "nombre": "Helmet Max",
        "folder_id": "1EHsFYc4TJeOT1M0oUDzUO9bRQ-YasyLO",
        "telefono": None,
    },
    {
        "nombre": "Hotel Dubai Valledupar",
        "folder_id": "1dwJhc_Gy-xo7EaXSu3fEQwLe7pJXl8ta",
        "telefono": None,
    },
    {
        "nombre": "Hotel Hawai",
        "folder_id": "1C16YO2-nAxuU66mk4mZvwAxBAZ2VrlES",
        "telefono": None,
    },
    {
        "nombre": "Hotel Honolulu",
        "folder_id": "1rGpiFHXlNX8PdgL5FCTYnxwGcCWQIis-",
        "telefono": None,
    },
    {
        "nombre": "La Gloriosa Centro",
        "folder_id": "1pPVzhuFS2yHFT0qVhbiAeWAzFQZTIRnf",
        "telefono": None,
    },
    {
        "nombre": "La Gloriosa Cundi",
        "folder_id": "1LpTm2XWLz9BXcCotcIGIIFL-pxp8T1iD",
        "telefono": None,
    },
    {
        "nombre": "Politécnico de Montería",
        "folder_id": "1xjg2MF9JisxtNuxqv24ts8y7myReg_qB",
        "telefono": None,
    },
    {
        "nombre": "Puerto Flauta",
        "folder_id": "1tyhfQRcVtZmZimd5QuLiBNXxPxmlyre_",
        "telefono": None,
    },
    {
        "nombre": "Club Tower II",
        "folder_id": "1jtYxHjNS6s2x_SVmBgqwUmlgNghm_7iM",
        "telefono": None,
    },
    {
        "nombre": "Pasaje Comercial",
        "folder_id": "1UMkq6aTDqA1cEf8jm_P7yK2HU0oxvUJo",
        "telefono": None,
    },
    {
        "nombre": "Fríos Caracolí",
        "folder_id": "1-tdhb0LiFnV9BCvE0NibLrqN9wFarYUA",
        "telefono": None,
    },
    {
        "nombre": "La Primavera",
        "folder_id": "1pCbr3gUiO2iCyYeZW6Yun6winoXbw0Uo",
        "telefono": None,
    },
    {
        "nombre": "Transporte La Ceja",
        "folder_id": "1RGkkCl3Bp8UuIa5hCkl9bBTOaNyKqOIo",
        "telefono": None,
    },
]

# Índice por nombre para búsqueda rápida
CLIENTES_POR_NOMBRE = {c["nombre"].lower(): c for c in CLIENTES}
