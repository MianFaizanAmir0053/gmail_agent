"""What M24 counts decisions by: the pipeline version and the action type.

A version that moves when nothing that shapes a proposal changed would reset
M24's evidence for nothing; one that stays put when a prompt changed would
pool approvals of two different pipelines. Both directions are tested.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agents import reviewer
from app.config import Settings
from app.extraction import prompts
from app.graph.versioning import action_type, pipeline_version


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql://x/y",
        "gemini_api_key": "k",
        "reviewer_enabled": False,
        "search_context_enabled": True,
    }
    return Settings(**(base | overrides))


def test_the_same_settings_give_the_same_version() -> None:
    assert pipeline_version(_settings()) == pipeline_version(_settings())


@pytest.mark.parametrize(
    "change",
    [
        {"reviewer_enabled": True},
        {"search_context_enabled": False},
        {"extraction_model": "another-extraction-model"},
        {"classify_model": "another-classify-model"},
    ],
)
def test_what_shapes_a_proposal_changes_the_version(change: dict[str, Any]) -> None:
    assert pipeline_version(_settings(**change)) != pipeline_version(_settings())


@pytest.mark.parametrize(
    "change",
    [
        {"user_timezone": "Europe/London"},
        {"dry_run": False},
        {"poll_interval_minutes": 17},
        {"embedding_model": "another-embedding-model"},
    ],
)
def test_what_does_not_shape_a_proposal_leaves_the_version(change: dict[str, Any]) -> None:
    assert pipeline_version(_settings(**change)) == pipeline_version(_settings())


def test_the_reviewer_model_counts_only_while_the_reviewer_runs() -> None:
    on = _settings(reviewer_enabled=True)
    on_other = _settings(reviewer_enabled=True, reviewer_model="another-reviewer-model")
    off = _settings(reviewer_enabled=False)
    off_other = _settings(reviewer_enabled=False, reviewer_model="another-reviewer-model")

    assert pipeline_version(on) != pipeline_version(on_other)
    assert pipeline_version(off) == pipeline_version(off_other)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (prompts, "EXTRACT_SYSTEM"),
        (prompts, "CLASSIFY_SYSTEM"),
        (prompts, "MEETING_QUESTION"),
        (prompts, "SEARCH_SUFFIX"),
    ],
)
def test_a_prompt_change_changes_the_version(
    monkeypatch: pytest.MonkeyPatch, module: Any, name: str
) -> None:
    before = pipeline_version(_settings())
    monkeypatch.setattr(module, name, getattr(module, name) + "\nOne more instruction.")
    assert pipeline_version(_settings()) != before


def test_the_reviewer_prompt_counts_only_while_the_reviewer_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_before = pipeline_version(_settings(reviewer_enabled=True))
    off_before = pipeline_version(_settings(reviewer_enabled=False))

    monkeypatch.setattr(reviewer, "REVIEWER_SYSTEM", reviewer.REVIEWER_SYSTEM + "\nBe stricter.")

    assert pipeline_version(_settings(reviewer_enabled=True)) != on_before
    assert pipeline_version(_settings(reviewer_enabled=False)) == off_before


def test_the_search_prompt_counts_only_while_search_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    off_before = pipeline_version(_settings(search_context_enabled=False))
    monkeypatch.setattr(prompts, "SEARCH_SUFFIX", prompts.SEARCH_SUFFIX + "\nSearch more.")
    assert pipeline_version(_settings(search_context_enabled=False)) == off_before


def test_the_version_is_short_and_never_the_legacy_tag() -> None:
    version = pipeline_version(_settings())
    assert len(version) == 12
    assert version != "pre-m16"


def test_an_event_with_no_guests_is_a_hold() -> None:
    assert action_type([]) == "calendar_hold"


def test_an_event_with_guests_is_an_invite() -> None:
    assert action_type(["sara@example.com"]) == "calendar_invite"
