import asyncio
import logging
from datetime import datetime
import httpx

from config import settings

logger = logging.getLogger(__name__)


class EspoCRMService:
    """Service for creating records in a custom EspoCRM entity (API key auth)."""

    def __init__(self):
        self.url = settings.ESPOCRM_URL.rstrip("/")
        self.api_key = settings.ESPOCRM_API_KEY
        self.entity = settings.ESPOCRM_ENTITY
        # Retener referencias fuertes a tasks pendientes para que asyncio
        # no las recolecte antes de que terminen (el event loop solo guarda
        # referencias debiles).
        self._pending_tasks: set[asyncio.Task] = set()

    async def create_lead(
        self,
        name: str,
        contact_name: str = "",
        phone: str = "",
        email: str = "",
        description: str = "",
    ) -> str | None:
        """Create a record in the configured EspoCRM entity. Returns the record ID or None."""
        if not self.url or not self.api_key:
            logger.warning("EspoCRM not configured (ESPOCRM_URL/ESPOCRM_API_KEY missing)")
            return None

        payload = {
            "name": name,
            "contactName": contact_name,
            "phone": phone,
            "email": email,
            "description": description,
            "source": "WhatsApp",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.url}/api/v1/{self.entity}",
                    headers={
                        "X-Api-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                record_id = resp.json().get("id")
                logger.info(
                    f"EspoCRM {self.entity} record created: id={record_id}, name={name}"
                )
                return record_id
        except httpx.HTTPStatusError as e:
            logger.error(
                f"EspoCRM HTTP error on {self.entity}: "
                f"{e.response.status_code} - {e.response.text}"
            )
            return None
        except Exception as e:
            logger.error(f"Error creating EspoCRM {self.entity} record: {e}", exc_info=True)
            return None

    def schedule_lead_from_conversation(
        self,
        db,
        sender_key: str,
        lead_label: str,
        contact_name: str,
        phone: str,
        email: str,
        session_started_at: datetime,
        delay_seconds: int | None = None,
    ) -> asyncio.Task:
        """Fire-and-forget: after `delay_seconds`, dump the messages of this session
        (those created at or after `session_started_at`) and create a lead in
        EspoCRM. Called when a new session begins."""
        delay = delay_seconds if delay_seconds is not None else settings.ESPOCRM_LEAD_DELAY_SECONDS

        async def _run():
            try:
                logger.info(
                    f"EspoCRM scheduler armed for {sender_key} "
                    f"(fires in {delay}s, session_started_at={session_started_at.isoformat()})"
                )
                await asyncio.sleep(delay)
                logger.info(f"EspoCRM scheduler firing for {sender_key}")
                history = await db.get_history_since(
                    sender_key, since=session_started_at, limit=1000
                )
                logger.info(
                    f"EspoCRM scheduler: {len(history)} messages in session for {sender_key}"
                )
                transcript_lines = []
                for msg in history:
                    role = "Klant" if msg.get("role") == "user" else "Assistent"
                    transcript_lines.append(f"{role}: {msg.get('content', '')}")
                description = "\n".join(transcript_lines) if transcript_lines else ""

                await self.create_lead(
                    name=f"WhatsApp NL - {lead_label}",
                    contact_name=contact_name,
                    phone=phone,
                    email=email,
                    description=description,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"Error scheduling EspoCRM lead for {sender_key}: {e}",
                    exc_info=True,
                )

        task = asyncio.create_task(_run())
        # Mantener referencia fuerte hasta que termine (evita GC temprano).
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task
