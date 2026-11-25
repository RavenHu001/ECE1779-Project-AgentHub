"""Upgrade either independently deployed schema without losing its saved records.
从任一独立部署的结构升级，保留已有记录。
"""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.db import SessionLocal
from app.event_store import EventToAppend, append_events, read_events
from app.models import AgentStep, Conversation, ConversationEventHead, Job, Message


@pytest.mark.parametrize("revision", ["0007_agent_steps", "0007_conversation_events"])
def test_upgrade_from_either_branch_preserves_records(accounts, revision):
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.downgrade(cfg, "0006_approved_resolutions")
    command.upgrade(cfg, revision)
    with SessionLocal.begin() as session:
        cid = uuid4()
        session.execute(
            text("INSERT INTO conversations (id, user_id) VALUES (:id, :owner)"),
            {"id": cid, "owner": accounts[0]},
        )
        if revision == "0007_agent_steps":
            source = Message(conversation_id=cid, seq=1, role="user", content="Hello", submission_key=uuid4())
            session.add(source)
            session.flush()
            job = Job(source_message_id=source.id, model_calls=1)
            session.add(job)
            session.flush()
            jid = job.id
            session.add(AgentStep(job_id=jid, seq=1, name="reply", arguments={"text": "Saved answer"}))
        else:
            session.add(ConversationEventHead(conversation_id=cid))
            append_events(session, cid, [EventToAppend("proposal.created", {"saved": True})])
    command.upgrade(cfg, "head")
    with SessionLocal() as session:
        assert session.scalars(text("SELECT version_num FROM alembic_version")).all() == [
            "0010_browser_sessions"
        ]
        assert session.get(Conversation, cid) is not None
        assert session.get(Conversation, cid).is_pinned is False
        assert session.get(Conversation, cid).deleted_at is None
        if revision == "0007_agent_steps":
            assert session.get(AgentStep, (jid, 1)).arguments == {"text": "Saved answer"}
            assert session.get(Job, jid).model_calls == 1
            assert session.get(ConversationEventHead, cid).last_event_seq == 0
        else:
            assert read_events(session, cid)[0].payload == {"saved": True}
            assert session.get(ConversationEventHead, cid).last_event_seq == 1
