"""Shared fixtures and flow-building helpers for the test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ivr.backend import Backend
from ivr.directory import Directory, default_directory
from ivr.flow import Flow, flow_from_dict, load_flow

REPO_ROOT = Path(__file__).resolve().parent.parent
FLOWS_DIR = REPO_ROOT / "flows"

# Showcase customers from ivr.directory, by name.
AVERY_ANI = "5550123456"
AVERY_ACCOUNT = "4155550124"
JORDAN_ANI = "5550100200"
JORDAN_ACCOUNT = "4155550207"
RILEY_ANI = "5550100300"
RILEY_ACCOUNT = "4155550306"
UNKNOWN_ANI = "5550100999"


def build_flow(
    nodes: dict[str, dict[str, Any]],
    start: str = "start",
    queues: tuple[str, ...] = ("general",),
    name: str = "test",
    version: str = "1",
) -> Flow:
    """Build a flow from a plain node dictionary."""
    return flow_from_dict(
        {"name": name, "version": version, "start": start, "queues": list(queues), "nodes": nodes}
    )


def menu(prompt: str, options: dict[str, tuple[str, list[str]]], **overrides: Any) -> dict[str, Any]:
    """A menu node dict; ``options`` maps key -> (next, intents)."""
    node: dict[str, Any] = {
        "type": "menu",
        "prompt": prompt,
        "options": {
            key: {"label": f"option {key}", "next": nxt, "intents": intents}
            for key, (nxt, intents) in options.items()
        },
        "invalid_prompt": "Invalid.",
        "timeout_prompt": "Silence.",
        "max_retries": 2,
        "on_max_retries": "fallback",
        "timeout_seconds": 5,
    }
    node.update(overrides)
    return node


def collect(variable: str = "account", **overrides: Any) -> dict[str, Any]:
    """A collect node dict with account-number defaults."""
    node: dict[str, Any] = {
        "type": "collect",
        "prompt": "Enter digits.",
        "variable": variable,
        "min_digits": 10,
        "max_digits": 10,
        "terminator": "#",
        "validation": "account",
        "confirm": False,
        "invalid_prompt": "Bad digits.",
        "timeout_prompt": "Nothing entered.",
        "max_retries": 2,
        "on_max_retries": "fallback",
        "next": "done",
    }
    node.update(overrides)
    return node


TRANSFER_FALLBACK = {"type": "transfer", "queue": "general", "priority": 4, "reason": "fallback"}
HANGUP_DONE = {"type": "hangup", "prompt": "Bye."}


@pytest.fixture(scope="session")
def care_v1() -> Flow:
    return load_flow(FLOWS_DIR / "care_v1.json")


@pytest.fixture(scope="session")
def care_v2() -> Flow:
    return load_flow(FLOWS_DIR / "care_v2.json")


@pytest.fixture(scope="session")
def directory() -> Directory:
    return default_directory()


@pytest.fixture(scope="session")
def backend(directory: Directory) -> Backend:
    return Backend(directory)
