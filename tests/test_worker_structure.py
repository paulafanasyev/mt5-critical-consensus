from __future__ import annotations

import ast
from pathlib import Path


def test_worker_declares_persistence_callback_before_pending_recovery_loop():
    root = Path(__file__).resolve().parents[1]
    worker_path = root / "scripts" / "run_worker.py"
    tree = ast.parse(worker_path.read_text(encoding="utf-8"), filename=str(worker_path))

    main = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    persist_index = next(
        (index for index, node in enumerate(main.body)
         if isinstance(node, ast.AsyncFunctionDef) and node.name == "persist"),
        None,
    )
    recovery_index = next(
        (index for index, node in enumerate(main.body)
         if isinstance(node, ast.While)
         and any(
             isinstance(child, ast.Call)
             and isinstance(child.func, ast.Name)
             and child.func.id == "reclaim_and_process_batch"
             for child in ast.walk(node)
         )),
        None,
    )

    assert persist_index is not None, "worker persistence callback is missing"
    assert recovery_index is not None, "worker pending-recovery loop is missing"
    assert persist_index < recovery_index, (
        "persistence callback must be defined before the first pending-recovery call"
    )
