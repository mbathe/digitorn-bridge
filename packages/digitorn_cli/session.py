"""CLISession - Dual-mode abstraction for slash command handlers.

Provides a uniform interface so slash commands work identically in
standalone mode (direct RuntimeApp calls) and daemon mode (HTTP API calls).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class CLISession(Protocol):
    """Protocol for CLI session backends."""

    app_id: str
    session_id: str
    mode: str

    def search_tools(self, query: str, max_results: int = 10) -> list[dict[str, Any]]: ...
    def list_categories(self) -> list[dict[str, Any]]: ...
    def browse_category(self, category: str, page: int = 1) -> list[dict[str, Any]]: ...
    def get_tool(self, name: str) -> dict[str, Any] | None: ...
    def execute_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def list_tasks(self) -> list[dict[str, Any]]: ...
    def cancel_task(self, task_id: str) -> bool: ...
    def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    def list_watchers(self) -> list[dict[str, Any]]: ...
    def stop_watcher(self, watcher_id: str) -> bool: ...
    def pause_watcher(self, watcher_id: str) -> bool: ...
    def resume_watcher(self, watcher_id: str) -> bool: ...

    def list_mcp_servers(self) -> list[dict[str, Any]]: ...
    def mcp_health(self, server_id: str | None = None) -> dict[str, Any]: ...

    def list_sessions(self) -> list[dict[str, Any]]: ...
    def get_session_history(self) -> list[dict[str, Any]]: ...
    def clear_session(self) -> bool: ...

    def get_status(self) -> dict[str, Any]: ...

    def get_model_info(self) -> dict[str, Any]: ...


class StandaloneCLISession:
    """Standalone CLI session - calls context_builder actions directly."""

    mode = "standalone"

    def __init__(
        self,
        app: Any,
        context_builder: Any,
        session_id: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.app = app
        self.app_id = app.app_id
        self.session_id = session_id
        self._cb = context_builder
        self._messages = messages

    def _run(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a context_builder action synchronously."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro = self._cb.execute(action, params or {})
        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return asyncio.run(coro)

    def _result_data(self, result: Any) -> Any:
        """Extract data from ActionResult."""
        if hasattr(result, "data"):
            return result.data
        if isinstance(result, dict):
            return result.get("data", result)
        return result


    def search_tools(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        result = self._run("search_tools", {"query": query, "max_results": max_results})
        data = self._result_data(result)
        if isinstance(data, dict):
            return data.get("results", [])
        return []

    def list_categories(self) -> list[dict[str, Any]]:
        result = self._run("list_categories", {})
        data = self._result_data(result)
        if isinstance(data, dict):
            return data.get("categories", [])
        return []

    def browse_category(self, category: str, page: int = 1) -> list[dict[str, Any]]:
        result = self._run("browse_category", {"category": category, "page": page})
        data = self._result_data(result)
        if isinstance(data, dict):
            return data.get("tools", [])
        return []

    def get_tool(self, name: str) -> dict[str, Any] | None:
        result = self._run("get_tool", {"name": name})
        data = self._result_data(result)
        return data if isinstance(data, dict) else None

    def execute_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        result = self._run("execute_tool", {"name": name, "params": params})
        data = self._result_data(result)
        return data if isinstance(data, dict) else {"result": str(data)}


    def list_tasks(self) -> list[dict[str, Any]]:
        result = self._run("background_run", {"list_tasks": True})
        data = self._result_data(result)
        if isinstance(data, dict):
            return data.get("tasks", [])
        return []

    def cancel_task(self, task_id: str) -> bool:
        result = self._run("background_run", {"task_id": task_id, "cancel": True})
        return getattr(result, "success", True) if result else False

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        result = self._run("background_run", {"task_id": task_id})
        data = self._result_data(result)
        return data if isinstance(data, dict) else None


    def list_watchers(self) -> list[dict[str, Any]]:
        result = self._run("watch_list", {})
        data = self._result_data(result)
        if isinstance(data, dict):
            return data.get("watchers", [])
        return []

    def stop_watcher(self, watcher_id: str) -> bool:
        result = self._run("watch_stop", {"watcher_id": watcher_id})
        return getattr(result, "success", True) if result else False

    def pause_watcher(self, watcher_id: str) -> bool:
        result = self._run("watch_pause", {"watcher_id": watcher_id})
        return getattr(result, "success", True) if result else False

    def resume_watcher(self, watcher_id: str) -> bool:
        result = self._run("watch_resume", {"watcher_id": watcher_id})
        return getattr(result, "success", True) if result else False


    def list_mcp_servers(self) -> list[dict[str, Any]]:
        mcp = self.app.modules.get("mcp")
        if mcp is None:
            return []
        result = self._run_module(mcp, "list_servers", {})
        data = self._result_data(result)
        if isinstance(data, dict):
            return data.get("servers", [])
        return []

    def mcp_health(self, server_id: str | None = None) -> dict[str, Any]:
        mcp = self.app.modules.get("mcp")
        if mcp is None:
            return {"error": "MCP module not loaded"}
        params: dict[str, Any] = {}
        if server_id:
            params["server_id"] = server_id
        result = self._run_module(mcp, "health_check", params)
        data = self._result_data(result)
        return data if isinstance(data, dict) else {}

    def _run_module(self, module: Any, action: str, params: dict[str, Any]) -> Any:
        """Execute a module action synchronously."""
        import asyncio
        import concurrent.futures

        coro = module.execute(action, params)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return asyncio.run(coro)


    def list_sessions(self) -> list[dict[str, Any]]:
        return []

    def get_session_history(self) -> list[dict[str, Any]]:
        if self._messages is not None:
            return [
                {"role": m.get("role", "?"), "content": str(m.get("content", ""))[:200]}
                for m in self._messages
                if m.get("role") != "system"
            ]
        return []

    def clear_session(self) -> bool:
        if self._messages is not None:
            system_msgs = [m for m in self._messages if m.get("role") == "system"]
            self._messages.clear()
            self._messages.extend(system_msgs)
            return True
        return False


    def get_status(self) -> dict[str, Any]:
        ctx = self.app.entry_context
        index = self._cb.index
        tasks = self.list_tasks()
        watchers = self.list_watchers()
        return {
            "mode": "standalone",
            "app_id": self.app_id,
            "session_id": self.session_id,
            "agent_id": ctx.agent_id,
            "tool_injection": ctx.tool_injection,
            "total_tools": index.total_tools,
            "total_categories": index.total_categories,
            "active_tasks": sum(1 for t in tasks if t.get("status") == "running"),
            "active_watchers": sum(1 for w in watchers if w.get("status") == "running"),
            "messages": len(self._messages) if self._messages else 0,
        }


    def get_model_info(self) -> dict[str, Any]:
        ctx = self.app.entry_context
        return {
            "agent_id": ctx.agent_id,
            "native_tool_use": ctx.native_tool_use,
            "tool_injection": ctx.tool_injection,
            "context_window": getattr(ctx.context_config, "max_tokens", "?"),
            "strategy": getattr(ctx.context_config, "strategy", "?"),
        }


class DaemonCLISession:
    """Daemon CLI session - routes through the REST API."""

    mode = "daemon"

    def __init__(self, daemon_url: str, app_id: str, session_id: str) -> None:
        self.daemon_url = daemon_url.rstrip("/")
        self.app_id = app_id
        self.session_id = session_id

    def _url(self, path: str) -> str:
        return f"{self.daemon_url}/api/apps/{self.app_id}{path}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        from digitorn_cli.auth import daemon_request
        try:
            resp = daemon_request("get", self._url(path), daemon=self.daemon_url, params=params, timeout=15.0)
            data = resp.json()
            return data.get("data") if data.get("success") else data
        except Exception as exc:
            logger.debug("CLI session GET %s failed: %s", path, exc)
            return {"error": str(exc)}

    def _post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        from digitorn_cli.auth import daemon_request
        try:
            resp = daemon_request("post", self._url(path), daemon=self.daemon_url, json=body or {}, timeout=15.0)
            data = resp.json()
            return data.get("data") if data.get("success") else data
        except Exception as exc:
            logger.debug("CLI session POST %s failed: %s", path, exc)
            return {"error": str(exc)}

    def _delete(self, path: str) -> bool:
        from digitorn_cli.auth import daemon_request
        try:
            resp = daemon_request("delete", self._url(path), daemon=self.daemon_url, timeout=10.0)
            return resp.status_code == 200
        except Exception:
            return False


    def search_tools(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        data = self._get("/tools/search", {"query": query, "max_results": max_results})
        if isinstance(data, dict):
            return data.get("results", [])
        return data if isinstance(data, list) else []

    def list_categories(self) -> list[dict[str, Any]]:
        data = self._get("/tools/categories")
        if isinstance(data, dict):
            return data.get("categories", [])
        return data if isinstance(data, list) else []

    def browse_category(self, category: str, page: int = 1) -> list[dict[str, Any]]:
        data = self._get(f"/tools/categories/{category}", {"page": page})
        if isinstance(data, dict):
            return data.get("tools", [])
        return data if isinstance(data, list) else []

    def get_tool(self, name: str) -> dict[str, Any] | None:
        data = self._get(f"/tools/{name}")
        return data if isinstance(data, dict) else None

    def execute_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        data = self._post(f"/tools/{name}/execute", {"params": params})
        return data if isinstance(data, dict) else {"result": str(data)}


    def list_tasks(self) -> list[dict[str, Any]]:
        data = self._get("/background-tasks")
        if isinstance(data, dict):
            return data.get("tasks", [])
        return data if isinstance(data, list) else []

    def cancel_task(self, task_id: str) -> bool:
        return self._delete(f"/background-tasks/{task_id}")

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        data = self._get(f"/background-tasks/{task_id}")
        return data if isinstance(data, dict) else None


    def list_watchers(self) -> list[dict[str, Any]]:
        data = self._get("/watchers")
        if isinstance(data, dict):
            return data.get("watchers", [])
        return data if isinstance(data, list) else []

    def stop_watcher(self, watcher_id: str) -> bool:
        return self._delete(f"/watchers/{watcher_id}")

    def pause_watcher(self, watcher_id: str) -> bool:
        data = self._post(f"/watchers/{watcher_id}/pause")
        return not isinstance(data, dict) or "error" not in data

    def resume_watcher(self, watcher_id: str) -> bool:
        data = self._post(f"/watchers/{watcher_id}/resume")
        return not isinstance(data, dict) or "error" not in data


    def list_mcp_servers(self) -> list[dict[str, Any]]:
        categories = self.list_categories()
        return [c for c in categories if isinstance(c, dict) and str(c.get("id", "")).startswith("mcp_")]

    def mcp_health(self, server_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if server_id:
            params["server_id"] = server_id
        data = self.execute_tool("mcp.health_check", params)
        return data if isinstance(data, dict) else {}


    def list_sessions(self) -> list[dict[str, Any]]:
        data = self._get("/sessions")
        if isinstance(data, dict):
            return data.get("sessions", [])
        return data if isinstance(data, list) else []

    def get_session_history(self) -> list[dict[str, Any]]:
        data = self._get(f"/sessions/{self.session_id}/history")
        if isinstance(data, dict):
            return data.get("messages", [])
        return data if isinstance(data, list) else []

    def clear_session(self) -> bool:
        return self._delete(f"/sessions/{self.session_id}")


    def get_status(self) -> dict[str, Any]:
        app_data = self._get("")
        tasks = self.list_tasks()
        watchers = self.list_watchers()
        status: dict[str, Any] = {
            "mode": "daemon",
            "app_id": self.app_id,
            "session_id": self.session_id,
            "daemon_url": self.daemon_url,
        }
        if isinstance(app_data, dict):
            status["total_tools"] = app_data.get("total_tools", 0)
            status["total_categories"] = app_data.get("total_categories", 0)
        status["active_tasks"] = sum(
            1 for t in tasks if isinstance(t, dict) and t.get("status") == "running"
        )
        status["active_watchers"] = sum(
            1 for w in watchers if isinstance(w, dict) and w.get("status") == "running"
        )
        return status


    def get_model_info(self) -> dict[str, Any]:
        data = self._get("")
        if isinstance(data, dict):
            return {
                "agents": data.get("agents", []),
                "total_tools": data.get("total_tools", 0),
                "mode": data.get("mode", "?"),
            }
        return {}
