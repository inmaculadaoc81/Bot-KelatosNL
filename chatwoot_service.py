import logging
import httpx

from config import settings

logger = logging.getLogger(__name__)


class ChatwootService:
    """Service for interacting with Chatwoot API as an Agent Bot."""

    def __init__(self):
        base = settings.CHATWOOT_URL.rstrip("/")
        self.account_id = settings.CHATWOOT_ACCOUNT_ID
        self.api_base = f"{base}/api/v1/accounts/{self.account_id}"
        self.headers = {
            "api_access_token": settings.CHATWOOT_BOT_TOKEN,
            "Content-Type": "application/json",
        }
        self.admin_headers = {
            "api_access_token": settings.CHATWOOT_ADMIN_TOKEN,
            "Content-Type": "application/json",
        }
        self._handoff_agent_ids = [
            int(x) for x in settings.CHATWOOT_HANDOFF_AGENT_IDS.split(",") if x.strip()
        ]
        self._last_assigned_index = -1

    async def send_message(self, conversation_id: int, text: str) -> dict:
        """
        Send a text message to a Chatwoot conversation.
        Chatwoot then delivers it to the customer via WhatsApp.
        """
        url = f"{self.api_base}/conversations/{conversation_id}/messages"
        payload = {
            "content": text,
            "message_type": "outgoing",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    url, json=payload, headers=self.headers
                )
                response.raise_for_status()
                data = response.json()
                logger.info(f"Message sent to conversation {conversation_id}")
                return data

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Chatwoot API error: {e.response.status_code} - {e.response.text}"
            )
            raise
        except Exception as e:
            logger.error(f"Error sending Chatwoot message: {e}", exc_info=True)
            raise

    async def get_contact_phone(self, contact_id: int) -> str | None:
        """
        Get a contact's phone number from Chatwoot.
        Used as fallback when source_id is not available.
        """
        url = f"{self.api_base}/contacts/{contact_id}"

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                phone = data.get("phone_number") or data.get("payload", {}).get("phone_number")
                if phone:
                    logger.info(f"Got phone {phone} for contact {contact_id}")
                return phone

        except Exception as e:
            logger.error(f"Error fetching contact {contact_id}: {e}", exc_info=True)
            return None

    async def find_conversation_by_phone(self, phone: str) -> int | None:
        """
        Search Chatwoot for a conversation belonging to the given phone number.
        Returns the most recent conversation_id, or None if not found.
        """
        search_url = f"{self.api_base}/contacts/search"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Search contact by phone
                resp = await client.get(
                    search_url,
                    params={"q": phone},
                    headers=self.headers,
                )
                resp.raise_for_status()
                contacts = resp.json().get("payload", [])

                if not contacts:
                    logger.info(f"No Chatwoot contact found for phone {phone}")
                    return None

                contact_id = contacts[0]["id"]

                # Get conversations for this contact
                conv_url = f"{self.api_base}/contacts/{contact_id}/conversations"
                resp2 = await client.get(conv_url, headers=self.headers)
                resp2.raise_for_status()
                conversations = resp2.json().get("payload", [])

                if not conversations:
                    logger.info(f"No conversations found for contact {contact_id}")
                    return None

                # Return the most recent conversation
                conv_id = conversations[0]["id"]
                logger.info(f"Found Chatwoot conversation {conv_id} for phone {phone}")
                return conv_id

        except Exception as e:
            logger.error(f"Error searching Chatwoot conversation for {phone}: {e}", exc_info=True)
            return None

    async def get_agent_availability(self) -> dict[int, str]:
        """
        Get availability status of handoff agents.
        Returns {agent_id: "available"|"busy"|"offline"}.
        """
        url = f"{self.api_base}/agents"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, headers=self.admin_headers)
                response.raise_for_status()
                agents = response.json()
                return {
                    a["id"]: a.get("availability_status", "offline")
                    for a in agents
                    if a["id"] in self._handoff_agent_ids
                }
        except Exception as e:
            logger.error(f"Error fetching agent availability: {e}", exc_info=True)
            return {}

    async def assign_handoff_agent(self, conversation_id: int) -> int | None:
        """
        Assign conversation to the next available handoff agent (round-robin).
        Prefers online agents. Returns assigned agent_id or None.
        """
        if not self._handoff_agent_ids:
            return None

        availability = await self.get_agent_availability()

        # Try round-robin starting from next agent
        agent_ids = self._handoff_agent_ids
        n = len(agent_ids)
        start = (self._last_assigned_index + 1) % n

        # First pass: prefer online agents
        for i in range(n):
            idx = (start + i) % n
            agent_id = agent_ids[idx]
            if availability.get(agent_id) == "available":
                assigned = await self._assign_agent(conversation_id, agent_id)
                if assigned:
                    self._last_assigned_index = idx
                    return agent_id

        # Second pass: assign next in round-robin regardless of status
        for i in range(n):
            idx = (start + i) % n
            agent_id = agent_ids[idx]
            assigned = await self._assign_agent(conversation_id, agent_id)
            if assigned:
                self._last_assigned_index = idx
                return agent_id

        return None

    async def _assign_agent(self, conversation_id: int, agent_id: int) -> bool:
        """Assign a specific agent to a conversation."""
        url = f"{self.api_base}/conversations/{conversation_id}/assignments"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    url,
                    json={"assignee_id": agent_id},
                    headers=self.admin_headers,
                )
                response.raise_for_status()
                logger.info(f"Agent {agent_id} assigned to conversation {conversation_id}")
                return True
        except Exception as e:
            logger.error(f"Error assigning agent {agent_id} to conversation {conversation_id}: {e}", exc_info=True)
            return False

    async def get_conversation_labels(self, conversation_id: int) -> list[str]:
        """Get current labels of a Chatwoot conversation."""
        url = f"{self.api_base}/conversations/{conversation_id}/labels"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(url, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                payload = data.get("payload", [])
                return [str(lbl).strip().lower() for lbl in payload] if isinstance(payload, list) else []
        except Exception as e:
            logger.error(f"Error fetching labels for conversation {conversation_id}: {e}", exc_info=True)
            return []

    async def handoff_to_agent(self, conversation_id: int) -> dict:
        """
        Toggle conversation status to 'open' so a human agent takes over.
        """
        url = f"{self.api_base}/conversations/{conversation_id}/toggle_status"
        payload = {"status": "open"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    url, json=payload, headers=self.headers
                )
                response.raise_for_status()
                logger.info(f"Conversation {conversation_id} handed off to agent")
                return response.json()

        except Exception as e:
            logger.error(f"Error toggling status: {e}", exc_info=True)
            raise
