import logging
import httpx

from config import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Service for sending messages via WhatsApp Cloud API."""

    def __init__(self):
        self.api_url = (
            f"https://graph.facebook.com/{settings.GRAPH_API_VERSION}"
            f"/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        )
        self.headers = {
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }

    async def send_message(self, to: str, text: str) -> dict:
        """
        Send a text message via WhatsApp.

        Args:
            to: Recipient phone number (with country code, no +)
            text: Message text

        Returns:
            API response dict
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers=self.headers,
                )
                response.raise_for_status()
                data = response.json()
                logger.info(f"Message sent to {to}: {data}")
                return data

        except httpx.HTTPStatusError as e:
            logger.error(
                f"WhatsApp API error: {e.response.status_code} - {e.response.text}"
            )
            raise
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}", exc_info=True)
            raise

    async def send_template(self, to: str, template_name: str, language: str = "nl") -> dict:
        """
        Send a template message (for initiating conversations).

        Args:
            to: Recipient phone number
            template_name: Name of the approved template
            language: Language code

        Returns:
            API response dict
        """
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers=self.headers,
                )
                response.raise_for_status()
                return response.json()

        except Exception as e:
            logger.error(f"Error sending template: {e}", exc_info=True)
            raise
