"""Memory Store - persistent storage for all memory layers."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class TodoStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"

@dataclass
class TodoItem:
    id: str
    content: str
    status: TodoStatus = TodoStatus.PENDING
    created_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None
    parent_id: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "notes": self.notes,
            "parent_id": self.parent_id,
        }

@dataclass
class Note:
    """A sticky note -- something the agent promised itself to do."""
    id: str
    content: str
    resolved: bool = False
    created_at: float = field(default_factory=time.monotonic)

@dataclass
class Checkpoint:
    """A save point -- the agent's self-assessment at a moment in time."""
    id: str
    summary: str
    findings: str
    next_steps: str
    created_at: float = field(default_factory=time.monotonic)

@dataclass
class WorkingMemory:
    """The agent's active cognitive workspace."""

    original_request: str = ""

    goal: str = ""
    sub_goals: list[str] = field(default_factory=list)

    plan: list[str] = field(default_factory=list)
    current_step: int = 0

    todos: list[TodoItem] = field(default_factory=list)
    _todo_counter: int = 0

    notes: list[Note] = field(default_factory=list)
    _note_counter: int = 0

    checkpoints: list[Checkpoint] = field(default_factory=list)
    _checkpoint_counter: int = 0

    key_facts: list[str] = field(default_factory=list)

    active_entities: dict[str, str] = field(default_factory=dict)

    content_cache: dict[str, CachedContent] = field(default_factory=dict)

    def add_checkpoint(self, summary: str, findings: str, next_steps: str) -> Checkpoint:
        self._checkpoint_counter += 1
        cp = Checkpoint(
            id=f"cp{self._checkpoint_counter}",
            summary=summary,
            findings=findings,
            next_steps=next_steps,
        )
        self.checkpoints.append(cp)
        if len(self.checkpoints) > 3:
            self.checkpoints = self.checkpoints[-3:]
        return cp

    def add_note(self, content: str) -> Note:
        self._note_counter += 1
        note = Note(id=f"n{self._note_counter}", content=content)
        self.notes.append(note)
        return note

    def resolve_note(self, note_id: str) -> bool:
        for note in self.notes:
            if note.id == note_id:
                note.resolved = True
                return True
        return False

    def pending_notes(self) -> list[Note]:
        return [n for n in self.notes if not n.resolved]

    def has_unfinished_work(self) -> tuple[bool, str]:
        """Check if there's unfinished work (incomplete todos or unresolved notes)."""
        pending_todos = [t for t in self.todos if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)]
        pending_notes = self.pending_notes()
        if not pending_todos and not pending_notes:
            return False, ""
        parts = []
        if pending_todos:
            parts.append(f"{len(pending_todos)} incomplete task(s)")
        if pending_notes:
            parts.append(f"{len(pending_notes)} unresolved note(s)")
        return True, " and ".join(parts)

    def add_todo(self, content: str, parent_id: str | None = None) -> TodoItem:
        self._todo_counter += 1
        item = TodoItem(
            id=f"t{self._todo_counter}",
            content=content,
            parent_id=parent_id,
        )
        self.todos.append(item)
        return item

    def complete_todo(self, todo_id: str, notes: str = "") -> bool:
        for item in self.todos:
            if item.id == todo_id:
                item.status = TodoStatus.DONE
                item.completed_at = time.monotonic()
                item.notes = notes
                return True
        return False

    def start_todo(self, todo_id: str) -> bool:
        for item in self.todos:
            if item.id == todo_id:
                item.status = TodoStatus.IN_PROGRESS
                return True
        return False

    def block_todo(self, todo_id: str, reason: str = "") -> bool:
        for item in self.todos:
            if item.id == todo_id:
                item.status = TodoStatus.BLOCKED
                item.notes = reason
                return True
        return False

    def get_progress(self) -> dict[str, int]:
        total = len(self.todos)
        done = sum(1 for t in self.todos if t.status == TodoStatus.DONE)
        in_progress = sum(1 for t in self.todos if t.status == TodoStatus.IN_PROGRESS)
        blocked = sum(1 for t in self.todos if t.status == TodoStatus.BLOCKED)
        pending = total - done - in_progress - blocked
        return {
            "total": total, "done": done, "in_progress": in_progress,
            "blocked": blocked, "pending": pending,
            "percent": round(done / total * 100) if total > 0 else 0,
        }

    def _current_status(self) -> str | None:
        for item in self.todos:
            if item.status == TodoStatus.IN_PROGRESS:
                return f"In progress: {item.content} [{item.id}]"
        for item in self.todos:
            if item.status == TodoStatus.PENDING:
                return f"⏭ Next task available: {item.content} [{item.id}]"
        progress = self.get_progress()
        if progress["total"] > 0 and progress["done"] == progress["total"]:
            pending = self.pending_notes()
            if pending:
                return f"✅ All tasks completed. ⚠ {len(pending)} unresolved note(s) remain."
            return "✅ All tasks completed."
        return None

    def render(self) -> str:
        """Render working memory as a compact text block for prompt injection."""
        parts: list[str] = []

        status = self._current_status()
        if status:
            parts.append(status)
            parts.append("")

        if self.goal:
            parts.append(f"🎯 Goal: {self.goal}")
            if self.sub_goals:
                for sg in self.sub_goals:
                    parts.append(f"   └─ {sg}")

        if self.plan:
            parts.append("📋 Plan:")
            for i, step in enumerate(self.plan):
                marker = "→" if i == self.current_step else " "
                check = "✅" if i < self.current_step else "🔄" if i == self.current_step else "⬚"
                parts.append(f"  {marker} {check} {i+1}. {step}")

        if self.todos:
            progress = self.get_progress()
            parts.append(
                f"📝 Tasks ({progress['done']}/{progress['total']} done, "
                f"{progress['percent']}%):"
            )
            next_task_shown = False
            for item in self.todos:
                icon = {
                    TodoStatus.DONE: "✅",
                    TodoStatus.IN_PROGRESS: "🔄",
                    TodoStatus.BLOCKED: "🚫",
                    TodoStatus.PENDING: "⬚",
                }[item.status]
                indent = "    " if item.parent_id else "  "
                marker = ""
                if item.status == TodoStatus.PENDING and not next_task_shown:
                    marker = " ← NEXT"
                    next_task_shown = True
                line = f"{indent}{icon} [{item.id}] {item.content}{marker}"
                if item.notes:
                    short_notes = item.notes[:80] + "..." if len(item.notes) > 80 else item.notes
                    line += f" - {short_notes}"
                parts.append(line)

        pending = self.pending_notes()
        if pending:
            parts.append(f"Notes ({len(pending)} pending -- don't forget!):")
            for note in pending:
                parts.append(f"  - [{note.id}] {note.content}")

        if self.checkpoints:
            cp = self.checkpoints[-1]
            parts.append(f"Last checkpoint [{cp.id}]:")
            parts.append(f"  Done so far: {cp.summary}")
            parts.append(f"  Key findings: {cp.findings}")
            parts.append(f"  Remaining: {cp.next_steps}")

        if self.key_facts:
            parts.append("💡 Key facts:")
            for fact in self.key_facts[-7:]:
                parts.append(f"  • {fact}")

        if self.active_entities:
            parts.append("📎 Active context:")
            for name, summary in list(self.active_entities.items())[-5:]:
                parts.append(f"  • {name}: {summary}")

        if self.original_request:
            req = self.original_request
            if len(req) > 300:
                req = req[:297] + "..."
            progress = self.get_progress()
            if progress["total"] > 0 and progress["done"] > 0:
                parts.append(
                    f"📋 Original request (you are already working on this - "
                    f"{progress['done']}/{progress['total']} tasks done, "
                    f"continue from where you left off):"
                )
            else:
                parts.append(f"📋 Original request (reference only):")
            parts.append(f"  \"{req}\"")

        return "\n".join(parts)

@dataclass
class CachedContent:
    """Cached file content for O(1) access."""
    path: str
    content: str
    content_hash: str
    cached_at: float
    size: int
    summary: str = ""

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:12]

    def is_fresh(self, ttl: float = 60.0) -> bool:
        return (time.monotonic() - self.cached_at) < ttl

@dataclass
class Episode:
    """Summary of a past session."""
    session_id: str
    timestamp: float
    summary: str
    decisions: list[str] = field(default_factory=list)
    errors_resolved: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    turns: int = 0
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "decisions": self.decisions,
            "errors_resolved": self.errors_resolved,
            "tools_used": self.tools_used,
            "turns": self.turns,
            "outcome": self.outcome,
        }

@dataclass
class SemanticFact:
    """A single fact with embedding for semantic search."""
    id: str
    content: str
    category: str = ""
    importance: float = 1.0
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    source: str = ""
    superseded_by: str | None = None
    embedding: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "importance": self.importance,
            "source": self.source,
        }

@dataclass
class GraphEdge:
    """A relationship between two entities."""
    source: str
    relation: str
    target: str
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

@dataclass
class SemanticMemory:
    """Facts + entity graph."""
    facts: list[SemanticFact] = field(default_factory=list)
    graph: list[GraphEdge] = field(default_factory=list)
    _fact_counter: int = 0

    def add_fact(
        self, content: str, category: str = "", importance: float = 1.0,
        source: str = "extracted",
    ) -> SemanticFact:
        # Deduplicate case/whitespace-insensitively; bump importance on re-mention.
        needle = (content or "").strip().lower()
        if needle:
            for existing in self.facts:
                if existing.superseded_by:
                    continue
                if existing.content.strip().lower() == needle:
                    existing.importance = min(10.0, existing.importance + 0.1)
                    if category and not existing.category:
                        existing.category = category
                    return existing
        self._fact_counter += 1
        fact = SemanticFact(
            id=f"f{self._fact_counter}",
            content=content,
            category=category,
            importance=importance,
            source=source,
        )
        self.facts.append(fact)
        return fact

    def add_edge(
        self, source: str, relation: str, target: str, **props: Any,
    ) -> GraphEdge:
        edge = GraphEdge(source=source, relation=relation, target=target, properties=props)
        self.graph.append(edge)
        return edge

    def find_facts(self, query: str, limit: int = 5) -> list[SemanticFact]:
        """Simple keyword search (semantic search done by runtime layer)."""
        q = query.lower()
        scored = []
        for fact in self.facts:
            if fact.superseded_by:
                continue
            score = sum(1 for word in q.split() if word in fact.content.lower())
            if score > 0:
                scored.append((score * fact.importance, fact))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:limit]]

    def get_entity_context(self, entity: str) -> list[GraphEdge]:
        """Get all edges involving an entity."""
        e = entity.lower()
        return [
            edge for edge in self.graph
            if e in edge.source.lower() or e in edge.target.lower()
        ]

@dataclass
class Procedure:
    """A learned pattern from past sessions."""
    id: str
    name: str
    trigger: str
    steps: list[str]
    success_rate: float = 0.0
    times_used: int = 0
    last_used: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger": self.trigger,
            "steps": self.steps,
            "success_rate": self.success_rate,
            "times_used": self.times_used,
        }

@dataclass
class MemoryConfig:
    """Parsed from YAML config."""
    working_memory: bool = False
    todo_list: bool = False
    checkpoint: bool = False
    episodic: bool = False
    semantic_vector: bool = False
    semantic_graph: bool = False
    procedural: bool = False
    runtime_proactive_injection: bool = False
    runtime_content_cache: bool = False
    runtime_goal_guardian: bool = False
    runtime_background_extraction: bool = False
    limits_max_injected: int = 7
    limits_budget_percent: int = 15
    limits_max_memories: int = 1000
    limits_episodic_ttl_days: int = 90

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> MemoryConfig:
        semantic = config.get("semantic", {})
        runtime = config.get("runtime", {})
        limits = config.get("limits", {})
        return cls(
            working_memory=config.get("working_memory", False),
            todo_list=config.get("todo_list", False),
            checkpoint=config.get("checkpoint", False),
            episodic=config.get("episodic", False),
            semantic_vector=semantic.get("vector", False) if isinstance(semantic, dict) else bool(semantic),
            semantic_graph=semantic.get("graph", False) if isinstance(semantic, dict) else False,
            procedural=config.get("procedural", False),
            runtime_proactive_injection=runtime.get("proactive_injection", False),
            runtime_content_cache=runtime.get("content_cache", False),
            runtime_goal_guardian=runtime.get("goal_guardian", False),
            runtime_background_extraction=runtime.get("background_extraction", False),
            limits_max_injected=limits.get("max_injected", 7),
            limits_budget_percent=limits.get("budget_percent", 15),
            limits_max_memories=limits.get("max_memories", 1000),
            limits_episodic_ttl_days=limits.get("episodic_ttl_days", 90),
        )

    def has_any_layer(self) -> bool:
        return any([
            self.working_memory, self.todo_list, self.checkpoint,
            self.episodic, self.semantic_vector, self.semantic_graph,
            self.procedural,
        ])

    def has_any_runtime(self) -> bool:
        return any([
            self.runtime_proactive_injection, self.runtime_content_cache,
            self.runtime_goal_guardian, self.runtime_background_extraction,
        ])

class MemoryStore:
    """Unified container for all memory layers, scoped by session."""

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config or MemoryConfig()

        self.working: WorkingMemory = WorkingMemory()
        self.episodes: list[Episode] = []

        self.semantic: SemanticMemory = SemanticMemory()
        self.procedures: list[Procedure] = []

        self.session_id: str | None = None
        self.app_id: str | None = None

    def render_full_snapshot(self) -> str:
        """Render ALL active memory layers as a single text block."""
        sections: list[str] = []

        sections.append("═══ MEMORY ═══")

        if self.config.working_memory or self.config.todo_list:
            wm = self.working.render()
            if wm:
                sections.append(wm)

        if self.config.episodic and self.episodes:
            sections.append("Recent sessions:")
            for ep in self.episodes[-3:]:
                sections.append(f"  [{ep.outcome}] {ep.summary}")
                if ep.errors_resolved:
                    for err in ep.errors_resolved[:2]:
                        sections.append(f"    Resolved: {err}")

        if self.config.semantic_vector and self.semantic.facts:
            active_facts = [
                f for f in self.semantic.facts
                if not f.superseded_by
            ]
            if active_facts:
                top = sorted(active_facts, key=lambda f: f.importance, reverse=True)
                sections.append("🧠 Known facts:")
                for fact in top[:self.config.limits_max_injected]:
                    sections.append(f"  • {fact.content}")

        if self.config.semantic_graph and self.working.active_entities:
            edges = []
            for entity in self.working.active_entities:
                edges.extend(self.semantic.get_entity_context(entity))
            if edges:
                sections.append("🔗 Relationships:")
                seen = set()
                for edge in edges[:5]:
                    key = f"{edge.source}-{edge.relation}-{edge.target}"
                    if key not in seen:
                        sections.append(f"  {edge.source} ──[{edge.relation}]──→ {edge.target}")
                        seen.add(key)

        if self.config.procedural and self.procedures:
            relevant = sorted(self.procedures, key=lambda p: p.success_rate, reverse=True)
            if relevant:
                sections.append("Known patterns:")
                for proc in relevant[:3]:
                    rate = f"{proc.success_rate:.0%}" if proc.times_used > 0 else "new"
                    sections.append(f"  • {proc.name} ({rate}): {proc.trigger}")

        sections.append("═══════════════")

        return "\n".join(sections)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for persistence."""
        return {
            "working": {
                "goal": self.working.goal,
                "sub_goals": self.working.sub_goals,
                "plan": self.working.plan,
                "current_step": self.working.current_step,
                "todos": [t.to_dict() for t in self.working.todos],
                "key_facts": self.working.key_facts,
                "active_entities": self.working.active_entities,
            },
            "episodes": [e.to_dict() for e in self.episodes],
            "semantic": {
                "facts": [f.to_dict() for f in self.semantic.facts],
                "graph": [
                    {"source": e.source, "relation": e.relation, "target": e.target}
                    for e in self.semantic.graph
                ],
            },
            "procedures": [p.to_dict() for p in self.procedures],
        }

    def restore_from_dict(self, data: dict[str, Any]) -> None:
        """Restore full memory state from a serialized dict (session resume)."""
        w = data.get("working", {})
        if w:
            self.working.goal = w.get("goal", "")
            self.working.sub_goals = w.get("sub_goals", [])
            self.working.plan = w.get("plan", [])
            self.working.current_step = w.get("current_step", 0)
            self.working.key_facts = w.get("key_facts", [])
            self.working.active_entities = w.get("active_entities", {})
            self.working.todos = []
            for td in w.get("todos", []):
                self.working.todos.append(TodoItem(
                    id=td.get("id", ""),
                    content=td.get("content", ""),
                    status=TodoStatus(td.get("status", "pending")),
                    notes=td.get("notes", ""),
                    created_at=td.get("created_at", 0.0),
                ))

        for ep_data in data.get("episodes", []):
            self.episodes.append(EpisodicEntry(
                summary=ep_data.get("summary", ""),
                timestamp=ep_data.get("timestamp", 0.0),
                turn_range=tuple(ep_data.get("turn_range", (0, 0))),
                importance=ep_data.get("importance", 0.5),
            ))

        # Dedup at restore: `semantic.facts` would otherwise grow
        # unbounded across multi-instance writes before a restart.
        existing_contents = {
            (f.content or "").strip().lower()
            for f in self.semantic.facts
        }
        for i, f_data in enumerate(data.get("semantic", {}).get("facts", [])):
            content_norm = (f_data.get("content", "") or "").strip().lower()
            if content_norm in existing_contents:
                continue
            existing_contents.add(content_norm)
            self.semantic.facts.append(SemanticFact(
                id=f_data.get("id", f"f{i+1}"),
                content=f_data.get("content", ""),
                category=f_data.get("category", ""),
                source=f_data.get("source", ""),
                importance=f_data.get("confidence", f_data.get("importance", 1.0)),
                created_at=f_data.get("timestamp", f_data.get("created_at", 0.0)),
            ))

    def persist(self, backend: Any, app_id: str, session_id: str, user_id: str = "") -> None:
        """Save long-term memory (facts, episodes, procedures) to a KV backend."""
        key = self._memory_key(app_id, user_id)
        data = {
            "key_facts": self.working.key_facts,
            "episodes": [e.to_dict() for e in self.episodes],
            "semantic": {
                "facts": [f.to_dict() for f in self.semantic.facts],
                "graph": [
                    {"source": e.source, "relation": e.relation, "target": e.target}
                    for e in self.semantic.graph
                ],
            },
            "procedures": [p.to_dict() for p in self.procedures],
        }
        backend.set(key, json.dumps(data, default=str), expire=86400 * 90)

    @staticmethod
    def _memory_key(app_id: str, user_id: str = "") -> str:
        if user_id:
            return f"memory:{app_id}:{user_id}:long_term"
        return f"memory:{app_id}:long_term"

    def restore(self, backend: Any, app_id: str, user_id: str = "") -> int:
        """Load long-term memory from a KV backend. Returns count of facts restored."""
        key = self._memory_key(app_id, user_id)
        raw = backend.get(key)
        migrated_from_legacy = False
        if not raw and user_id:
            legacy_key = f"memory:{app_id}:long_term"
            raw = backend.get(legacy_key)
            if raw:
                try:
                    backend.set(key, raw, expire=86400 * 90)
                    # Keep the legacy key so other users can still
                    # migrate; the first per-user persist supersedes it.
                    migrated_from_legacy = True
                except Exception as exc:
                    logger.debug("store best-effort block failed: %s", exc)
        if not raw:
            return 0

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return 0

        count = 0

        existing = set(self.working.key_facts)
        for fact in data.get("key_facts", []):
            if fact not in existing:
                self.working.key_facts.append(fact)
                existing.add(fact)
                count += 1

        for ep_data in data.get("episodes", []):
            ep = EpisodicEntry(
                session_id=ep_data.get("session_id", ""),
                summary=ep_data.get("summary", ""),
                timestamp=ep_data.get("timestamp", 0),
                key_decisions=ep_data.get("key_decisions", []),
                errors_encountered=ep_data.get("errors_encountered", []),
                tools_used=ep_data.get("tools_used", []),
                turns=ep_data.get("turns", 0),
            )
            self.episodes.append(ep)
            count += 1

        return count
