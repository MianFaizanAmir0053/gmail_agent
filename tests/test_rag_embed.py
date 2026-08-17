"""Embedding, with particular attention to the batch that silently misaligns."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from app.rag.embed import (
    DOCUMENT_TASK,
    QUERY_TASK,
    EmbeddingError,
    Pacer,
    embed_documents,
    embed_query,
    estimated_tokens,
    normalise,
    to_pgvector,
)


class FakeModels:
    """Stands in for `client.models`, recording what it was handed."""

    def __init__(self, dimensions: int = 4, returns: int | None = None) -> None:
        self.dimensions = dimensions
        self.returns = returns
        self.calls: list[dict[str, Any]] = []

    def embed_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        contents = kwargs["contents"]
        count = self.returns if self.returns is not None else len(contents)
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(values=[float(i + 1)] * self.dimensions) for i in range(count)
            ]
        )


class FakeClient:
    def __init__(self, models: FakeModels) -> None:
        self.models = models


# --- the trap ---------------------------------------------------------------


def test_a_short_batch_raises_instead_of_misaligning() -> None:
    """`gemini-embedding-2` returns one vector for a list of bare strings.

    Zipped against its inputs that files every embedding under the wrong chunk,
    and the only symptom is retrieval that is confidently irrelevant forever.
    """
    client = FakeClient(FakeModels(returns=1))

    with pytest.raises(EmbeddingError, match="misaligned"):
        embed_documents(client, ["a", "b", "c"], model="fake", dimensions=4)


def test_each_input_is_sent_as_its_own_content() -> None:
    """The shape that behaves identically across models. Bare strings do not."""
    models = FakeModels()
    embed_documents(FakeClient(models), ["a", "b"], model="fake", dimensions=4)

    contents = models.calls[0]["contents"]
    assert len(contents) == 2
    assert all(len(c.parts) == 1 for c in contents)


def test_wrong_width_raises() -> None:
    client = FakeClient(FakeModels(dimensions=8))
    with pytest.raises(EmbeddingError, match="8 dimensions, expected 4"):
        embed_documents(client, ["a"], model="fake", dimensions=4)


# --- normalisation ----------------------------------------------------------


def test_vectors_come_back_unit_length() -> None:
    """Sub-3072 output is un-normalised, at a norm of roughly 0.69."""
    models = FakeModels()
    vectors, _ = embed_documents(FakeClient(models), ["a"], model="fake", dimensions=4)
    assert math.isclose(math.sqrt(sum(v * v for v in vectors[0])), 1.0, rel_tol=1e-9)


def test_a_zero_vector_survives_normalisation() -> None:
    assert normalise([0.0, 0.0]) == [0.0, 0.0]


# --- batching and task types ------------------------------------------------


def test_batching_splits_into_the_expected_number_of_requests() -> None:
    models = FakeModels()
    vectors, calls = embed_documents(
        FakeClient(models), [f"t{i}" for i in range(70)], model="fake", dimensions=4, batch_size=32
    )
    assert calls == 3
    assert len(vectors) == 70


def test_documents_and_queries_use_different_task_types() -> None:
    """Embedding a query as a document degrades recall without any error."""
    models = FakeModels()
    client = FakeClient(models)

    embed_documents(client, ["a"], model="fake", dimensions=4)
    embed_query(client, "a", model="fake", dimensions=4)

    assert models.calls[0]["config"].task_type == DOCUMENT_TASK
    assert models.calls[1]["config"].task_type == QUERY_TASK


# --- pacing -----------------------------------------------------------------


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_the_pacer_does_not_wait_below_the_limit() -> None:
    clock = FakeClock()
    pacer = Pacer(per_minute=100, sleep=clock.sleep, clock=clock)

    for _ in range(3):
        pacer.reserve(32)

    assert clock.slept == []


def test_the_pacer_waits_out_the_window_when_full() -> None:
    """The quota counts documents, not requests, so batching does not evade it."""
    clock = FakeClock()
    pacer = Pacer(per_minute=100, sleep=clock.sleep, clock=clock)

    for _ in range(4):
        pacer.reserve(32)

    assert clock.slept
    assert clock.now >= 60.0


def test_the_window_slides_rather_than_resetting() -> None:
    clock = FakeClock()
    pacer = Pacer(per_minute=100, sleep=clock.sleep, clock=clock)

    pacer.reserve(90)
    clock.now += 61.0
    pacer.reserve(90)

    assert clock.slept == []


def test_a_batch_larger_than_the_whole_quota_still_proceeds() -> None:
    """Otherwise an oversized batch would sleep for ever waiting for room."""
    clock = FakeClock()
    Pacer(per_minute=10, sleep=clock.sleep, clock=clock).reserve(32)
    assert clock.slept == []


def test_embed_documents_reserves_the_batch_it_is_about_to_send() -> None:
    clock = FakeClock()
    pacer = Pacer(per_minute=100, sleep=clock.sleep, clock=clock)

    embed_documents(
        FakeClient(FakeModels()),
        [f"t{i}" for i in range(150)],
        model="fake",
        dimensions=4,
        batch_size=32,
        pacer=pacer,
    )

    assert clock.slept


# --- small helpers ----------------------------------------------------------


def test_pgvector_literal_shape() -> None:
    assert to_pgvector([1.0, -0.5]) == "[1,-0.5]"


def test_token_estimate_is_characters_over_four() -> None:
    assert estimated_tokens(["x" * 400]) == 100
