import aiosqlite
import os
from datetime import datetime, timezone, timedelta

from config import settings


class Database:
    """SQLite database for storing chat history and conversation state."""

    def __init__(self):
        self.db_path = settings.DATABASE_PATH

    async def init(self):
        """Create tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            # Messages table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_number TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

            # Conversations table (tracks mode: bot or human)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    phone_number TEXT PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'bot',
                    updated_at TEXT NOT NULL
                )
            """)

            # Index for faster queries
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_phone 
                ON messages(phone_number, created_at DESC)
            """)

            await db.commit()

    async def close(self):
        """Cleanup (aiosqlite handles connections per-query)."""
        pass

    async def save_message(self, phone_number: str, role: str, content: str):
        """Save a message to the history."""
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages (phone_number, role, content, created_at) VALUES (?, ?, ?, ?)",
                (phone_number, role, content, now),
            )

            # Update conversation record
            await db.execute(
                """INSERT INTO conversations (phone_number, mode, updated_at) 
                   VALUES (?, 'bot', ?)
                   ON CONFLICT(phone_number) DO UPDATE SET updated_at = ?""",
                (phone_number, now, now),
            )

            await db.commit()

    async def get_history(self, phone_number: str, limit: int = 10) -> list[dict]:
        """Get recent conversation history for a phone number."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT role, content FROM messages 
                   WHERE phone_number = ? 
                   ORDER BY created_at DESC 
                   LIMIT ?""",
                (phone_number, limit),
            )
            rows = await cursor.fetchall()

            # Reverse to get chronological order
            return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def get_history_since(
        self, phone_number: str, since: datetime, limit: int = 1000
    ) -> list[dict]:
        """Get messages for this sender created at-or-after `since`, chronological."""
        since_iso = since.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT role, content FROM messages
                   WHERE phone_number = ? AND created_at >= ?
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (phone_number, since_iso, limit),
            )
            rows = await cursor.fetchall()
            return [{"role": row["role"], "content": row["content"]} for row in rows]

    async def get_last_message_time(self, phone_number: str) -> datetime | None:
        """Return UTC datetime of the most recent message for this sender, or None."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT created_at FROM messages
                   WHERE phone_number = ?
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (phone_number,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return datetime.fromisoformat(row[0])

    async def get_conversation_mode(self, phone_number: str) -> str:
        """Get the current mode for a conversation (bot or human)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT mode FROM conversations WHERE phone_number = ?",
                (phone_number,),
            )
            row = await cursor.fetchone()
            return row["mode"] if row else "bot"

    async def set_conversation_mode(self, phone_number: str, mode: str):
        """Set conversation mode to 'bot' or 'human'."""
        now = datetime.now(timezone.utc).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO conversations (phone_number, mode, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(phone_number) DO UPDATE SET mode = ?, updated_at = ?""",
                (phone_number, mode, now, mode, now),
            )
            await db.commit()

    async def count_recent_messages(self, phone_number: str, seconds: int = 60) -> int:
        """Count messages from a phone number in the last N seconds."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE phone_number = ? AND role = 'user' AND created_at > ?""",
                (phone_number, cutoff),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_all_conversations(self) -> list[dict]:
        """Get all conversations (for the agent panel - Phase 2)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT c.phone_number, c.mode, c.updated_at,
                          (SELECT content FROM messages m 
                           WHERE m.phone_number = c.phone_number 
                           ORDER BY m.created_at DESC LIMIT 1) as last_message
                   FROM conversations c
                   ORDER BY c.updated_at DESC"""
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
