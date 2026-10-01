"""Hybrid retrieval. The fusion tests are pure; the SQL tests need Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest

from app.rag.search import (
    RRF_K,
    ContextSearch,
    Hit,
    fuse,
    keyword_search,
    or_terms,
    rrf,
    vector_search,
)

THREAD = "test-thread-search"
DIMENSIONS = 1536


def hit(chunk_id: int, subject: str = "s", score: float = 0.0) -> Hit:
    return Hit(
        chunk_id=chunk_id,
        thread_id="t",
        message_id="m",
        subject=subject,
        content="body",
        participants=(),
        sent_at=datetime(2026, 8, 12, tzinfo=UTC),
        score=score,
    )


# --- fusion, no database ----------------------------------------------------


def test_rrf_scores_by_rank_not_by_score() -> None:
    """The whole point: cosine 0.71 and ts_rank 0.09 share no scale."""
    scores = rrf([[10, 20], [20, 10]])
    assert scores[10] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert scores[10] == pytest.approx(scores[20])


def test_agreement_between_searches_beats_a_single_first_place() -> None:
    """A document both searches like should outrank one only one of them loves."""
    consensus = fuse([hit(1), hit(2)], [hit(3), hit(2)], limit=3)
    assert consensus[0].chunk_id == 2


def test_weighting_tilts_towards_the_list_it_favours() -> None:
    """The knob M12 used to ask whether RRF's loss was inherent or just symmetry."""
    even = rrf([[1], [2]])
    tilted = rrf([[1], [2]], weights=(4.0, 1.0))

    assert even[1] == even[2]
    assert tilted[1] > tilted[2]


def test_keyword_terms_are_ored_not_anded() -> None:
    """plainto_tsquery ANDs, which made the keyword half work only on identifiers."""
    assert or_terms("who is Alice from Zetafonts") == "who or is or Alice or from or Zetafonts"


def test_a_query_with_no_word_characters_returns_nothing() -> None:
    assert or_terms("!!! ???") == ""


def test_a_document_found_by_only_one_search_still_survives() -> None:
    """Keyword-only hits are how exact strings get found at all."""
    ids = {h.chunk_id for h in fuse([hit(1)], [hit(9)], limit=5)}
    assert ids == {1, 9}


def test_fused_score_replaces_the_incomparable_ones() -> None:
    fused = fuse([hit(1, score=0.9)], [hit(1, score=0.02)], limit=1)
    assert fused[0].score == pytest.approx(2 / (RRF_K + 1))


def test_fusion_is_deterministic_on_ties() -> None:
    first = fuse([hit(3), hit(1), hit(2)], [], limit=3)
    second = fuse([hit(3), hit(1), hit(2)], [], limit=3)
    assert [h.chunk_id for h in first] == [h.chunk_id for h in second]


def test_empty_inputs_fuse_to_nothing() -> None:
    assert fuse([], [], limit=5) == []


def test_snippets_are_cut_on_a_word_boundary() -> None:
    long = Hit(
        chunk_id=1,
        thread_id="t",
        message_id="m",
        subject="s",
        content="alpha beta gamma delta epsilon",
        participants=(),
        sent_at=datetime(2026, 8, 12, tzinfo=UTC),
        score=0.0,
    )
    assert long.snippet(12) == "alpha beta…"


# --- against Postgres -------------------------------------------------------

pytestmark_integration = pytest.mark.integration


@pytest.fixture
def corpus(migrated_database: str) -> Iterator[psycopg.Connection]:
    """Three chunks with hand-made embeddings, so similarity is predictable."""
    # The sentinel tokens matter. This runs against the same database the real
    # poller and ingester use, so a query built from ordinary words ("review",
    # "payment") matches hundreds of real chunks and the assertions become a
    # measurement of somebody's actual mailbox.
    rows = [
        (
            "m1",
            0,
            "Platform review",
            "Ahmed Raza will join the platform review. sentinelalpha",
            ["ahmed@x.com"],
        ),
        (
            "m2",
            1,
            "Offsite logistics",
            "Room 2 is booked for the offsite. sentinelbeta",
            ["sara@x.com"],
        ),
        (
            "m3",
            2,
            "Invoice ORD-77Z",
            "Payment reference ORD-77Z cleared today. sentinelgamma",
            ["fin@x.com"],
        ),
    ]

    with psycopg.connect(migrated_database) as conn:
        conn.execute("DELETE FROM chunks WHERE thread_id = %s", (THREAD,))
        for index, (message_id, _, subject, content, participants) in enumerate(rows):
            vector = [0.0] * DIMENSIONS
            vector[index] = 1.0
            conn.execute(
                """
                INSERT INTO chunks (thread_id, message_id, ordinal, subject, content,
                                    content_hash, participants, sent_at, embedding)
                VALUES (%s, %s, 0, %s, %s, %s, %s, %s, %s::vector)
                """,
                (
                    THREAD,
                    message_id,
                    subject,
                    content,
                    f"{THREAD}-{message_id}",
                    participants,
                    datetime(2026, 8, 10 + index, tzinfo=UTC),
                    "[" + ",".join(str(v) for v in vector) + "]",
                ),
            )
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute("DELETE FROM chunks WHERE thread_id = %s", (THREAD,))
            conn.commit()


@pytest.mark.integration
def test_keyword_search_finds_an_exact_string(corpus: psycopg.Connection) -> None:
    """The case embeddings are worst at: a reference nobody would paraphrase."""
    hits = keyword_search(corpus, "ORD-77Z")
    assert [h.message_id for h in hits] == ["m3"]


@pytest.mark.integration
def test_keyword_search_survives_punctuation_from_a_model(corpus: psycopg.Connection) -> None:
    """plainto_tsquery, not to_tsquery -- the latter raises on stray operators."""
    assert keyword_search(corpus, "who is Ahmed? (platform review!)") != []


@pytest.mark.integration
def test_vector_search_orders_by_similarity(corpus: psycopg.Connection) -> None:
    probe = [0.0] * DIMENSIONS
    probe[1] = 1.0
    assert vector_search(corpus, probe)[0].message_id == "m2"


@pytest.mark.integration
def test_participant_filter(corpus: psycopg.Connection) -> None:
    hits = keyword_search(corpus, "review OR offsite OR payment", participant="sara@x.com")
    assert {h.message_id for h in hits} <= {"m2"}


@pytest.mark.integration
def test_since_filter(corpus: psycopg.Connection) -> None:
    hits = keyword_search(corpus, "sentinelalpha", since=datetime(2026, 8, 11, tzinfo=UTC).date())
    assert hits == []


# --- the searcher as the tool sees it ---------------------------------------


class FakeEmbeddings:
    def __init__(
        self, index: int | None = 0, fail: bool = False, runner_up: int | None = None
    ) -> None:
        self.index = index
        self.runner_up = runner_up
        self.fail = fail

    def embed_content(self, **kwargs: Any) -> Any:
        if self.fail:
            raise RuntimeError("embeddings unavailable")
        values = [0.0] * DIMENSIONS
        if self.index is not None:
            values[self.index] = 1.0
        if self.runner_up is not None:
            # A weaker second match, so the corpus has a strict vector order.
            # Otherwise both remaining rows sit at cosine distance 1 and tie.
            values[self.runner_up] = 0.5
        return SimpleNamespace(embeddings=[SimpleNamespace(values=values)])


class FakeClient:
    def __init__(self, models: FakeEmbeddings) -> None:
        self.models = models


@pytest.mark.integration
def test_the_default_mode_is_vector_only(corpus: psycopg.Connection) -> None:
    """What M12 measured. Fusion is a setting, not the default."""
    search = ContextSearch(
        conn=corpus,
        client=FakeClient(FakeEmbeddings(index=1, runner_up=2)),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
    )
    # The query text points squarely at m3; the embedding points at m2, with m3
    # as its runner-up so that m3 stays among the vector candidates however many
    # other chunks are stored. Fused, m3 would then win as the pick both halves
    # agree on. Vector alone keeps m2 first.
    assert search("sentinelgamma")[0].message_id == "m2"


@pytest.mark.integration
def test_hybrid_mode_fuses_both_halves(corpus: psycopg.Connection) -> None:
    """The embedding points at m1, then m2; only the keyword half can reach m3."""
    search = ContextSearch(
        conn=corpus,
        client=FakeClient(FakeEmbeddings(index=0, runner_up=1)),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
        mode="hybrid",
    )
    # Two slots, which the embedding alone fills before it reaches m3.
    assert {h.message_id for h in search("sentinelgamma", limit=2)} == {"m1", "m3"}


@pytest.mark.integration
def test_vector_mode_cannot_reach_a_keyword_only_match(corpus: psycopg.Connection) -> None:
    """The same search without the keyword half."""
    search = ContextSearch(
        conn=corpus,
        client=FakeClient(FakeEmbeddings(index=0, runner_up=1)),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
    )
    # Two slots again, not the default five: vector search has no similarity
    # cutoff, so on a table with few other chunks (CI's has none) it pads the
    # list with m3. Two are filled before m3 on any table -- m1, then m2 or a
    # stored chunk nearer still -- so m3 could only take one through its text.
    assert "m3" not in {h.message_id for h in search("sentinelgamma", limit=2)}


@pytest.mark.integration
def test_a_failed_embedding_degrades_to_keyword_only(corpus: psycopg.Connection) -> None:
    """Half a search beats an exception thrown three nodes deep over a lookup
    the model asked for speculatively."""
    search = ContextSearch(
        conn=corpus,
        client=FakeClient(FakeEmbeddings(fail=True)),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
    )
    assert [h.message_id for h in search("ORD-77Z")] == ["m3"]


@pytest.mark.integration
def test_the_reranker_seam_is_actually_applied(corpus: psycopg.Connection) -> None:
    search = ContextSearch(
        conn=corpus,
        client=FakeClient(FakeEmbeddings(index=0, runner_up=1)),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
        rerank=lambda _query, hits: list(reversed(hits)),
    )
    plain: ContextSearch = ContextSearch(
        conn=corpus,
        client=FakeClient(FakeEmbeddings(index=0, runner_up=1)),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
    )
    query = "review offsite payment"
    assert [h.chunk_id for h in search(query)] == [h.chunk_id for h in reversed(plain(query))]


@pytest.mark.parametrize(
    "refusal", ["BudgetExhaustedError", "UnpricedModelError", "MessageTooCostlyError"]
)
def test_a_refusal_by_the_spend_gate_is_not_degraded_away(refusal: str) -> None:
    """Stopping is the point (M17, D5): keyword search must not quietly go on
    where the gate said no."""
    from app.policy import budget

    error = getattr(budget, refusal)

    class Refused:
        def embed_content(self, **kwargs: Any) -> Any:
            raise error("no")

    search = ContextSearch(
        conn=cast(psycopg.Connection, None),
        client=FakeClient(cast(FakeEmbeddings, Refused())),
        embedding_model="fake",
        embedding_dimensions=DIMENSIONS,
    )
    with pytest.raises(error):
        search("anything")
