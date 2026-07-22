import logging
import time
import re

import gspread
from google.oauth2.service_account import Credentials

from config import settings

logger = logging.getLogger(__name__)

# Columnas seguras que SE EXPONEN al modelo (sin datos sensibles ni privados).
# Cualquier columna fuera de esta lista se ignora al formatear el contexto.
# Las cabeceras de la hoja deben coincidir exactamente con estos nombres.
REPAIR_COLUMNS = [
    "resguardo",
    "fecha_recepcion",
    "cliente_nombre",
    "equipo_modelo",
    "sintoma",
    "estado",
    "presupuesto_aceptado_id",
    "tecnico_asignado",
    "fecha_reparacion",
    "resultado_reparacion",
    "motivo_sin_reparacion",
    "fecha_entrega",
    "estado_entrega",
    "tipo_recepcion",
    "entrega_mensajeria",
]

# Mapeo opcional de cabeceras "humanas" a los nombres internos snake_case.
# La hoja actual usa directamente snake_case, por lo que esta tabla queda
# vacia. Si en el futuro alguien cambia las cabeceras a formato "amigable",
# añadir aqui el mapeo (p. ej. "Nombre de Cliente" → "cliente_nombre").
HEADER_ALIASES: dict[str, str] = {}


def _normalize_headers(record: dict) -> dict:
    """Translate human headers to snake_case internal keys (HEADER_ALIASES).
    Keys not in the map are kept as-is so existing sheets with snake_case keep working."""
    normalized = {}
    for key, value in record.items():
        canonical = HEADER_ALIASES.get(key, key)
        normalized[canonical] = value
    return normalized


# Estados que significan que la reparacion ya esta cerrada para el cliente.
# Se comparan en mayusculas. Si algun dia se introducen estados nuevos de cierre,
# anadirlos aqui.
CLOSED_STATES = {
    "ENTREGADO",
    "RECICLAJE",
    "REPARADO Y ENTREGADO",
    "ANULADO",
}


def normalize_phone(phone: str) -> str:
    """Remove +, spaces, and leading zeros to normalize phone numbers."""
    return re.sub(r"[+\s\-]", "", phone).lstrip("0")


def phones_match(a: str, b: str) -> bool:
    """Compare two phone numbers, tolerating missing country code 34."""
    na, nb = normalize_phone(a), normalize_phone(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # Try adding/removing Spanish country code (34)
    if na.startswith("34") and na[2:] == nb:
        return True
    if nb.startswith("34") and nb[2:] == na:
        return True
    return False


def _extract_repair(record: dict) -> dict:
    """Extract only safe columns from a sheet record."""
    repair = {}
    for col in REPAIR_COLUMNS:
        value = record.get(col, "")
        if value is not None and str(value).strip():
            repair[col] = str(value).strip()
    return repair


def _is_active(repair: dict) -> bool:
    """A repair is active unless its delivery/estado indicates closure.
    Si la hoja tiene estado_entrega, manda ese. Si no, caemos a `estado`."""
    estado_entrega = (repair.get("estado_entrega") or "").upper().strip()
    if estado_entrega:
        return estado_entrega not in CLOSED_STATES
    estado = (repair.get("estado") or "").upper().strip()
    return estado not in CLOSED_STATES


class SheetsService:
    """Service for fetching repair data and prices from Google Sheets."""

    def __init__(self):
        self._client: gspread.Client | None = None
        self._cache: list[dict] = []
        self._cache_time: float = 0
        self._prices_cache: list[dict] = []
        self._prices_cache_time: float = 0

    async def connect(self):
        """Authenticate with Google Sheets API using a service account."""
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets.readonly",
            ]
            creds = Credentials.from_service_account_file(
                settings.GOOGLE_CREDENTIALS_PATH, scopes=scopes
            )
            self._client = gspread.authorize(creds)
            logger.info("Connected to Google Sheets API")
        except Exception as e:
            logger.error(f"Failed to connect to Google Sheets: {e}", exc_info=True)
            self._client = None

    def _is_cache_valid(self) -> bool:
        return (
            len(self._cache) > 0
            and (time.time() - self._cache_time) < settings.SHEETS_CACHE_TTL
        )

    async def _fetch_all_records(self) -> list[dict]:
        """Fetch all records from the sheet, using cache if valid."""
        if self._is_cache_valid():
            return self._cache

        if not self._client:
            logger.warning("Sheets client not connected, attempting reconnect")
            await self.connect()
            if not self._client:
                return []

        try:
            sheet = self._client.open_by_key(settings.GOOGLE_SHEETS_ID).worksheet("Reparaciones")
            raw_records = sheet.get_all_records()
            records = [_normalize_headers(r) for r in raw_records]
            self._cache = records
            self._cache_time = time.time()
            logger.info(f"Fetched {len(records)} records from Google Sheets")
            return records
        except Exception as e:
            logger.error(f"Error fetching sheet data: {e}", exc_info=True)
            if self._cache:
                logger.warning("Returning stale cache due to fetch error")
                return self._cache
            return []

    async def get_repairs_by_phone(self, phone: str) -> list[dict]:
        """Get all repairs for a given phone number."""
        records = await self._fetch_all_records()

        matches = []
        for record in records:
            record_phone = str(record.get("cliente_telefono", ""))
            if record_phone and phones_match(record_phone, phone):
                matches.append(_extract_repair(record))

        logger.info(f"Found {len(matches)} repairs for phone {phone}")
        return matches

    async def get_repair_by_resguardo(self, resguardo: str, phone: str | None = None) -> dict | None:
        """Get a specific repair by resguardo number.

        Si `phone` es None, no se valida la propiedad del resguardo y se
        devuelve la reparacion tal cual. Si `phone` se pasa, se exige que el
        telefono coincida (modo seguro legado).
        """
        records = await self._fetch_all_records()
        resguardo_clean = resguardo.strip()

        for record in records:
            if str(record.get("resguardo", "")).strip() == resguardo_clean:
                if phone is None:
                    return _extract_repair(record)
                record_phone = str(record.get("cliente_telefono", ""))
                if record_phone and phones_match(record_phone, phone):
                    return _extract_repair(record)
                return None

        return None

    async def _fetch_all_prices(self) -> list[dict]:
        """Fetch all records from the Prices sheet, using cache if valid."""
        if (
            len(self._prices_cache) > 0
            and (time.time() - self._prices_cache_time) < settings.SHEETS_CACHE_TTL
        ):
            return self._prices_cache

        if not self._client:
            await self.connect()
            if not self._client:
                return []

        if not settings.GOOGLE_PRICES_SHEET_ID:
            logger.error("GOOGLE_PRICES_SHEET_ID is not configured — prices will not be available")
            return []

        try:
            spreadsheet = self._client.open_by_key(settings.GOOGLE_PRICES_SHEET_ID)

            # Find the prices worksheet — try "Precios" first, then fall back to first sheet
            available_tabs = [ws.title for ws in spreadsheet.worksheets()]
            logger.info(f"Available tabs in prices spreadsheet: {available_tabs}")

            tab_name = None
            for candidate in ("Precios", "precios", "Prices", "prices", "Sheet1", "Hoja1"):
                if candidate in available_tabs:
                    tab_name = candidate
                    break
            if tab_name is None and available_tabs:
                tab_name = available_tabs[0]
                logger.warning(f"Tab 'Precios' not found — using first tab: '{tab_name}'")

            sheet = spreadsheet.worksheet(tab_name)
            all_values = sheet.get_all_values()

            if not all_values:
                logger.warning("Prices sheet is empty")
                return []

            # Auto-detect header row: find first row that looks like headers
            # (contains at least one non-empty cell that isn't purely numeric)
            header_row_idx = 0
            for i, row in enumerate(all_values[:5]):
                non_empty = [c.strip() for c in row if c.strip()]
                if len(non_empty) >= 3:
                    header_row_idx = i
                    break

            headers = all_values[header_row_idx]
            logger.info(f"Price sheet headers (row {header_row_idx + 1}): {headers}")

            records = []
            for row in all_values[header_row_idx + 1:]:
                if any(cell.strip() for cell in row):
                    record = dict(zip(headers, row))
                    records.append(record)

            self._prices_cache = records
            self._prices_cache_time = time.time()
            logger.info(f"Fetched {len(records)} price records from Google Sheets (tab: '{tab_name}')")
            return records
        except Exception as e:
            logger.error(f"Error fetching prices sheet: {e}", exc_info=True)
            if self._prices_cache:
                return self._prices_cache
            return []

    def _get_field(self, record: dict, *candidates: str) -> str:
        """Return first non-empty value from a list of possible column name candidates."""
        for key in candidates:
            val = str(record.get(key, "")).strip()
            if val:
                return val
        return ""

    async def get_all_prices(self) -> list[dict]:
        """Get all price records."""
        records = await self._fetch_all_prices()
        prices = []
        for r in records:
            prices.append({
                "categoria": self._get_field(r, "Categoria", "Categoría", "categoria", "CATEGORIA"),
                "marca": self._get_field(r, "Marca", "marca", "MARCA"),
                "modelo": self._get_field(r, "Modelo", "modelo", "MODELO"),
                "tipo_reparacion": self._get_field(
                    r, "Tipo_Reparacion", "Tipo de Reparacion", "Tipo de Reparación",
                    "Tipo Reparacion", "tipo_reparacion", "TipoReparacion", "Reparacion",
                ),
                "precio": self._get_field(
                    r, "Precio (S/)", "Precio (€)", "Precio", "precio", "Price",
                    "Precio EUR", "Precio (€ + IVA)", "Precio S/", "PRECIO",
                ),
                "disponible": self._get_field(r, "Disponible", "disponible", "DISPONIBLE", "Estado"),
            })
        # Filter out rows with no marca and no tipo_reparacion (likely blank separator rows)
        prices = [p for p in prices if p["marca"] or p["tipo_reparacion"]]
        # Deze bot behandelt alleen Dyson — toon uitsluitend Dyson-prijzen aan het model.
        prices = [p for p in prices if "dyson" in p["marca"].lower()]
        return prices

    def format_prices_for_prompt(self, prices: list[dict]) -> str:
        """Format price data into context for the AI."""
        if not prices:
            return ""

        lines = ["[PRIJSTABEL REPARATIES]"]
        lines.append(f"Totaal aantal beschikbare diensten: {len(prices)}\n")

        for p in prices:
            disponible = "BESCHIKBAAR" if p["disponible"].lower() == "si" else "NIET BESCHIKBAAR"
            lines.append(
                f"- {p['marca']} {p['modelo']} | {p['tipo_reparacion']} | "
                f"{p['precio']} | {disponible}"
            )

        lines.append("\nINSTRUCTIES — HOE DEZE TABEL TE GEBRUIKEN:")
        lines.append("⚠️ HOOFDREGEL: Deze tabel wordt ALLEEN geraadpleegd als de klant EXPLICIET naar de prijs vraagt ('hoeveel kost het?', 'wat is de prijs?', 'wat rekenen jullie?'). Als de klant alleen een probleem of storing beschrijft ZONDER naar de prijs te vragen, negeer deze tabel dan en volg de normale reparatieflow.")
        lines.append("")
        lines.append("Wanneer je WEL een prijs moet geven, zoek dan op BETEKENIS, niet op exacte tekst.")
        lines.append("Voorbeelden: 'behuizing vervangen' = 'Vervanging behuizing'; 'trekker kapot' = 'Trekker vervangen'; 'laadt niet op' kan batterij of printplaat zijn.")
        lines.append("")
        lines.append("GEVAL A — De klant vraagt naar de prijs en de match is duidelijk:")
        lines.append("  Geef de prijs direct. Format: '🔧 [Type reparatie]: [prijs]+btw'.")
        lines.append("  ❌ Kopieer niet de hele rij. ❌ Zeg niet 'ik geef liever geen prijs zonder controle' als de prijs hier al staat.")
        lines.append("")
        lines.append("GEVAL B — De klant vraagt naar de prijs maar het is niet duidelijk welke exacte dienst nodig is:")
        lines.append("  Toon MAXIMAAL 3 genummerde opties van dat merk/model, kies de meest waarschijnlijke:")
        lines.append("  'Bedoel je een van deze opties?")
        lines.append("  1. [Type reparatie A] — [prijs]+btw")
        lines.append("  2. [Type reparatie B] — [prijs]+btw")
        lines.append("  Is het een van deze, of iets anders?'")
        lines.append("")
        lines.append("GEVAL C — De dienst staat niet in de tabel:")
        lines.append("  Geef aan dat die dienst controle in de winkel vereist om een offerte te geven.")
        lines.append("")
        lines.append("- Bij 'NIET BESCHIKBAAR': geef de prijs door, maar geef aan dat het nu niet beschikbaar is.")
        lines.append("- Verzin NOOIT prijzen. Toon NOOIT interne ruwe velden.")

        return "\n".join(lines)

    def format_repairs_for_prompt(self, repairs: list[dict]) -> str:
        """Format repair data into context for the AI, separating active from closed."""
        if not repairs:
            return ""

        active = [r for r in repairs if _is_active(r)]
        closed = [r for r in repairs if not _is_active(r)]

        lines = ["[REPARATIEGEGEVENS VAN DE KLANT]"]

        # Active repairs - full detail
        if active:
            lines.append(f"\nACTIEVE REPARATIES ({len(active)}):")
            lines.append("Deze toestellen zijn nog in behandeling of wachten op ophalen/verzending.\n")
            for i, r in enumerate(active, 1):
                lines.append(f"--- Toestel {i} van {len(active)} ---")
                lines.extend(_format_single_repair(r))
                lines.append("")
        else:
            lines.append("\nGeen actieve reparaties op dit moment.")

        # Closed repairs - minimal summary
        if closed:
            lines.append(f"\nEERDERE AFGERONDE REPARATIES: {len(closed)}")
            lines.append("Details alleen beschikbaar als de klant naar een specifiek ontvangstbewijsnummer vraagt.\n")
            for r in closed:
                estado_entrega = r.get("estado_entrega", "")
                lines.append(
                    f"  Ontvangstbewijs {r.get('resguardo', '?')} | "
                    f"{r.get('equipo_modelo', '?')} | "
                    f"{estado_entrega}"
                )
            lines.append("")

        # Instructions for GPT
        lines.append("INSTRUCTIES:")
        lines.append("- Als de klant algemeen vraagt, antwoord dan ALLEEN over de ACTIEVE reparaties.")
        lines.append("- Bij meerdere actieve toestellen, informeer over ALLEMAAL, niet alleen het eerste.")
        lines.append("- Geen actieve maar wel eerdere reparaties: 'Je hebt geen actieve reparaties. Je hebt X eerdere afgeronde reparaties.'")
        lines.append("- Bij een specifiek ontvangstbewijsnummer uit de geschiedenis, geef de status daarvan.")
        lines.append("- Verzin NOOIT informatie die niet in deze gegevens staat.")

        return "\n".join(lines)



def _format_single_repair(r: dict) -> list[str]:
    """Format a single repair into readable lines."""
    lines = [
        f"Ontvangstbewijsnr.: {r.get('resguardo', 'N/A')}",
        f"Toestel: {r.get('equipo_modelo', 'N/A')}",
        f"Probleem: {r.get('sintoma', 'N/A')}",
        f"Status: {r.get('estado', 'N/A')}",
        f"Ontvangen: {r.get('fecha_recepcion', 'N/A')}",
    ]
    if r.get("presupuesto_aceptado_id"):
        lines.append(f"Offerte geaccepteerd: {r['presupuesto_aceptado_id']}")
    if r.get("tecnico_asignado"):
        lines.append(f"Toegewezen technicus: {r['tecnico_asignado']}")
    if r.get("fecha_reparacion"):
        lines.append(f"Reparatiedatum: {r['fecha_reparacion']}")
    if r.get("resultado_reparacion"):
        lines.append(f"Resultaat: {r['resultado_reparacion']}")
    if r.get("motivo_sin_reparacion"):
        lines.append(f"Reden geen reparatie: {r['motivo_sin_reparacion']}")
    if r.get("fecha_entrega"):
        lines.append(f"Afleverdatum: {r['fecha_entrega']}")
    if r.get("estado_entrega"):
        estado_e = r["estado_entrega"]
        if estado_e.upper() == "ENVIO":
            lines.append("Afleverstatus: ONDERWEG (verzonden naar de klant)")
        else:
            lines.append(f"Afleverstatus: {estado_e}")
    if r.get("tipo_recepcion"):
        lines.append(f"Type ontvangst: {r['tipo_recepcion']}")
    if r.get("entrega_mensajeria"):
        lines.append(f"Levering per koerier: {r['entrega_mensajeria']}")
    return lines
