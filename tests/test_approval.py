"""E2E tests - ApprovalQueue: enqueue, resolve, timeout, cancel.

Covers:
- ApprovalRequest creation and serialization
- Enqueue + resolve (approve/deny) flow
- Timeout auto-deny
- cancel_all on shutdown
- Callback notification
- Multiple concurrent approvals
- Resolve unknown request
- List pending requests
"""
from __future__ import annotations

import asyncio

import pytest

from digitorn.core.runtime.approval import ApprovalQueue, ApprovalRequest


# ── ApprovalRequest ──────────────────────────────────────────────────────


class TestApprovalRequest:
    def test_creation(self):
        req = ApprovalRequest(
            request_id="r1",
            agent_id="agent1",
            user_id="test-user",
            tool_name="filesystem.write",
            tool_params={"path": "/tmp/x"},
            risk_level="high",
            description="Write to /tmp/x",
        )
        assert req.request_id == "r1"
        assert req.tool_name == "filesystem.write"
        assert req.risk_level == "high"

    def test_to_dict(self):
        req = ApprovalRequest(
            request_id="r1", agent_id="a", user_id="test-user",
            tool_name="t", tool_params={"k": "v"},
            risk_level="medium", description="desc",
        )
        d = req.to_dict()
        assert d["request_id"] == "r1"
        assert d["tool_params"] == {"k": "v"}
        assert "created_at" in d
        # _future should not be in serialized output
        assert "_future" not in d


# ── ApprovalQueue - basic flow ───────────────────────────────────────────


class TestApprovalQueueBasic:
    @pytest.mark.asyncio
    async def test_enqueue_and_approve(self):
        queue = ApprovalQueue()

        async def approve_after_delay():
            await asyncio.sleep(0.05)
            pending = queue.list_pending()
            assert len(pending) == 1
            queue.resolve(pending[0]["request_id"], approved=True)

        asyncio.create_task(approve_after_delay())
        approved, msg = await queue.enqueue(
            agent_id="agent1", tool_name="fs.write",
            tool_params={}, risk_level="high",
        )
        assert approved is True
        assert msg == ""
        assert queue.pending_count == 0

    @pytest.mark.asyncio
    async def test_enqueue_and_deny(self):
        queue = ApprovalQueue()

        async def deny_after_delay():
            await asyncio.sleep(0.05)
            pending = queue.list_pending()
            queue.resolve(pending[0]["request_id"], approved=False, message="Not allowed")

        asyncio.create_task(deny_after_delay())
        approved, msg = await queue.enqueue(
            agent_id="agent1", tool_name="db.drop",
            tool_params={}, risk_level="high",
        )
        assert approved is False
        assert msg == "Not allowed"

    @pytest.mark.asyncio
    async def test_timeout_auto_deny(self):
        queue = ApprovalQueue(default_timeout=0.1)
        approved, msg = await queue.enqueue(
            agent_id="agent1", tool_name="fs.delete",
            tool_params={}, risk_level="high",
        )
        assert approved is False
        assert "timed out" in msg.lower()
        assert queue.pending_count == 0

    @pytest.mark.asyncio
    async def test_custom_timeout(self):
        queue = ApprovalQueue(default_timeout=300)  # 5 min default
        approved, msg = await queue.enqueue(
            agent_id="agent1", tool_name="fs.delete",
            tool_params={}, timeout=0.1,  # override to 0.1s
        )
        assert approved is False
        assert "timed out" in msg.lower()


# ── ApprovalQueue - resolve edge cases ───────────────────────────────────


class TestApprovalQueueResolve:
    @pytest.mark.asyncio
    async def test_resolve_unknown_returns_false(self):
        queue = ApprovalQueue()
        assert queue.resolve("nonexistent", approved=True) is False

    @pytest.mark.asyncio
    async def test_resolve_already_resolved(self):
        queue = ApprovalQueue()

        async def double_resolve():
            await asyncio.sleep(0.05)
            pending = queue.list_pending()
            rid = pending[0]["request_id"]
            first = queue.resolve(rid, approved=True)
            second = queue.resolve(rid, approved=False)
            assert first is True
            assert second is False  # already done

        asyncio.create_task(double_resolve())
        await queue.enqueue(
            agent_id="a", tool_name="t", tool_params={},
        )

    @pytest.mark.asyncio
    async def test_approve_ignores_message(self):
        """When approved=True, message should be empty string."""
        queue = ApprovalQueue()

        async def approve():
            await asyncio.sleep(0.05)
            pending = queue.list_pending()
            queue.resolve(pending[0]["request_id"], approved=True, message="ignored")

        asyncio.create_task(approve())
        _, msg = await queue.enqueue(
            agent_id="a", tool_name="t", tool_params={},
        )
        assert msg == ""


# ── ApprovalQueue - cancel_all ───────────────────────────────────────────


class TestApprovalQueueCancel:
    @pytest.mark.asyncio
    async def test_cancel_all(self):
        queue = ApprovalQueue(default_timeout=10)
        results = []

        async def enqueue_and_collect(name: str):
            approved, msg = await queue.enqueue(
                agent_id=name, tool_name="t", tool_params={},
            )
            results.append((name, approved))

        # Start 3 enqueues concurrently
        tasks = [
            asyncio.create_task(enqueue_and_collect("a1")),
            asyncio.create_task(enqueue_and_collect("a2")),
            asyncio.create_task(enqueue_and_collect("a3")),
        ]

        await asyncio.sleep(0.05)
        assert queue.pending_count == 3

        count = queue.cancel_all()
        assert count == 3

        await asyncio.gather(*tasks)
        assert all(not approved for _, approved in results)
        assert queue.pending_count == 0


# ── ApprovalQueue - callback ────────────────────────────────────────────


class TestApprovalQueueCallback:
    @pytest.mark.asyncio
    async def test_on_request_callback(self):
        queue = ApprovalQueue()
        callback_calls = []

        async def on_request(req: ApprovalRequest):
            callback_calls.append(req.tool_name)
            # Auto-approve in callback
            queue.resolve(req.request_id, approved=True)

        queue.set_on_request(on_request)
        approved, _ = await queue.enqueue(
            agent_id="a", tool_name="test_tool", tool_params={},
        )
        assert approved is True
        assert callback_calls == ["test_tool"]

    @pytest.mark.asyncio
    async def test_callback_error_does_not_break(self):
        queue = ApprovalQueue(default_timeout=0.1)

        async def bad_callback(req):
            raise RuntimeError("callback exploded")

        queue.set_on_request(bad_callback)

        # Should not raise, just timeout
        approved, msg = await queue.enqueue(
            agent_id="a", tool_name="t", tool_params={},
        )
        assert approved is False


# ── ApprovalQueue - list_pending ─────────────────────────────────────────


class TestApprovalQueueListPending:
    @pytest.mark.asyncio
    async def test_list_pending(self):
        queue = ApprovalQueue(default_timeout=10)

        async def enqueue():
            await queue.enqueue(agent_id="a1", tool_name="t1", tool_params={})

        task = asyncio.create_task(enqueue())

        await asyncio.sleep(0.05)
        pending = queue.list_pending()
        assert len(pending) == 1
        assert pending[0]["agent_id"] == "a1"
        assert pending[0]["tool_name"] == "t1"

        # Cleanup
        queue.cancel_all()
        await task

    @pytest.mark.asyncio
    async def test_list_pending_empty(self):
        queue = ApprovalQueue()
        assert queue.list_pending() == []


# ── ApprovalQueue - concurrent approvals ─────────────────────────────────


class TestApprovalQueueConcurrency:
    @pytest.mark.asyncio
    async def test_multiple_agents_independent(self):
        """Each agent's approval is independent - approving one doesn't affect others."""
        queue = ApprovalQueue(default_timeout=5)
        results = {}

        async def enqueue_for(name: str):
            approved, _ = await queue.enqueue(
                agent_id=name, tool_name=f"tool_{name}",
                tool_params={},
            )
            results[name] = approved

        t1 = asyncio.create_task(enqueue_for("agent1"))
        t2 = asyncio.create_task(enqueue_for("agent2"))
        t3 = asyncio.create_task(enqueue_for("agent3"))

        await asyncio.sleep(0.05)
        pending = queue.list_pending()

        # Approve agent1, deny agent2, leave agent3
        for p in pending:
            if p["agent_id"] == "agent1":
                queue.resolve(p["request_id"], approved=True)
            elif p["agent_id"] == "agent2":
                queue.resolve(p["request_id"], approved=False)

        await t1
        await t2
        assert results["agent1"] is True
        assert results["agent2"] is False

        # agent3 still pending
        assert queue.pending_count == 1
        queue.cancel_all()
        await t3
        assert results["agent3"] is False
