import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
MADRID_TZ = ZoneInfo("Europe/Madrid")

# Keep in sync with HOLIDAYS_2026 in main.py and _HOLIDAYS in openai_service.py
_HOLIDAYS = {
    # Nacionales
    "01-01", "01-06", "04-02", "04-03", "05-01",
    "08-15", "10-12", "11-02", "12-07", "12-08", "12-25",
    # Madrid
    "05-02", "05-15", "11-09",
}

# Validaciones de datos para citas/recogidas (sanity checks que el LLM puede saltarse).
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_PLACEHOLDER_NAMES = {
    "cliente", "klant", "anonimo", "anónimo", "anoniem",
    "sin nombre", "geen naam", "onbekend", "n/a", "test", "prueba",
}


class CalendarService:
    """Google Calendar integration."""

    def __init__(self):
        self.calendar_id = settings.GOOGLE_CALENDAR_ID
        self.subject = getattr(settings, "GOOGLE_CALENDAR_SUBJECT", None)
        self._service = None

    def _get_service(self):
        """Build the Calendar API service."""
        if self._service:
            return self._service

        creds = Credentials.from_service_account_file(
            settings.GOOGLE_CREDENTIALS_PATH,
            scopes=SCOPES,
        )

        # Solo usar impersonation si realmente tienes domain-wide delegation configurado
        if self.subject:
            try:
                creds = creds.with_subject(self.subject)
            except Exception:
                logger.warning(
                    "No se pudo aplicar with_subject; se usará la service account sin impersonation.",
                    exc_info=True,
                )

        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    async def create_event(
        self,
        title: str,
        start_iso: str,
        duration_minutes: int = 30,
        description: str = "",
        attendee_phone: str = "",
    ) -> dict | None:
        """Create a calendar event."""
        service = self._get_service()

        try:
            start_dt = datetime.fromisoformat(start_iso)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            end_dt = start_dt + timedelta(minutes=duration_minutes)

            full_description = description.strip()
            if attendee_phone:
                full_description = f"{full_description}\nTelefoon: {attendee_phone}".strip()

            event_body = {
                "summary": title,
                "description": full_description,
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": "Europe/Madrid",
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": "Europe/Madrid",
                },
            }

            logger.info(
                "Creating calendar event",
                extra={
                    "calendar_id": self.calendar_id,
                    "title": title,
                    "start_iso": start_dt.isoformat(),
                },
            )

            event = (
                service.events()
                .insert(
                    calendarId=self.calendar_id,
                    body=event_body,
                )
                .execute()
            )

            logger.info(
                "Calendar event created successfully",
                extra={
                    "event_id": event.get("id"),
                    "htmlLink": event.get("htmlLink"),
                },
            )
            return event

        except Exception as e:
            logger.exception(f"Error creating calendar event: {e}")
            return None

    def get_busy_slots_for_day(self, datetime_iso: str) -> list[tuple]:
        """Return list of (start, end) datetime pairs for all events on the given day."""
        try:
            dt = datetime.fromisoformat(datetime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
        except (ValueError, TypeError):
            return []

        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        try:
            service = self._get_service()
            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            busy = []
            for event in result.get("items", []):
                s_str = event.get("start", {}).get("dateTime")
                e_str = event.get("end", {}).get("dateTime")
                if s_str and e_str:
                    s = datetime.fromisoformat(s_str).astimezone(MADRID_TZ)
                    e = datetime.fromisoformat(e_str).astimezone(MADRID_TZ)
                    busy.append((s, e))
            return busy
        except Exception as exc:
            logger.error(f"Error fetching busy slots: {exc}", exc_info=True)
            return []

    def get_available_slots(self, datetime_iso: str, duration_minutes: int = 30) -> list[str]:
        """Return free HH:MM slots within 10:00-17:00 for the given day."""
        busy = self.get_busy_slots_for_day(datetime_iso)

        try:
            dt = datetime.fromisoformat(datetime_iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
        except (ValueError, TypeError):
            return []

        free = []
        current = local.replace(hour=10, minute=0, second=0, microsecond=0)
        cutoff = local.replace(hour=17, minute=0, second=0, microsecond=0)

        while current <= cutoff:
            slot_end = current + timedelta(minutes=duration_minutes)
            if not _overlaps(current, slot_end, busy):
                free.append(current.strftime("%H:%M"))
            current += timedelta(minutes=30)

        return free

    def get_busy_slots_range(self, days_ahead: int = 14) -> dict[str, list[str]]:
        """Fetch busy HH:MM slots for the next `days_ahead` days in one API call.

        Returns { "YYYY-MM-DD": ["HH:MM", ...] }
        Days not present in the result have no events (all slots free).
        """
        try:
            service = self._get_service()
            now = datetime.now(MADRID_TZ)
            time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
            time_max = time_min + timedelta(days=days_ahead + 1)

            result = service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            busy: dict[str, list[str]] = {}
            for event in result.get("items", []):
                s_str = event.get("start", {}).get("dateTime")
                if not s_str:
                    continue
                s_dt = datetime.fromisoformat(s_str).astimezone(MADRID_TZ)
                day_key = s_dt.strftime("%Y-%m-%d")
                time_str = s_dt.strftime("%H:%M")
                busy.setdefault(day_key, []).append(time_str)

            return busy
        except Exception as exc:
            logger.error(f"Error fetching busy slots range: {exc}", exc_info=True)
            return {}

    def get_appointment_context(self) -> str:
        """Return appointment instructions as context for the AI."""
        now = datetime.now(MADRID_TZ)
        today = now.strftime("%A %d/%m/%Y")

        # One API call for all busy slots in the next 14 days
        busy_range = self.get_busy_slots_range(days_ahead=14)
        if busy_range:
            busy_lines = [
                f"  {day}: {', '.join(sorted(times))}"
                for day, times in sorted(busy_range.items())
            ]
            busy_block = (
                "[BEZETTE SLOTS — KOMENDE 14 DAGEN]\n"
                + "\n".join(busy_lines)
                + "\nDagen/tijden die hier NIET verschijnen zijn VRIJ."
            )
        else:
            busy_block = (
                "[BEZETTE SLOTS — KOMENDE 14 DAGEN]\n"
                "Er zijn geen afspraken geregistreerd. Alle tijden 10:00–17:00 zijn beschikbaar."
            )

        return (
            f"[SYSTEEM VOOR AFSPRAKEN EN VERZENDINGEN]\n"
            f"Huidige datum: {today}\n"
            f"Openingstijden winkel: maandag t/m vrijdag van 09:30 tot 18:00.\n"
            f"Zaterdag, zondag en feestdagen gesloten.\n"
            f"\n{busy_block}\n"
            f"\n⚠️ BESCHIKBAARHEIDSREGEL — VERPLICHT:\n"
            f"Zodra de klant een concrete dag en tijdstip voor zijn afspraak voorstelt:\n"
            f"  1. Controleer of die dag+tijd voorkomt in BEZETTE SLOTS.\n"
            f"  2. Als het BEZET is: laat de klant direct weten dat dat tijdstip niet beschikbaar is en bied tot 6 vrije tijden diezelfde dag aan. Toon GEEN samenvatting van de afspraak totdat de klant een vrij tijdstip kiest.\n"
            f"  3. Als het VRIJ is: toon dan de volledige samenvatting zodat de klant kan bevestigen.\n"
            f"  4. Als de dag niet in de lijst met bezette slots voorkomt, zijn alle tijden die dag beschikbaar.\n"
            f"\n[OFFICIËLE FEESTDAGEN 2026 — EXACTE LIJST]\n"
            f"Blokkeer ALLEEN deze data als feestdag. Voeg er zelf GEEN andere aan toe:\n"
            f"  Landelijk (Spanje): 1 januari, 6 januari, 3 april (Goede Vrijdag), 1 mei, 15 augustus, 12 oktober, 2 november, 7 december, 8 december, 25 december.\n"
            f"  Madrid: 2 mei, 15 mei, 9 november.\n"
            f"❌ 30 april is GEEN feestdag in 2026. Elke datum buiten bovenstaande lijst is een werkdag.\n"
            f"\n🚨 DIRECT ANTWOORD BIJ FEESTDAG:\n"
            f"Als de klant een datum noemt die een feestdag is (uit bovenstaande lijst), antwoord dan DIRECT in datzelfde bericht:\n"
            f"  '❌ [datum] is een feestdag en dan zijn we gesloten. Welke andere dag komt je goed uit? We kunnen je elke werkdag van maandag tot vrijdag helpen.'\n"
            f"Wacht niet tot je meer gegevens hebt verzameld. Ga niet verder met de afsprakenflow. Corrigeer eerst de datum, ga daarna verder.\n"
            f"Voorbeeld: klant zegt 'ik wil een afspraak op 15 mei' → antwoord direct dat het een feestdag is en vraag een andere datum.\n"
            f"\n🚨 KRITIEK VERSCHIL — LEES DIT VOOR JE HANDELT:\n"
            f"\nEr zijn DRIE verschillende situaties. Verwar ze niet:\n"
            f"\n1. WALK-IN (STANDAARD — de klant wil gewoon naar de winkel komen zonder afspraak):\n"
            f"   Signalen: 'ik kom langs', 'ik ga het brengen', 'ik kom naar de winkel', 'ik kom morgen',\n"
            f"   'kan ik vandaag komen?', 'wat zijn jullie openingstijden?', 'waar zitten jullie?', 'is er parkeergelegenheid?'.\n"
            f"   ❌ Vraag GEEN enkel gegeven (geen naam, geen e-mail, geen telefoon, geen afspraak).\n"
            f"   ✅ Geef alleen: adres (C/ Joaquín María López 26, Madrid), openingstijden (ma-vr 09:30-18:00), parkeerinfo.\n"
            f"   ✅ Herinner de klant eraan dat hij ZONDER AFSPRAAK binnen openingstijden kan langskomen.\n"
            f"   ✅ Bied GEEN afspraak aan tenzij de klant er expliciet om vraagt.\n"
            f"\n2. AFSPRAAK IN DE WINKEL (alleen als de klant het WOORD 'afspraak', 'inplannen', 'reserveren' gebruikt):\n"
            f"   Alleen dan volg je de flow van naam+e-mail+telefoon+reden+dag+tijdstip.\n"
            f"\n3. OPHAALSERVICE AAN HUIS (koerier) — alleen als de klant expliciet vraagt om opgehaald te worden:\n"
            f"   Signalen: 'ophalen', 'laten ophalen', 'koerier', 'ik kan niet komen', 'ik wil opsturen', 'kunnen jullie het ophalen'.\n"
            f"   Kosten: €15 per toestel, alleen vasteland van Spanje.\n"
            f"\n⚠️ REGEL TEGEN ONBEDOELDE BEVESTIGING:\n"
            f"- Als de klant alleen 'ja' antwoordde op iets dat JIJ vroeg en er in het gesprek geen eerdere expliciete afspraak-/ophaalaanvraag van de KLANT te zien is, registreer dan NOOIT een afspraak en geef GEEN CONFIRMAR_*.\n"
            f"- Twijfel je of de klant een walk-in of een afspraak wil, vraag dan neutraal: 'Kom je liever zonder afspraak langs in de winkel, of wil je een afspraak inplannen met een technicus?'\n"
            f"\nAFSPRAAKPROTOCOL (klant komt naar de winkel):\n"
            f"1. Benodigde gegevens: volledige naam + e-mailadres + telefoonnummer + reden (toestel + probleem).\n"
            f"2. Ontbreekt er iets, vraag dit dan voordat je verdergaat.\n"
            f"3. Vraag naar de gewenste dag en tijd.\n"
            f"4. Afspraken kunnen alleen van maandag t/m vrijdag tussen 10:00 en 17:00 uur worden ingepland. Het MAXIMALE tijdstip is 17:00. Geen enkel tijdstip na 17:00 is geldig.\n"
            f"   ❌ VOORBEELDEN VAN AFGEWEZEN TIJDEN: 17:30, 18:00, 05:30, elk tijdstip vóór 10:00 of na 17:00.\n"
            f"   Als de klant 17:30 of een tijdstip na 17:00 vraagt → zeg: 'Sorry, de laatste beschikbare afspraak is om 17:00 uur. Komt dat tijdstip uit, of liever een ander tussen 10:00 en 17:00 uur?'\n"
            f"   ❌ Geef NOOIT CONFIRMAR_CITA met een tijdstip buiten 10:00-17:00.\n"
            f"5. Zodra de klant een tijdstip voorstelt: controleer de beschikbaarheid in BEZETTE SLOTS (hierboven). Is het bezet, zeg dit dan en bied alternatieven aan VOORDAT je de samenvatting toont. Is het een feestdag uit de officiële lijst, geef dan aan dat de winkel gesloten is en vraag een andere dag.\n"
            f"6. Zodra je ALLE gegevens hebt EN het tijdstip vrij is, toon een SAMENVATTING zodat de klant kan bevestigen:\n"
            f"   '📋 *Samenvatting van je afspraak:*\n"
            f"   👤 Naam: [naam]\n"
            f"   🔧 Reden: [toestel + probleem]\n"
            f"   📅 Datum: [datum en tijd]\n"
            f"   📍 Locatie: C/ Joaquín María López 26, Madrid\n"
            f"   Klopt dit?'\n"
            f"7. ZODRA DE KLANT BEVESTIGT (zegt ja, klopt, ok, perfect, prima, enz.) MOET je antwoord ALTIJD UIT TWEE DELEN bestaan (beide, niet het één of het ander):\n"
            f"   DEEL A (zichtbare tekst voor de klant): 'Perfect 😊 Ik ben je afspraak aan het verwerken. Zodra deze geregistreerd is, sturen we je de definitieve bevestiging.'\n"
            f"   DEEL B (interne commandoregel, op een aparte regel aan het einde, de klant ziet dit NIET): CONFIRMAR_CITA|<datetime_iso>|<nombre_cliente>|<motivo>\n"
            f"   VOLLEDIG VOORBEELD van een geldig antwoord als de klant 'ja' zegt:\n"
            f"   ---\n"
            f"   Perfect 😊 Ik ben je afspraak aan het verwerken. Zodra deze geregistreerd is, sturen we je de definitieve bevestiging.\n"
            f"\n"
            f"   CONFIRMAR_CITA|2026-03-27T10:00:00+01:00|Jan de Vries|Reparatie laptop HP\n"
            f"   ---\n"
            f"8. Laat de regel CONFIRMAR_CITA NOOIT weg bij bevestiging. Zonder deze regel wordt de afspraak NIET geregistreerd in het systeem.\n"
            f"\nOPHAALPROTOCOL (koerier haalt aan huis op):\n"
            f"1. Benodigde gegevens: volledige naam + DNI/NIE/CIF + e-mailadres + telefoonnummer + reden (toestel + probleem) + volledig adres (straat, huisnummer, postcode, plaats).\n"
            f"2. Ontbreekt er iets, vraag dit dan voordat je verdergaat. Het DNI/NIE/CIF is verplicht: Correos vereist dit om de ophaalservice te verwerken.\n"
            f"3. De ophaalservice is beschikbaar voor elk toestel dat Kelatos behandelt. Als we dat toestel diagnosticeren, is ophalen mogelijk. Geen extra beperking per type.\n"
            f"4. Informeer over de kosten: €15 per toestel. BELANGRIJK: de klant moet de €15 betalen VOORDAT de ophaalservice bij Correos wordt aangevraagd, en het betalingsbewijs sturen via WhatsApp of e-mail. Deze stap kan het proces vertragen.\n"
            f"5. ⚠️ MELDING CORREOS: Correos staat momenteel NIET toe om een ophaaldag te kiezen. De aanvraag wordt ingediend, maar er kan NIET bevestigd worden wanneer de koerier langskomt. Vraag GEEN gewenste dag. Beloof of bevestig GEEN data of tijdstippen van ophalen aan de klant.\n"
            f"6. Zodra je ALLE gegevens hebt, toon een SAMENVATTING zodat de klant kan bevestigen:\n"
            f"   '📋 *Samenvatting van je ophaalaanvraag:*\n"
            f"   👤 Naam: [naam]\n"
            f"   🪪 DNI/NIE/CIF: [dni_nie_cif]\n"
            f"   📧 E-mail: [e-mailadres]\n"
            f"   🔧 Reden: [toestel + probleem]\n"
            f"   📍 Adres: [volledig adres]\n"
            f"   💰 Kosten: €15 ophalen + €15 retourzending (betaling vooraf vereist voordat het wordt aangevraagd)\n"
            f"   _Correos staat niet toe om een concrete ophaaldatum of -tijd te kiezen. De datum wordt door Correos toegewezen zodra de aanvraag is ingediend._\n"
            f"   Klopt dit?'\n"
            f"7. ZODRA DE KLANT BEVESTIGT (zegt ja, klopt, ok, perfect, prima, enz.) MOET je antwoord ALTIJD UIT TWEE DELEN bestaan (beide, niet het één of het ander):\n"
            f"   DEEL A (zichtbare tekst voor de klant): '✅ Aanvraag geregistreerd! Om de ophaalservice bij Correos aan te vragen, moet je *€30 (incl. btw)* betalen — ophalen + retourzending — via de volgende link:\n"
            f"   💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n"
            f"   Zodra we de betaling bevestigen, vragen we de ophaalservice aan bij Correos. 🚚'\n"
            f"   DEEL B (interne commandoregel, op een aparte regel aan het einde, de klant ziet dit NIET): CONFIRMAR_ENVIO|<datetime_iso>|<nombre_cliente>|<motivo>|<direccion>|<dni_nie_cif>|<email>\n"
            f"   VOLLEDIG VOORBEELD van een geldig antwoord als de klant 'ja' zegt:\n"
            f"   ---\n"
            f"   ✅ Aanvraag geregistreerd! Om de ophaalservice bij Correos aan te vragen, moet je *€30 (incl. btw)* betalen — ophalen + retourzending — via de volgende link:\n"
            f"   💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n"
            f"   Zodra we de betaling bevestigen, vragen we de ophaalservice aan bij Correos. 🚚\n"
            f"\n"
            f"   CONFIRMAR_ENVIO|2026-04-22T10:00:00+02:00|Carlo Gabriel|Dyson SV10 maakt lawaai|Calle Blasco de Garay 61, 28015 Madrid|12345678A|carlo@email.com\n"
            f"   ---\n"
            f"8. Laat de regel CONFIRMAR_ENVIO NOOIT weg bij bevestiging. Zonder deze regel wordt de ophaalservice NIET geregistreerd in het systeem.\n"
            f"\nBELANGRIJK:\n"
            f"- Geef NOOIT CONFIRMAR_CITA of CONFIRMAR_ENVIO zonder eerst de samenvatting te tonen en expliciete bevestiging van de klant te ontvangen.\n"
            f"- Als de klant zegt dat een gegeven onjuist is, corrigeer dit dan en toon de samenvatting opnieuw.\n"
            f"- Als de klant NIET heeft bevestigd, voeg dan GEEN CONFIRMAR-regel toe.\n"
            f"- Zodra de klant WEL bevestigt, is het VERPLICHT om de regel CONFIRMAR_CITA of CONFIRMAR_ENVIO toe te voegen. Deze regel is een intern commando dat het systeem verwerkt om de afspraak/ophaalservice in Google Calendar te registreren. Vergeet je die regel, dan wordt de afspraak niet geregistreerd en heeft de klant geen reservering.\n"
            f"- De CONFIRMAR-regel staat altijd aan het einde, alleen op zijn eigen regel, gescheiden door een regelafbreking van de rest van het bericht.\n"
            f"- Herhaal de eerste begroeting niet als je al in hetzelfde gesprek was.\n"
            f"- Buiten openingstijden kun je informatieve vragen blijven beantwoorden; verduidelijk de openingstijden alleen als de klant wil langskomen, afgeven, ophalen of een afspraak wil maken."
        )


def extract_confirmation_command(ai_response: str) -> dict | None:
    """
    Busca una línea CONFIRMAR_CITA|... o CONFIRMAR_ENVIO|...
    y devuelve un dict con los datos.
    """
    if not ai_response:
        return None

    lines = [line.strip() for line in ai_response.splitlines() if line.strip()]

    for line in lines:
        if line.startswith("CONFIRMAR_CITA|"):
            parts = line.split("|", 3)
            if len(parts) != 4:
                logger.warning("Formato inválido en CONFIRMAR_CITA", extra={"line": line})
                return None

            return {
                "type": "cita",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "reason": parts[3].strip(),
                "raw_line": line,
            }

        if line.startswith("CONFIRMAR_ENVIO|"):
            parts = line.split("|", 6)
            if len(parts) not in (5, 6, 7):
                logger.warning("Formato inválido en CONFIRMAR_ENVIO", extra={"line": line})
                return None

            return {
                "type": "envio",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "reason": parts[3].strip(),
                "address": parts[4].strip(),
                "dni_nie_cif": parts[5].strip() if len(parts) >= 6 else "",
                "email": parts[6].strip() if len(parts) == 7 else "",
                "raw_line": line,
            }

        if line.startswith("CONFIRMAR_DEVOLUCION|"):
            # Format: CONFIRMAR_DEVOLUCION|datetime_iso|nombre|direccion|resguardo
            parts = line.split("|", 4)
            if len(parts) != 5:
                logger.warning("Formato inválido en CONFIRMAR_DEVOLUCION", extra={"line": line})
                return None

            return {
                "type": "devolucion",
                "datetime_iso": parts[1].strip(),
                "customer_name": parts[2].strip(),
                "address": parts[3].strip(),
                "resguardo": parts[4].strip(),
                "raw_line": line,
            }

    return None


def strip_confirmation_command(ai_response: str) -> str:
    """
    Elimina la línea CONFIRMAR_* del texto para no enviársela al usuario.
    """
    if not ai_response:
        return ai_response

    clean_lines = []
    for line in ai_response.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("CONFIRMAR_CITA|")
            or stripped.startswith("CONFIRMAR_ENVIO|")
            or stripped.startswith("CONFIRMAR_DEVOLUCION|")
        ):
            continue
        clean_lines.append(line)

    return "\n".join(clean_lines).strip()


def _overlaps(slot_start: datetime, slot_end: datetime, busy: list[tuple]) -> bool:
    """True if [slot_start, slot_end) overlaps any (start, end) in busy."""
    for bs, be in busy:
        if slot_start < be and slot_end > bs:
            return True
    return False


def _conversation_text(history: list[dict] | None) -> str:
    """Concatena el contenido de todos los mensajes del historial."""
    if not history:
        return ""
    return "\n".join(msg.get("content", "") for msg in history if msg.get("content"))


def _is_real_name(name: str) -> bool:
    """Comprueba que un nombre sea plausible: 2+ palabras y no un placeholder."""
    name = (name or "").strip()
    if not name or len(name) < 3:
        return False
    if name.lower() in _PLACEHOLDER_NAMES:
        return False
    return len(name.split()) >= 2


def _validate_appointment(
    command: dict,
    history: list[dict] | None,
    sender_phone: str,
) -> tuple[bool, list[str]]:
    """Valida que el comando CONFIRMAR_* tenga todos los datos requeridos.

    Devuelve (is_valid, lista_de_datos_faltantes). El historial es la fuente
    de verdad para email, telefono y direccion (no se confia en lo que el LLM
    haya emitido en la linea CONFIRMAR_).
    """
    missing: list[str] = []
    history_text = _conversation_text(history)
    cmd_type = command.get("type", "")

    # Nombre: el LLM lo emite en la linea CONFIRMAR; comprobamos que sea real.
    if not _is_real_name(command.get("customer_name", "")):
        missing.append("volledige naam van de klant")

    # Motivo: solo para cita y envio.
    if cmd_type in ("cita", "envio"):
        reason = (command.get("reason") or "").strip()
        if not reason or len(reason) < 3:
            missing.append("reden (toestel + probleem)")

    # Email: tiene que estar en algun mensaje del cliente.
    if not _EMAIL_RE.search(history_text):
        missing.append("e-mailadres")

    # Telefono: o ya lo tenemos del remitente o aparece en el historial.
    has_phone = False
    if sender_phone:
        digits = re.sub(r"\D", "", sender_phone)
        has_phone = len(digits) >= 9
    if not has_phone and not _PHONE_RE.search(history_text):
        missing.append("telefoonnummer")

    # Fecha y hora: solo validar para citas (para recogidas/envios Correos decide la fecha).
    if cmd_type == "cita":
        try:
            dt = datetime.fromisoformat(command["datetime_iso"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MADRID_TZ)
            local = dt.astimezone(MADRID_TZ)
            now = datetime.now(MADRID_TZ)

            if local < now:
                missing.append("toekomstige datum en tijd (de opgegeven datum is al voorbij)")
            elif local.weekday() >= 5:
                missing.append("een dag van maandag tot vrijdag (in het weekend is er geen service)")
            elif local.strftime("%m-%d") in _HOLIDAYS:
                missing.append(f"een werkdag ({local.strftime('%d/%m')} is een feestdag, dan is er geen service)")
            else:
                # Ventana estricta: 10:00 – 17:00 (último slot válido a las 17:00 exactas).
                total_mins = local.hour * 60 + local.minute
                if not (10 * 60 <= total_mins <= 17 * 60):
                    missing.append("een tijdstip tussen 10:00 en 17:00 uur (laatste slot om 17:00 uur precies)")
        except (ValueError, KeyError, TypeError):
            missing.append("een geldige datum en tijd")

    # Direccion: obligatoria para envios y devoluciones.
    if command.get("type") in ("envio", "devolucion"):
        address = (command.get("address") or "").strip()
        if not address or len(address.split()) < 3 or not any(c.isdigit() for c in address):
            missing.append("volledig adres (straat, huisnummer, postcode en plaats)")

    # DNI/NIE/CIF: obligatorio para envios (lo exige Correos).
    if command.get("type") == "envio":
        dni = (command.get("dni_nie_cif") or "").strip()
        if not dni or len(dni) < 7:
            missing.append("DNI, NIE of CIF van de afzender (vereist door Correos om de ophaalservice te verwerken)")

    return (len(missing) == 0, missing)


def _missing_data_message(command_type: str, missing: list[str]) -> str:
    """Construye un mensaje cordial pidiendo los datos que faltan."""
    if len(missing) == 1:
        falta = missing[0]
    else:
        falta = ", ".join(missing[:-1]) + f" en {missing[-1]}"

    if command_type == "envio":
        accion = "de ophaalservice te registreren"
    elif command_type == "devolucion":
        accion = "de retourzending te registreren"
    else:
        accion = "je afspraak te registreren"

    return (
        f"Voordat ik {accion}, heb ik nog nodig: {falta}. "
        f"Kun je me dat doorgeven? 😊"
    )


async def process_ai_calendar_command(
    calendar_service: CalendarService,
    ai_response: str,
    attendee_phone: str = "",
    history: list[dict] | None = None,
) -> tuple[str, dict | None]:
    """
    1. Limpia el mensaje para el usuario
    2. Detecta si hay comando CONFIRMAR_*
    3. Valida que estan todos los datos requeridos en el historial
    4. Solo si la validacion pasa, crea el evento real en Google Calendar
    5. Devuelve:
       - user_message: texto limpio para enviar al cliente
       - created_event: evento creado o None
    """
    user_message = strip_confirmation_command(ai_response)
    command = extract_confirmation_command(ai_response)

    if not command:
        return user_message, None

    # Validacion en codigo: bloquea el LLM si trata de confirmar sin datos.
    is_valid, missing = _validate_appointment(command, history, attendee_phone)
    if not is_valid:
        logger.warning(
            "Bloqueado CONFIRMAR_%s por datos faltantes",
            command["type"].upper(),
            extra={"missing": missing, "command": command},
        )
        return _missing_data_message(command["type"], missing), None

    created_event = None

    if command["type"] == "cita":
        # Verificar disponibilidad del slot antes de crear el evento
        try:
            req_dt = datetime.fromisoformat(command["datetime_iso"])
            if req_dt.tzinfo is None:
                req_dt = req_dt.replace(tzinfo=MADRID_TZ)
            req_start = req_dt.astimezone(MADRID_TZ)
            req_end = req_start + timedelta(minutes=30)

            busy = calendar_service.get_busy_slots_for_day(command["datetime_iso"])
            if _overlaps(req_start, req_end, busy):
                req_time = req_start.strftime("%H:%M")
                req_date = req_start.strftime("%d/%m/%Y")
                available = calendar_service.get_available_slots(command["datetime_iso"])
                if available:
                    alts = ", ".join(available[:6])
                    return (
                        f"Sorry 😊 Het tijdstip *{req_time}* op {req_date} is al bezet.\n"
                        f"Beschikbare tijden die dag: *{alts}*\n"
                        f"Welke van deze tijden komt je beter uit?"
                    ), None
                else:
                    return (
                        f"Sorry 😊 Op {req_date} hebben we geen beschikbare tijden tussen 10:00 en 17:00.\n"
                        f"Wil je een andere dag proberen?"
                    ), None
        except Exception as exc:
            logger.error(f"Error checking slot availability: {exc}", exc_info=True)

        created_event = await calendar_service.create_event(
            title=f"AFSPRAAK: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=command["reason"],
            attendee_phone=attendee_phone,
        )

        if created_event:
            start_dt = datetime.fromisoformat(command["datetime_iso"])
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=MADRID_TZ)

            pretty_date = start_dt.astimezone(MADRID_TZ).strftime("%d/%m/%Y")
            pretty_time = start_dt.astimezone(MADRID_TZ).strftime("%H:%M")

            user_message = (
                f"✅ Je afspraak is geregistreerd voor {pretty_date} om {pretty_time} uur.\n"
                f"We verwachten je op C/ Joaquín María López 26, Madrid."
            )
        else:
            user_message = (
                "Perfect 😊 Ik heb je afspraakverzoek ontvangen, maar kon het nu niet automatisch registreren.\n"
                "Een technicus bekijkt dit en bevestigt je binnenkort."
            )

    elif command["type"] == "envio":
        dni_info = f"\nDNI/NIE/CIF: {command['dni_nie_cif']}" if command.get("dni_nie_cif") else ""
        email_info = f"\nE-mail: {command['email']}" if command.get("email") else ""
        created_event = await calendar_service.create_event(
            title=f"OPHALEN: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=f"{command['reason']}\nAdres: {command['address']}{dni_info}{email_info}",
            attendee_phone=attendee_phone,
        )

        payment_msg = (
            "Om de ophaalservice bij Correos aan te vragen, moet je *€30 (incl. btw)* betalen "
            "— ophalen + retourzending — via de volgende link:\n\n"
            "💳 https://sis.redsys.es/tiendaWeb/item/NDk4OzI=\n\n"
            "Stuur na de betaling het betalingsbewijs naar *soporte@kelatos.com*, dan regelen we de ophaalservice met Correos. 🚚"
        )

        if created_event:
            user_message = f"✅ Aanvraag geregistreerd!\n\n{payment_msg}"
        else:
            user_message = f"✅ We hebben je ophaalaanvraag ontvangen.\n\n{payment_msg}"

    elif command["type"] == "devolucion":
        resguardo_info = f"\nOntvangstbewijsnr.: {command['resguardo']}" if command.get("resguardo") else ""
        description = (
            f"Verzendadres: {command['address']}"
            f"{resguardo_info}\n"
            f"Telefoon klant: {attendee_phone}"
        )
        created_event = await calendar_service.create_event(
            title=f"RETOURZENDING: {command['customer_name']}",
            start_iso=command["datetime_iso"],
            duration_minutes=30,
            description=description,
            attendee_phone=attendee_phone,
        )

        if created_event:
            user_message = (
                f"✅ Je verzoek om retourzending is geregistreerd.\n\n"
                f"📦 Het toestel wordt verzonden naar: {command['address']}\n\n"
                f"Een medewerker van Kelatos neemt contact met je op om de betaling te regelen en de verzending te coördineren. Bedankt! 😊"
            )
        else:
            user_message = (
                "✅ We hebben je verzoek om retourzending ontvangen.\n\n"
                "Een medewerker van Kelatos neemt contact met je op om de betaling te regelen en de verzending te coördineren. Bedankt! 😊"
            )

    return user_message, created_event
