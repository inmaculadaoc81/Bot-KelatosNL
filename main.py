import logging
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from config import settings

# Rate limiter: max requests per IP
limiter = Limiter(key_func=get_remote_address)

# Per-phone rate limit settings
PHONE_RATE_LIMIT = 10  # max messages per phone number
PHONE_RATE_WINDOW = 60  # in seconds

from database import Database
from openai_service import OpenAIService
from whatsapp_service import WhatsAppService
from sheets_service import SheetsService
from chatwoot_service import ChatwootService
from espocrm_service import EspoCRMService
from intent_classifier import classify_intent
from faq_service import load_brand_faq
from calendar_service import CalendarService, process_ai_calendar_command

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Services
db = Database()
openai_svc = OpenAIService()
whatsapp_svc = WhatsAppService()
sheets_svc = SheetsService()
chatwoot_svc = ChatwootService()
espocrm_svc = EspoCRMService()
calendar_svc = CalendarService()


# Marcadores del flujo de cita/recogida que aparecen en el último mensaje del bot
# cuando está pidiendo al cliente que confirme el resumen antes de registrar.
SURVEY_LABEL = "encuesta_reseña_pendiente"
SURVEY_RESPONSES = {"muy bueno", "bueno", "malo", "muy malo", "zeer goed", "goed", "slecht", "zeer slecht"}

APPOINTMENT_FLOW_MARKERS = (
    "Samenvatting van je afspraak",
    "Samenvatting van je ophaalaanvraag",
    "Samenvatting van je retourzending",
    "Klopt dit?",
)


def _is_in_appointment_flow(history: list[dict]) -> bool:
    """True si el último mensaje del bot pidió confirmación de cita/recogida.

    Sirve para rescatar turnos donde el cliente responde solo 'si' y el
    clasificador no detecta wants_appointment por falta de contexto.
    """
    if not history:
        return False
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "") or ""
            return any(m in content for m in APPOINTMENT_FLOW_MARKERS)
    return False


_BUDGET_DECISION_KEYWORDS = [
    # Nederlands
    "wijs af", "wijs de offerte af", "ik ga niet akkoord", "ik accepteer de offerte",
    "ik accepteer de reparatie", "ja akkoord", "ik ga akkoord",
    "ik wil niet dat het gerepareerd wordt", "annuleer de reparatie",
    "geen interesse in de reparatie", "ik wil de reparatie annuleren",
    # Spaans (compatibiliteit als klant in het Spaans schrijft)
    "rechaz", "no acepto", "no lo acepto", "acepto el presupuesto",
    "acepto la reparacion", "acepto la reparación", "sí acepto",
    "si acepto", "no quiero que lo reparen", "cancelar la reparacion",
    "cancelar la reparación", "no me interesa la reparacion",
    "no me interesa la reparación",
]
_BUDGET_CONTEXT_KEYWORDS = [
    "offerte", "reparatie", "prijs",
    "presupuesto", "reparacion", "reparación", "precio",
]

BUDGET_DECISION_RESPONSE = (
    "Om de offerte te accepteren of af te wijzen, moet je antwoorden op de e-mail "
    "waarin we deze naar je hebben gestuurd. Via WhatsApp kunnen we die bevestiging "
    "niet verwerken. 😊"
)


def _is_budget_decision(text: str, history: list[dict]) -> bool:
    """True si el mensaje es una aceptación o rechazo de presupuesto."""
    t = text.lower()
    if any(kw in t for kw in _BUDGET_DECISION_KEYWORDS):
        return True
    # Palabras sueltas necesitan contexto de presupuesto en el historial
    if any(kw in t for kw in ["afwijzen", "accepteren", "ik wil het niet", "ga door", "rechazarlo", "aceptarlo", "no lo quiero", "adelante"]):
        combined = " ".join(
            m.get("content", "") for m in (history or []) if m.get("content")
        ).lower()
        if any(kw in combined for kw in _BUDGET_CONTEXT_KEYWORDS):
            return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    await db.init()
    logger.info("Database initialized")
    await sheets_svc.connect()
    logger.info(f"Webhook verify token: {settings.VERIFY_TOKEN}")
    yield
    await db.close()
    logger.info("Database closed")


app = FastAPI(title="WhatsApp Bot API", version="1.0.1", lifespan=lifespan)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Return 429 when IP rate limit is exceeded."""
    logger.warning(f"IP rate limit exceeded: {get_remote_address(request)}")
    return JSONResponse(
        status_code=429,
        content={"error": "Too many requests. Try again later."},
    )


@app.get("/")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "whatsapp-bot"}


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta webhook verification.
    Meta sends a GET request with a challenge to verify your endpoint.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")

    logger.warning(f"Webhook verification failed. Mode: {hub_mode}")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
@limiter.limit("30/minute")
async def receive_message(request: Request):
    """
    Receive incoming WhatsApp messages from Meta.
    Must return 200 quickly to avoid Meta retrying.
    """
    body = await request.json()
    logger.info(f"Incoming webhook: {json.dumps(body, indent=2)}")

    try:
        # Extract message data from Meta's webhook format
        entry = body.get("entry", [])
        if not entry:
            return {"status": "no entry"}

        changes = entry[0].get("changes", [])
        if not changes:
            return {"status": "no changes"}

        value = changes[0].get("value", {})
        messages = value.get("messages", [])

        if not messages:
            # Could be a status update (delivered, read, etc.)
            logger.info("No messages in webhook (status update)")
            return {"status": "no messages"}

        message = messages[0]
        sender = message.get("from")  # Phone number of sender
        msg_type = message.get("type")

        # Only handle text messages for now
        if msg_type != "text":
            logger.info(f"Ignoring non-text message type: {msg_type}")
            if msg_type in ("image", "video"):
                response_text = (
                    "Bedankt voor je bericht! 😊 Ik begrijp dat je ons foto's of video's wilt sturen, "
                    "maar helaas kunnen we geen precieze technische diagnose stellen "
                    "op basis van alleen afbeeldingen of video's.\n\n"
                    "Om je toestel goed te kunnen beoordelen, moeten onze technici het "
                    "fysiek in de winkel hebben. 🔧\n\n"
                    "Het fijne is dat:\n"
                    "✅ De diagnose *GRATIS* is voor Dyson-toestellen\n"
                    "✅ Voor andere toestellen de kosten *€20 + btw* zijn, wat wordt afgetrokken als je besluit te repareren 💶\n\n"
                    "📌 Je kunt het toestel zonder afspraak naar de winkel 🏪 brengen (ma-vr 09:30-18:00), "
                    "of we kunnen het ophalen met onze *ophaalservice aan huis* 🚚 "
                    "(€15 ophalen + €15 retourzending, alleen vasteland van Spanje).\n\n"
                    "Wil je een ophaalservice inplannen, of heb je nog een vraag? 😊"
                )
            else:
                response_text = "Op dit moment kan ik alleen tekstberichten lezen. 😊"
            await whatsapp_svc.send_message(
                to=sender,
                text=response_text,
            )
            return {"status": "non-text ignored"}

        text = message["text"]["body"]
        logger.info(f"Message from {sender}: {text}")

        # Survey response interception — ignore chatbot processing when the
        # conversation has the encuesta_reseña_pendiente label and the client
        # replies with one of the four valid survey options.
        if text.strip().lower() in SURVEY_RESPONSES:
            conv_id = await chatwoot_svc.find_conversation_by_phone(sender)
            if conv_id:
                labels = await chatwoot_svc.get_conversation_labels(conv_id)
                if SURVEY_LABEL in labels:
                    logger.info(
                        f"Survey response '{text}' intercepted for {sender} "
                        f"(conversation {conv_id}, label: {SURVEY_LABEL})"
                    )
                    return {"status": "survey_response_ignored"}

        # Per-phone rate limiting
        recent = await db.count_recent_messages(sender, seconds=PHONE_RATE_WINDOW)
        if recent >= PHONE_RATE_LIMIT:
            logger.warning(f"Phone rate limit exceeded for {sender} ({recent} msgs in {PHONE_RATE_WINDOW}s)")
            await whatsapp_svc.send_message(
                to=sender,
                text="Je stuurt berichten erg snel. Wacht even voordat je verdergaat.",
            )
            return {"status": "phone_rate_limited"}

        # Check conversation mode (bot or human)
        mode = await db.get_conversation_mode(sender)

        if mode == "human":
            # In human mode, just save the message (agent sees it in panel)
            await db.save_message(sender, "user", text)
            logger.info(f"Message saved for human agent (sender: {sender})")
            return {"status": "human mode"}

        # Bot mode: get conversation history for context
        history = await db.get_history(sender, limit=20)

        # Detect returning session (gap > 4 hours)
        last_msg_time_wa = await db.get_last_message_time(sender)
        now_utc_wa = datetime.now(timezone.utc)
        session_context_wa = None
        if last_msg_time_wa:
            gap_hours = (now_utc_wa - last_msg_time_wa).total_seconds() / 3600
            if gap_hours >= 4:
                session_context_wa = (
                    f"[SESIÓN RETOMADA]\n"
                    f"Han pasado aproximadamente {gap_hours:.0f} horas desde el último mensaje del cliente.\n"
                    f"Revisa el historial para identificar el tema de la última consulta y saluda en consecuencia."
                )
                logger.info(f"Returning session detected for {sender} (gap: {gap_hours:.1f}h)")

        # Save user message
        await db.save_message(sender, "user", text)

        # Classify intent (cheap gpt-4o-mini call)
        intent = await classify_intent(
            client=openai_svc.client,
            user_message=text,
            history=history,
        )
        logger.info(f"Intent for {sender}: {intent}")
        logger.info(
            f"[PRICES] needs_prices={intent.needs_prices} | "
            f"PRICES_SHEET_ID_SET={bool(settings.GOOGLE_PRICES_SHEET_ID)}"
        )

        # Handoff to human agent if requested
        if intent.needs_human:
            if is_within_business_hours():
                await db.save_message(sender, "assistant", HANDOFF_MESSAGE)
                await whatsapp_svc.send_message(to=sender, text=HANDOFF_MESSAGE)
                await _handle_handoff(sender)
                return {"status": "handoff"}
            else:
                msg = get_outside_hours_message()
                await db.save_message(sender, "assistant", msg)
                await whatsapp_svc.send_message(to=sender, text=msg)
                logger.info(f"Handoff denied for {sender} — outside business hours")
                return {"status": "outside_hours"}

        # Fetch only what's needed based on classification
        extra_context_parts = []

        if session_context_wa:
            extra_context_parts.append(session_context_wa)

        needs_repair = intent.needs_repair_lookup
        if needs_repair:
            repair_ctx = await _repair_lookup(sender, text)
            if repair_ctx:
                extra_context_parts.append(repair_ctx)

        if intent.needs_prices:
            try:
                prices = await sheets_svc.get_all_prices()
                if prices:
                    extra_context_parts.append(sheets_svc.format_prices_for_prompt(prices))
            except Exception as e:
                logger.error(f"Error fetching prices: {e}", exc_info=True)

        if not intent.wants_appointment and _is_in_appointment_flow(history):
            intent.wants_appointment = True
            logger.info("Forced wants_appointment=True (active appointment flow detected in history)")

        if intent.wants_appointment:
            extra_context_parts.append(calendar_svc.get_appointment_context())

        extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else None

        # Load brand-specific FAQ if classified
        brand_faq = None
        if intent.brand:
            brand_faq = load_brand_faq(intent.brand)

        # Intercept: aceptación/rechazo de presupuesto — respuesta fija, sin LLM
        if _is_budget_decision(text, history):
            ai_response = BUDGET_DECISION_RESPONSE
            logger.info("Budget decision intercepted (WhatsApp), returning fixed response.")
        else:
        # Generate AI response
            ai_response = await openai_svc.generate_response(
                user_message=text,
                history=history,
                extra_context=extra_context,
                brand_faq=brand_faq,
            )
        logger.info("RAW AI RESPONSE (WhatsApp):\n%s", ai_response)

        # Check if AI wants to transfer to agent (product purchase)
        if "TRANSFERIR_AGENTE" in ai_response:
            if is_within_business_hours():
                clean_response = ai_response.replace("TRANSFERIR_AGENTE", "").strip()
                full_msg = f"{clean_response}\n\n{HANDOFF_MESSAGE}" if clean_response else HANDOFF_MESSAGE
                await db.save_message(sender, "assistant", full_msg)
                await whatsapp_svc.send_message(to=sender, text=full_msg)
                await _handle_handoff(sender)
            else:
                # Outside hours: discard "te transfiero" text, ask for contact instead
                full_msg = get_outside_hours_message()
                await db.save_message(sender, "assistant", full_msg)
                await whatsapp_svc.send_message(to=sender, text=full_msg)
            return {"status": "handoff"}

        # Check if AI confirmed an appointment or pickup
        clean_response, created_event = await process_ai_calendar_command(
            calendar_service=calendar_svc,
            ai_response=ai_response,
            attendee_phone=sender,
            history=history,
        )
        logger.info("CLEAN RESPONSE (WhatsApp):\n%s", clean_response)
        logger.info("CREATED EVENT (WhatsApp): %s", created_event)

        # Save bot response
        await db.save_message(sender, "assistant", clean_response)

        # Send response via WhatsApp
        await whatsapp_svc.send_message(to=sender, text=clean_response)

        logger.info(f"Response sent to {sender}: {clean_response[:100]}...")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing message: {e}", exc_info=True)
        return {"status": "error"}


HANDOFF_MESSAGE = (
    "🔄 Ik verbind je door met een collega van het *technische team*. "
    "Je wordt zo geholpen."
)

# Feestdagen Madrid/Spanje 2026 (jaarlijks bijwerken) — winkel bevindt zich in Madrid
HOLIDAYS_2026 = {
    # Nacionales
    "01-01", "01-06", "04-02", "04-03", "05-01",
    "08-15", "10-12", "11-02", "12-07", "12-08", "12-25",
    # Madrid
    "05-02", "05-15", "11-09",
}

WEEKDAY_NAMES = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _get_madrid_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid"))


def is_within_business_hours() -> bool:
    """Check if current time is within L-V 9:30-18:00 Madrid time, excluding holidays."""
    now = _get_madrid_now()
    if now.weekday() >= 5:
        return False
    if now.strftime("%m-%d") in HOLIDAYS_2026:
        return False
    current_time = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= current_time < 18 * 60


def get_outside_hours_message() -> str:
    """Build message with the next business day/time."""
    now = _get_madrid_now()
    current_time = now.hour * 60 + now.minute

    from datetime import timedelta

    # Find next business day/time
    if now.weekday() < 5 and now.strftime("%m-%d") not in HOLIDAYS_2026 and current_time < 9 * 60 + 30:
        when = "vandaag binnen onze openingstijden"
    else:
        next_day = now + timedelta(days=1)
        for _ in range(7):
            if next_day.weekday() < 5 and next_day.strftime("%m-%d") not in HOLIDAYS_2026:
                break
            next_day += timedelta(days=1)

        diff_days = (next_day.date() - now.date()).days
        if diff_days == 1:
            when = "morgen binnen onze openingstijden"
        else:
            day_name = WEEKDAY_NAMES[next_day.weekday()]
            when = f"op *{day_name}* binnen onze openingstijden"

    # Pedir contacto si el siguiente día laborable es lunes (viernes noche, sábado o domingo).
    # WhatsApp solo permite responder dentro de las 24h del último mensaje del cliente,
    # por lo que si escribe el fin de semana, para el lunes puede haber vencido esa ventana.
    next_day_is_monday = now.weekday() >= 4  # viernes=4, sábado=5, domingo=6
    if next_day_is_monday:
        contact_line = (
            "\n\nOmdat de winkel gesloten is tot maandag en WhatsApp alleen binnen 24 uur "
            "reageren toestaat, raad ik je aan om maandagochtend opnieuw te schrijven, "
            "of laat me je *naam* en *telefoonnummer* achter zodat we je kunnen bellen zodra we open zijn. 😊"
        )
    else:
        contact_line = "\n\nIn de tussentijd kan ik je verder helpen met al je vragen. 😊"

    return (
        f"🕐 Op dit moment zijn we buiten openingstijden. Een collega helpt je "
        f"{when}.{contact_line}"
    )


async def _handle_handoff(sender_key: str, conversation_id: int | None = None):
    """
    Transfer the conversation to a human agent.
    - If conversation_id is provided (Chatwoot webhook), use it directly.
    - Otherwise (WhatsApp webhook), search Chatwoot by phone.
    Sets mode to 'human' so the bot stops responding.
    """
    # Find conversation_id if not provided
    if conversation_id is None:
        conversation_id = await chatwoot_svc.find_conversation_by_phone(sender_key)

    # Toggle conversation to open in Chatwoot so agent sees it
    if conversation_id:
        try:
            await chatwoot_svc.handoff_to_agent(conversation_id)
        except Exception as e:
            logger.error(f"Failed to handoff conversation {conversation_id}: {e}", exc_info=True)

        # Auto-assign to handoff agent (round-robin: Iván/Daniela)
        try:
            assigned = await chatwoot_svc.assign_handoff_agent(conversation_id)
            if assigned:
                logger.info(f"Handoff agent {assigned} auto-assigned to conversation {conversation_id}")
        except Exception as e:
            logger.error(f"Failed to auto-assign agent for conversation {conversation_id}: {e}", exc_info=True)

    # Set mode to human so bot stops responding
    await db.set_conversation_mode(sender_key, "human")
    logger.info(f"Handoff complete for {sender_key} (conversation: {conversation_id})")


# ── Repair lookup helper ──────────────────────────────────────────────

_RESGUARDO_RE = re.compile(r"\b(\d{4,6})\b")


async def _repair_lookup(phone: str, message: str) -> str | None:
    """Try to find repair data for the user.

    Prioridad:
    1. Si el mensaje contiene un numero de resguardo (4-6 digitos), buscar
       por ese resguardo sin validar el telefono. Es el flujo principal:
       el cliente da su resguardo y el bot le dice el estado.
    2. Si no hay resguardo en el mensaje, intentar buscar por el telefono
       del remitente por si coincide (atajo opcional).
    3. Si no hay nada, pedir amablemente el resguardo.
    """
    # Step 1: resguardo directly in message → lookup without phone check
    match = _RESGUARDO_RE.search(message)
    if match:
        resguardo = match.group(1)
        try:
            repair = await sheets_svc.get_repair_by_resguardo(resguardo)
            if repair:
                logger.info(f"Found repair by resguardo {resguardo}")
                return sheets_svc.format_repairs_for_prompt([repair])
            else:
                logger.info(f"Resguardo {resguardo} not found in sheet")
                return (
                    "[ZOEKRESULTAAT ONTVANGSTBEWIJS]\n"
                    f"Er is geen ontvangstbewijs gevonden met nummer {resguardo}.\n"
                    "INSTRUCTIES: Laat de klant vriendelijk weten dat dit ontvangstbewijsnummer "
                    "niet gevonden kan worden en vraag hem het te controleren en opnieuw te versturen. "
                    "Blijft hij aandringen dat het correct is, bied dan aan door te verbinden met een collega."
                )
        except Exception as e:
            logger.error(f"Error fetching resguardo {resguardo}: {e}", exc_info=True)

    # Step 2: try by sender phone as a shortcut (in case registered from same number)
    try:
        repairs = await sheets_svc.get_repairs_by_phone(phone)
        if repairs:
            logger.info(f"Found {len(repairs)} repairs by phone for {phone}")
            return sheets_svc.format_repairs_for_prompt(repairs)
    except Exception as e:
        logger.error(f"Error fetching repairs by phone: {e}", exc_info=True)

    # Step 3: nothing found → ask for resguardo
    return (
        "[ZOEKRESULTAAT REPARATIES]\n"
        "Er zijn geen reparaties gevonden voor deze klant.\n"
        "INSTRUCTIES: Vraag de klant om zijn ontvangstbewijsnummer (4 tot 6 cijfers "
        "die op het papier/de e-mail staan die hij kreeg bij het achterlaten van het toestel) "
        "om de status te kunnen opzoeken."
    )


# ── Chatwoot Agent Bot webhook ──────────────────────────────────────────


@app.post("/chatwoot/webhook")
@limiter.limit("30/minute")
async def chatwoot_webhook(request: Request):
    """
    Receive incoming events from Chatwoot Agent Bot.
    Chatwoot sends message_created events when a customer writes.
    """
    body = await request.json()
    logger.info(f"Chatwoot webhook: {json.dumps(body, indent=2)}")

    try:
        event = body.get("event")
        message_type = body.get("message_type")

        # Handle conversation resolved → restore bot mode
        if event == "conversation_status_changed":
            status = body.get("status")
            if status == "resolved":
                conversation = body.get("id") or body.get("conversation", {}).get("id")
                if conversation:
                    sender_key = f"chatwoot_{conversation}"
                    await db.set_conversation_mode(sender_key, "bot")
                    logger.info(f"Conversation {conversation} resolved — bot mode restored")

                    # Also restore by phone if available
                    contact_inbox = body.get("contact_inbox", {})
                    source_id = contact_inbox.get("source_id")
                    if source_id:
                        await db.set_conversation_mode(source_id, "bot")
                        logger.info(f"Bot mode restored for phone {source_id}")

                return {"status": "bot_restored"}
            return {"status": "ignored"}

        # Handle agent assigned/unassigned → toggle human/bot mode
        if event == "conversation_updated":
            # changed_attributes is an array of dicts, merge into one
            changed_list = body.get("changed_attributes", [])
            changed = {}
            for item in changed_list:
                changed.update(item)

            if "assignee_id" in changed:
                assignee = changed["assignee_id"]
                current = assignee.get("current_value")
                previous = assignee.get("previous_value")
                conversation_id = body.get("id")

                if conversation_id:
                    sender_key = f"chatwoot_{conversation_id}"
                    contact_inbox = body.get("contact_inbox", {})
                    source_id = contact_inbox.get("source_id")

                    if current and not previous:
                        # Agent assigned → human mode
                        await db.set_conversation_mode(sender_key, "human")
                        if source_id:
                            await db.set_conversation_mode(source_id, "human")
                        logger.info(f"Agent {current} assigned to conversation {conversation_id} — human mode")
                        return {"status": "agent_assigned"}

                    elif not current and previous:
                        # Agent unassigned → restore bot mode
                        await db.set_conversation_mode(sender_key, "bot")
                        if source_id:
                            await db.set_conversation_mode(source_id, "bot")
                        logger.info(f"Agent unassigned from conversation {conversation_id} — bot mode restored")
                        return {"status": "agent_unassigned"}

            return {"status": "ignored"}

        # Only process incoming messages (from the customer)
        if event != "message_created" or message_type != "incoming":
            # Agent sent a message → activate human mode so bot doesn't interfere
            # But ignore messages from the bot itself (sender type "agent_bot")
            if event == "message_created" and message_type == "outgoing":
                sender_info = body.get("sender", {})
                sender_type = sender_info.get("type", "")
                if sender_type == "agent_bot":
                    logger.info(f"Ignoring bot's own outgoing message")
                    return {"status": "bot_echo_ignored"}

                conversation = body.get("conversation", {})
                conv_id = conversation.get("id")
                if conv_id:
                    sender_key = f"chatwoot_{conv_id}"
                    await db.set_conversation_mode(sender_key, "human")

                    # Also set by phone so WhatsApp webhook respects it
                    contact_inbox = conversation.get("contact_inbox", {})
                    source_id = contact_inbox.get("source_id")
                    if source_id:
                        await db.set_conversation_mode(source_id, "human")

                    logger.info(f"Agent message detected — human mode set for conversation {conv_id}")
                    return {"status": "agent_mode"}

            logger.info(f"Ignoring Chatwoot event: {event}, type: {message_type}")
            return {"status": "ignored"}

        # Extract data from Chatwoot payload
        content = body.get("content", "")
        conversation = body.get("conversation", {})
        conversation_id = conversation.get("id")
        sender = body.get("sender", {})
        contact_id = sender.get("id")

        if not conversation_id:
            logger.warning("Missing conversation_id in Chatwoot webhook")
            return {"status": "missing data"}

        # Detect attachments (images, audio, video, files) or empty content
        attachments = body.get("attachments", [])
        if attachments or not content:
            if attachments:
                file_type = attachments[0].get("file_type", "file")
                logger.info(f"Received {file_type} attachment in conversation {conversation_id}")
            else:
                logger.info(f"Empty content in conversation {conversation_id}")
            await chatwoot_svc.send_message(
                conversation_id,
                "Op dit moment kan ik alleen tekstberichten lezen. 😊 Zou je je vraag in woorden kunnen omschrijven?",
            )
            return {"status": "non-text ignored"}

        # Ignore non-text content_type
        content_type = body.get("content_type", "text")
        if content_type != "text":
            logger.info(f"Ignoring non-text content_type: {content_type}")
            await chatwoot_svc.send_message(
                conversation_id,
                "Op dit moment kan ik alleen tekstberichten lezen. 😊 Zou je je vraag in woorden kunnen omschrijven?",
            )
            return {"status": "non-text ignored"}

        # Get the customer's phone number for repair lookups
        # First try source_id from contact_inbox (WhatsApp number)
        phone = None
        contact_inbox = conversation.get("contact_inbox", {})
        source_id = contact_inbox.get("source_id")
        if source_id:
            phone = source_id
            logger.info(f"Phone from source_id: {phone}")

        # Fallback: fetch phone from Chatwoot Contacts API
        if not phone and contact_id:
            phone = await chatwoot_svc.get_contact_phone(contact_id)
            logger.info(f"Phone from Contacts API: {phone}")

        # Use conversation_id as the "sender" key for mode/rate tracking
        sender_key = f"chatwoot_{conversation_id}"
        # Use phone as history key when available so context persists across
        # new conversations (new conversation_id) from the same client.
        history_key = phone if phone else sender_key

        # Per-phone rate limiting
        recent = await db.count_recent_messages(sender_key, seconds=PHONE_RATE_WINDOW)
        if recent >= PHONE_RATE_LIMIT:
            logger.warning(f"Rate limit exceeded for conversation {conversation_id} ({recent} msgs in {PHONE_RATE_WINDOW}s)")
            await chatwoot_svc.send_message(
                conversation_id,
                "Estás enviando mensajes muy rápido. Por favor espera un momento antes de continuar.",
            )
            return {"status": "phone_rate_limited"}

        # Check conversation mode (bot or human)
        mode = await db.get_conversation_mode(sender_key)

        if mode == "human":
            await db.save_message(history_key, "user", content)
            logger.info(f"Message saved for human agent (conversation: {conversation_id})")
            return {"status": "human mode"}

        # Get conversation history
        history = await db.get_history(history_key, limit=20)

        # Si la conversacion es nueva (sin historial) o lleva mas de
        # ESPOCRM_NEW_LEAD_AFTER_SECONDS inactiva, programar un volcado
        # diferido de la conversacion completa hacia EspoCRM.
        last_msg_time = await db.get_last_message_time(history_key)
        now_utc = datetime.now(timezone.utc)
        gap = (now_utc - last_msg_time).total_seconds() if last_msg_time else None
        is_new_session = last_msg_time is None or (
            gap is not None and gap > settings.ESPOCRM_NEW_LEAD_AFTER_SECONDS
        )
        logger.info(
            f"EspoCRM check for {sender_key}: last_msg={last_msg_time}, "
            f"gap={gap}s, threshold={settings.ESPOCRM_NEW_LEAD_AFTER_SECONDS}s, "
            f"is_new_session={is_new_session}"
        )
        if is_new_session:
            sender_name = sender.get("name", "")
            sender_email = sender.get("email", "")
            if not phone:
                phone = sender.get("phone_number", "")
            lead_label = sender_name or phone or f"Gesprek {conversation_id}"
            try:
                espocrm_svc.schedule_lead_from_conversation(
                    db=db,
                    sender_key=history_key,
                    lead_label=lead_label,
                    contact_name=sender_name,
                    phone=phone or "",
                    email=sender_email,
                    session_started_at=now_utc,
                )
            except Exception as e:
                logger.error(f"Error scheduling EspoCRM lead: {e}", exc_info=True)

        # Save user message
        await db.save_message(history_key, "user", content)

        # Classify intent (cheap gpt-4o-mini call)
        intent = await classify_intent(
            client=openai_svc.client,
            user_message=content,
            history=history,
        )
        logger.info(f"Intent for conversation {conversation_id}: {intent}")
        logger.info(
            f"[PRICES] needs_prices={intent.needs_prices} | "
            f"PRICES_SHEET_ID_SET={bool(settings.GOOGLE_PRICES_SHEET_ID)}"
        )

        # Handoff to human agent if requested
        if intent.needs_human:
            if is_within_business_hours():
                await db.save_message(history_key, "assistant", HANDOFF_MESSAGE)
                await chatwoot_svc.send_message(conversation_id, HANDOFF_MESSAGE)
                await _handle_handoff(sender_key, conversation_id=conversation_id)
                return {"status": "handoff"}
            else:
                msg = get_outside_hours_message()
                await db.save_message(history_key, "assistant", msg)
                await chatwoot_svc.send_message(conversation_id, msg)
                logger.info(f"Handoff denied for conversation {conversation_id} — outside business hours")
                return {"status": "outside_hours"}

        # Fetch only what's needed based on classification
        extra_context_parts = []

        # Detect returning session (gap > 4 hours)
        if last_msg_time and gap is not None and gap >= 4 * 3600:
            gap_hours = gap / 3600
            extra_context_parts.append(
                f"[SESIÓN RETOMADA]\n"
                f"Han pasado aproximadamente {gap_hours:.0f} horas desde el último mensaje del cliente.\n"
                f"Revisa el historial para identificar el tema de la última consulta y saluda en consecuencia."
            )
            logger.info(f"Returning session detected for {sender_key} (gap: {gap_hours:.1f}h)")

        needs_repair = intent.needs_repair_lookup
        if needs_repair and phone:
            repair_ctx = await _repair_lookup(phone, content)
            if repair_ctx:
                extra_context_parts.append(repair_ctx)

        if intent.needs_prices:
            try:
                prices = await sheets_svc.get_all_prices()
                if prices:
                    extra_context_parts.append(sheets_svc.format_prices_for_prompt(prices))
            except Exception as e:
                logger.error(f"Error fetching prices: {e}", exc_info=True)

        if not intent.wants_appointment and _is_in_appointment_flow(history):
            intent.wants_appointment = True
            logger.info("Forced wants_appointment=True (active appointment flow detected in history)")

        if intent.wants_appointment:
            extra_context_parts.append(calendar_svc.get_appointment_context())

        extra_context = "\n\n".join(extra_context_parts) if extra_context_parts else None

        # Load brand-specific FAQ if classified
        brand_faq = None
        if intent.brand:
            brand_faq = load_brand_faq(intent.brand)

        # Intercept: aceptación/rechazo de presupuesto — respuesta fija, sin LLM
        if _is_budget_decision(content, history):
            ai_response = BUDGET_DECISION_RESPONSE
            logger.info("Budget decision intercepted (Chatwoot), returning fixed response.")
        else:
        # Generate AI response
            ai_response = await openai_svc.generate_response(
                user_message=content,
                history=history,
                extra_context=extra_context,
                brand_faq=brand_faq,
            )
        logger.info("RAW AI RESPONSE (Chatwoot):\n%s", ai_response)

        # Check if AI wants to transfer to agent (product purchase)
        if "TRANSFERIR_AGENTE" in ai_response:
            if is_within_business_hours():
                clean_response = ai_response.replace("TRANSFERIR_AGENTE", "").strip()
                full_msg = f"{clean_response}\n\n{HANDOFF_MESSAGE}" if clean_response else HANDOFF_MESSAGE
                await db.save_message(history_key, "assistant", full_msg)
                await chatwoot_svc.send_message(conversation_id, full_msg)
                await _handle_handoff(sender_key, conversation_id=conversation_id)
            else:
                # Outside hours: discard "te transfiero" text, ask for contact instead
                full_msg = get_outside_hours_message()
                await db.save_message(history_key, "assistant", full_msg)
                await chatwoot_svc.send_message(conversation_id, full_msg)
            return {"status": "handoff"}

        # Check if AI confirmed an appointment or pickup
        clean_response, created_event = await process_ai_calendar_command(
            calendar_service=calendar_svc,
            ai_response=ai_response,
            attendee_phone=phone or sender_key,
            history=history,
        )
        logger.info("CLEAN RESPONSE (Chatwoot):\n%s", clean_response)
        logger.info("CREATED EVENT (Chatwoot): %s", created_event)

        # Save bot response
        await db.save_message(history_key, "assistant", clean_response)

        # Send response via Chatwoot
        await chatwoot_svc.send_message(conversation_id, clean_response)

        logger.info(f"Response sent to conversation {conversation_id}: {clean_response[:100]}...")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing Chatwoot webhook: {e}", exc_info=True)
        return {"status": "error"}
