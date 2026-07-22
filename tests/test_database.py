"""Tests for database.py - real SQLite with tmp_path."""
import pytest


class TestSaveAndGetHistory:
    async def test_save_and_retrieve(self, db):
        await db.save_message("34612345678", "user", "Hola")
        await db.save_message("34612345678", "assistant", "Bienvenido")
        await db.save_message("34612345678", "user", "Mi Dyson no aspira")

        history = await db.get_history("34612345678", limit=10)
        assert len(history) == 3
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hola"
        assert history[2]["content"] == "Mi Dyson no aspira"

    async def test_history_limit(self, db):
        for i in range(5):
            await db.save_message("34612345678", "user", f"msg {i}")

        history = await db.get_history("34612345678", limit=2)
        assert len(history) == 2
        assert history[0]["content"] == "msg 3"
        assert history[1]["content"] == "msg 4"

    async def test_history_empty(self, db):
        history = await db.get_history("34612345678", limit=10)
        assert history == []

    async def test_history_isolated_by_phone(self, db):
        await db.save_message("111", "user", "msg A")
        await db.save_message("222", "user", "msg B")

        history = await db.get_history("111", limit=10)
        assert len(history) == 1
        assert history[0]["content"] == "msg A"


class TestConversationMode:
    async def test_default_is_bot(self, db):
        mode = await db.get_conversation_mode("34612345678")
        assert mode == "bot"

    async def test_set_to_human(self, db):
        await db.set_conversation_mode("34612345678", "human")
        mode = await db.get_conversation_mode("34612345678")
        assert mode == "human"

    async def test_set_back_to_bot(self, db):
        await db.set_conversation_mode("34612345678", "human")
        await db.set_conversation_mode("34612345678", "bot")
        mode = await db.get_conversation_mode("34612345678")
        assert mode == "bot"

    async def test_save_message_creates_conversation(self, db):
        await db.save_message("34612345678", "user", "Hola")
        mode = await db.get_conversation_mode("34612345678")
        assert mode == "bot"


class TestCountRecentMessages:
    async def test_counts_recent(self, db):
        await db.save_message("34612345678", "user", "msg 1")
        await db.save_message("34612345678", "user", "msg 2")
        await db.save_message("34612345678", "user", "msg 3")

        count = await db.count_recent_messages("34612345678", seconds=60)
        assert count == 3

    async def test_only_counts_user_role(self, db):
        await db.save_message("34612345678", "user", "msg 1")
        await db.save_message("34612345678", "assistant", "reply")
        await db.save_message("34612345678", "user", "msg 2")

        count = await db.count_recent_messages("34612345678", seconds=60)
        assert count == 2

    async def test_zero_when_no_messages(self, db):
        count = await db.count_recent_messages("34612345678", seconds=60)
        assert count == 0

    async def test_isolated_by_phone(self, db):
        await db.save_message("111", "user", "msg")
        await db.save_message("222", "user", "msg")

        count = await db.count_recent_messages("111", seconds=60)
        assert count == 1
