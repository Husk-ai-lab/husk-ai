"""Husk's dynamic graph replay dispatcher.

An instrumented run writes `husk.graph_module = "/abs/path/file.py:graph"` on the
run's root span. The replay endpoint reads that attribute, imports the file
fresh, and either resumes it from a Husk checkpoint (`replay_from`) or re-invokes
it (`invoke`) with the user's state override. The resume/replay itself is owned
by Husk's own engine (`husk_studio_shared.engine`); this module only locates and drives
the user's graph module.

Security note: this dynamically imports user code by path. Local-only MVP.
DO NOT expose this endpoint over a non-localhost interface.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_import_lock = threading.Lock()
# path -> (mtime_ns, module). The mtime is part of the value, not just the key, so a
# re-import evicts the previous module instead of growing the dict forever.
_module_cache: dict[str, tuple[int, Any]] = {}


class GraphModuleNotAllowed(PermissionError):
    """Refused to import a graph module from outside the allowed roots."""


def _allowed_roots() -> list[Path]:
    """Directories a replay is permitted to import a graph module from.

    Default: the backend's current working directory (where ``husk-studio start`` was
    launched, i.e. the user's project). Extra roots can be added via
    ``HUSK_ALLOWED_GRAPH_DIRS`` (os.pathsep-separated). This is the gate that turns
    the dynamic ``exec_module`` from "import any path on disk" (an RCE primitive
    when an attacker controls the stored ``husk.graph_module`` attribute) into
    "import only from trusted project dirs".
    """
    roots = [Path.cwd().resolve()]
    extra = os.environ.get("HUSK_ALLOWED_GRAPH_DIRS", "")
    for part in extra.split(os.pathsep):
        if part.strip():
            roots.append(Path(part).expanduser().resolve())
    return roots


def _check_allowed(p: Path) -> None:
    resolved = p.resolve()
    for root in _allowed_roots():
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    raise GraphModuleNotAllowed(
        f"graph module {resolved} is outside the allowed roots "
        f"(cwd or $HUSK_ALLOWED_GRAPH_DIRS); refusing to import"
    )

# Serialises ALL replays, not just checkpoint resumes. Every replay re-enters the
# same cached module object, so two concurrent replays share that module's globals
# (the HuskAgent instance, its snapshot store, any module-level state the user's
# graph keeps). Interleaving them corrupts both runs. This covers the re-invoke
# paths too — they mutate exactly the same module state as a resume does.
_replay_lock = threading.RLock()


def _load_module(path: str) -> Any:
    """Import a Python file by absolute path, re-importing when the file changes.

    The cache is keyed by path AND mtime. Caching on path alone would pin the first
    version of the agent for the life of the backend process, so the core debugging
    loop — analyze, apply the proposed fix to the source, replay to check it — would
    re-execute the *pre-fix* code and silently report the old behaviour.
    """
    p = Path(path)
    _check_allowed(p)  # refuse to import code from outside the allowed roots
    if not p.exists():
        raise FileNotFoundError(f"Graph file not found: {path}")
    key = str(p.resolve())
    mtime = p.stat().st_mtime_ns
    with _import_lock:
        cached = _module_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        spec = importlib.util.spec_from_file_location(
            f"husk_graph_{p.stem}_{uuid.uuid4().hex[:8]}", str(p)
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot build import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        # Make `examples/` importable as a sibling so relative imports work.
        sys.path.insert(0, str(p.parent.parent))
        try:
            spec.loader.exec_module(module)
        finally:
            try:
                sys.path.remove(str(p.parent.parent))
            except ValueError:
                pass
        _module_cache[key] = (mtime, module)
        return module


def replay_graph(
    *,
    graph_module: str,
    state_override: dict[str, Any],
    new_thread_id: str | None = None,
    parent_thread_id: str | None = None,
    fork_node: str | None = None,
) -> dict[str, Any]:
    """Drive the graph in `graph_module` with `state_override`.

    `graph_module` is "<abs_path_to_file>:<symbol>" — typically ":graph" or
    ":invoke". If the symbol is callable, it's called with state_override.
    If it's a graph object exposing `.invoke`, we call
    `.invoke(state_override, config={...})`.

    TRUE checkpoint resume (Husk's own engine): when both `parent_thread_id` and
    `fork_node` are given and the module exposes `replay_from`, we resume that
    thread from its Husk snapshot and re-run only `fork_node` onward (the upstream
    nodes are bypassed). Otherwise we fall back to a full re-run with a fresh
    thread, which keeps the endpoint backward compatible.
    """
    # Split on the LAST colon so Windows drive letters (C:\...) survive.
    path, _, symbol = graph_module.rpartition(":")
    if not path:
        # No symbol separator → assume whole string is the path, default symbol.
        path, symbol = graph_module, "graph"
    symbol = symbol or "graph"
    module = _load_module(path)
    target = getattr(module, symbol, None)

    def _resolve(name: str) -> Any:
        # Older examples expose invoke/replay_from as module-level functions; a
        # decorator-style HuskAgent exposes them as methods on the symbol object.
        # The agent is marked (`_husk_agent`) so we never misread a LangGraph
        # object's `.invoke(state, config=)` as the Husk `invoke(state, thread_id=)`.
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
        if target is not None and getattr(target, "_husk_agent", False):
            m = getattr(target, name, None)
            if callable(m):
                return m
        return None

    with _replay_lock:
        # Preferred path: a true checkpoint resume that skips the upstream nodes.
        if parent_thread_id and fork_node:
            replay_from = _resolve("replay_from")
            if replay_from is not None:
                resumed: dict[str, Any] = replay_from(
                    state_override=state_override,
                    parent_thread_id=parent_thread_id,
                    fork_node=fork_node,
                )
                return resumed

        if target is None:
            raise AttributeError(f"{path} has no attribute {symbol!r}")

        tid = new_thread_id or str(uuid.uuid4())

        # Preferred path: an `invoke(state, thread_id=...)` on the module or the agent.
        fn = _resolve("invoke")
        if fn is not None and fn is not target:
            invoked: dict[str, Any] = fn(state_override, thread_id=tid)
            return invoked

        # Otherwise treat the symbol as a graph object exposing `.invoke`.
        if hasattr(target, "invoke"):
            config = {"configurable": {"thread_id": tid}}
            result = target.invoke(state_override, config=config)
            return {
                "thread_id": tid,
                "state": dict(result) if hasattr(result, "items") else result,
            }

        if callable(target):
            return {"thread_id": tid, "state": target(state_override)}

        raise TypeError(
            f"{symbol!r} is not invokable: expected a graph or callable, "
            f"got {type(target).__name__}"
        )
