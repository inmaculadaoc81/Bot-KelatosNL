import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from faq_service import list_available_brands

logger = logging.getLogger(__name__)

CLASSIFIER_MODEL = "gpt-4o-mini"
CLASSIFIER_MAX_TOKENS = 100


@dataclass
class IntentResult:
    needs_repair_lookup: bool = False
    needs_prices: bool = False
    wants_appointment: bool = False
    needs_human: bool = False
    brand: str | None = None


async def classify_intent(
    client: AsyncOpenAI,
    user_message: str,
    history: list[dict] | None = None,
) -> IntentResult:
    """Lightweight classification to determine what context is needed."""
    brands = list_available_brands()
    brands_str = ", ".join(brands)

    system_prompt = (
        "Eres un clasificador para un bot de WhatsApp de un servicio técnico de reparaciones. "
        "Dado el mensaje del usuario, devuelve un JSON con estos campos:\n"
        '- "needs_repair_lookup": true si el usuario pregunta por el estado de su reparación, '
        "seguimiento, resguardo, o un equipo que dejó para reparar.\n"
        '- "needs_prices": true si el usuario pregunta por precios, costes, cuánto cuesta, '
        "tarifas, presupuesto, o información de precios de reparación. "
        "También true si menciona una marca/modelo específico y pregunta qué le costaría arreglarlo, "
        "qué precio tiene un servicio concreto, o cuánto vale una reparación o pieza. "
        "En caso de duda, marcar true.\n"
        '- "wants_appointment": true SOLO si el usuario pide EXPLICITAMENTE agendar/reservar/programar/coger una cita, '
        "o pide que le recojan el equipo a domicilio (recogida, mensajero, que pasen a buscarlo). "
        "IMPORTANTE: marcar FALSE si el usuario solo dice que va a ir, va a pasar, va a llevar, "
        "quiere acercarse, o pregunta por horarios/direccion/parking. Eso es walk-in (sin cita) y "
        "el bot no debe pedir datos. Solo true cuando el cliente usa la palabra 'cita', 'reservar', "
        "'agendar', 'recogida', 'recoger', 'mensajero' o equivalente claro.\n"
        '- "needs_human": true SOLO si el usuario pide EXPLICITAMENTE hablar con una persona real, '
        "un agente humano, quiere ser transferido, o expresa frustracion clara con el bot "
        "(ej: 'quiero hablar con alguien de verdad', 'pasame con una persona', 'no me entiendes'). "
        "IMPORTANTE: NO marcar needs_human cuando el usuario describe un problema tecnico, una averia, "
        "o quiere comprar un producto/pieza. Esos casos los maneja el bot. "
        "NUNCA marcar needs_human para preguntas sobre diagnostico, presupuesto, horarios, "
        "direccion, precios o servicios — esas preguntas las responde el bot directamente. "
        "Ejemplos que NO son needs_human: 'me podeis dar un diagnostico previo', "
        "'cuanto cuesta reparar', 'podeis diagnosticarlo', 'que precio tiene', "
        "'cuando abris', 'donde estais'.\n"
        '- "brand": el slug de la marca si el usuario menciona o pregunta sobre una marca específica. '
        f"Valores válidos: {brands_str}, o null si no menciona ninguna marca.\n"
        "Devuelve SOLO JSON válido, sin explicación."
    )

    messages = [{"role": "system", "content": system_prompt}]

    # Include last 2 history messages for follow-up context
    if history:
        for msg in history[-2:]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": user_message})

    try:
        response = await client.chat.completions.create(
            model=CLASSIFIER_MODEL,
            messages=messages,
            max_tokens=CLASSIFIER_MAX_TOKENS,
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        logger.info(f"Intent classification: {data}")

        result = IntentResult(
            needs_repair_lookup=bool(data.get("needs_repair_lookup", False)),
            needs_prices=bool(data.get("needs_prices", False)),
            wants_appointment=bool(data.get("wants_appointment", False)),
            needs_human=bool(data.get("needs_human", False)),
            brand=data.get("brand"),
        )

        # Validate brand against known list
        if result.brand and result.brand.lower() not in brands:
            logger.warning(f"Unknown brand '{result.brand}', ignoring")
            result.brand = None
        elif result.brand:
            result.brand = result.brand.lower()

        # Fallback: si el mensaje contiene palabras de precio y no se activó needs_prices, forzarlo.
        # Cubre casos donde gpt-4o-mini clasifica como consulta de reparación pero no de precio.
        if not result.needs_prices:
            _PRICE_KEYWORDS = (
                "precio", "coste", "cuesta", "cuanto", "cuánto",
                "tarifa", "presupuesto", "vale", "cobran", "cobráis",
                "reparación cuesta", "repair cost",
            )
            msg_lower = user_message.lower()
            if any(kw in msg_lower for kw in _PRICE_KEYWORDS):
                result.needs_prices = True
                logger.info("needs_prices forced True via keyword fallback")

        return result

    except Exception as e:
        logger.error(f"Intent classification failed: {e}", exc_info=True)
        # Fail-open: fetch everything (current behavior)
        return IntentResult(needs_repair_lookup=True, needs_prices=True, brand=None)
