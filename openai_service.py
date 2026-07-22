import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from config import settings
from faq_service import load_general_faq

_MADRID_TZ = ZoneInfo("Europe/Madrid")
_DIAS_SEMANA = [
    "maandag", "dinsdag", "woensdag", "donderdag",
    "vrijdag", "zaterdag", "zondag",
]
_MESES = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

# Keep in sync with HOLIDAYS_2026 in main.py
_HOLIDAYS = {
    # Nacionales
    "01-01", "01-06", "04-02", "04-03", "05-01",
    "08-15", "10-12", "11-02", "12-07", "12-08", "12-25",
    # Madrid
    "05-02", "05-15", "11-09",
}


def _now_madrid() -> tuple[str, str]:
    now = datetime.now(_MADRID_TZ)
    fecha = f"{_DIAS_SEMANA[now.weekday()]} {now.day} de {_MESES[now.month - 1]} de {now.year}"
    hora = now.strftime("%H:%M")
    return fecha, hora


def _is_business_day(dt: datetime) -> bool:
    return dt.weekday() < 5 and dt.strftime("%m-%d") not in _HOLIDAYS


def _build_temporal_context() -> str:
    now = datetime.now(_MADRID_TZ)
    fecha = f"{_DIAS_SEMANA[now.weekday()]} {now.day} de {_MESES[now.month - 1]} de {now.year}"
    hora = now.strftime("%H:%M")
    mins = now.hour * 60 + now.minute

    is_open = _is_business_day(now) and (9 * 60 + 30) <= mins < 18 * 60
    estado = "OPEN" if is_open else "GESLOTEN"

    # Compute next opening moment
    if _is_business_day(now) and mins < 9 * 60 + 30:
        proximo = "vandaag om 09:30"
    else:
        candidate = now + timedelta(days=1)
        for _ in range(14):
            if _is_business_day(candidate):
                break
            candidate += timedelta(days=1)
        diff = (candidate.date() - now.date()).days
        day_name = _DIAS_SEMANA[candidate.weekday()]
        day_num = candidate.day
        month_name = _MESES[candidate.month - 1]
        if diff == 1:
            proximo = f"morgen {day_name} {day_num} {month_name} om 09:30"
        else:
            proximo = f"op {day_name} {day_num} {month_name} om 09:30"

    ctx = (
        f"\n\n[TIJDCONTEXT]\n"
        f"Huidige datum: {fecha}\n"
        f"Huidige tijd: {hora}\n"
        f"Tijdzone: Europe/Madrid\n"
        f"Status winkel: {estado} (openingstijden ma-vr 09:30-18:00)\n"
        f"Volgende opening: {proximo}\n"
        f"BELANGRIJK: Gebruik UITSLUITEND deze gegevens om de huidige dag en tijd te bepalen. "
        f"Bereken of veronderstel GEEN feestdagen of weekdagen die afwijken van wat hier staat."
    )
    return ctx

logger = logging.getLogger(__name__)

# Patterns for generic filler phrases that gpt-4o-mini likes to append
_FILLER_PATTERNS = [
    # Nederlands
    r"als je iets nodig hebt.*",
    r"aarzel niet om.*",
    r"ik sta voor je klaar.*",
    r"heb je nog iets nodig.*",
    r"nog iets anders.*",
    r"als je nog vragen hebt.*",
    r"laat het gerust weten.*",
    r"ik hoor het graag.*",
    r"ik sta tot je beschikking.*",
    # Spaans (por compatibilidad si el modelo responde en español)
    r"si necesitas.*",
    r"no dudes en.*",
    r"estoy aquí para.*",
    r"¿necesitas algo.*",
    r"¿algo más.*",
    r"si tienes alguna.*",
    r"no dude en.*",
    r"cualquier cosa.*avis.*",
    r"quedo a tu disposición.*",
    r"quedo atento.*",
]
_FILLER_RE = re.compile(
    r"[\.\!\s]*\s*(" + "|".join(_FILLER_PATTERNS) + r")\s*$",
    re.IGNORECASE,
)


def _strip_filler(text: str) -> str:
    """Remove generic closing filler phrases from the end of a response."""
    cleaned = _FILLER_RE.sub("", text).rstrip()
    # Ensure it ends with proper punctuation
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


class OpenAIService:
    """Service for generating AI responses using OpenAI."""

    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self.model = settings.OPENAI_MODEL
        self._general_faq = load_general_faq()

    async def generate_response(
        self,
        user_message: str,
        history: list[dict] | None = None,
        extra_context: str | None = None,
        brand_faq: str | None = None,
    ) -> str:
        """
        Generate a response using OpenAI.

        Args:
            user_message: The user's message
            history: Previous messages for context
            extra_context: Additional context (e.g. repair data from sheets)

        Returns:
            The AI-generated response text
        """
        try:
            # Construimos system_content con el bloque mas estable primero (favorece la cache de OpenAI):
            # 1. SYSTEM_PROMPT (estatico, ~9k tokens)
            # 2. FAQ general (estatico, ~2k tokens)
            # 3. FAQ de marca (estatico por marca)
            # 4. extra_context (variable: reparaciones/precios/citas)
            # 5. CONTEXTO TEMPORAL (cambia cada minuto - va al final para no romper la cache)
            system_content = settings.SYSTEM_PROMPT
            if self._general_faq:
                system_content += "\n\n" + self._general_faq
            if brand_faq:
                system_content += "\n\n[SPECIFIEKE MERK-FAQ]\n" + brand_faq
            if extra_context:
                system_content += "\n\n" + extra_context

            system_content += _build_temporal_context()

            messages = [{"role": "system", "content": system_content}]

            # Add conversation history for context
            if history:
                for msg in history:
                    messages.append({
                        "role": msg["role"],
                        "content": msg["content"],
                    })

            # Add the new user message
            messages.append({"role": "user", "content": user_message})

            # Call OpenAI API
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=700,
                temperature=0.7,
            )

            reply = response.choices[0].message.content.strip()
            reply = _strip_filler(reply)
            logger.info(f"OpenAI response ({self.model}): {reply[:100]}...")
            return reply

        except Exception as e:
            logger.error(f"OpenAI API error: {e}", exc_info=True)
            return "Sorry, ik ondervind op dit moment technische problemen. Kun je het over een paar minuten opnieuw proberen?"
