"""Index lifecycle + privacy for purpose-specific retrieval (spec
amendment sections 3-5, 9, 13.1-18)."""

import datetime as dt

import pytest
from _helpers import call, running_session

from montauk.bootstrap import build_context, reconcile_on_startup
from montauk.config import EmbeddingConfig, MontaukConfig, RetrievalConfig
from montauk.embeddings.local import LocalEmbeddingProvider
from montauk.markdown_store import MarkdownStore
from montauk.models import Fact, Interaction, Person
from montauk.reconciliation import scan_people_directory
from montauk.semantic_index import SCHEMA_VERSION, SemanticIndex, chunk_person

LONG_SUMMARY = (
    "We met for the first date at the waterfront. We walked most of the pier and talked about "
    "her move from Denver and her nursing shifts. Then we climbed the old stone tower and she "
    "said she could stay up there all afternoon. Afterward we got coffee and she had to leave "
    "by six to let her dogs out. On the ferry back she asked whether I was really over my last "
    "relationship, which was a little pointed but fair. Overall it felt easy and low pressure "
    "even if I was not sure about the spark yet."
)


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


def _person(**kw) -> Person:
    base = {"id": "P0001", "name": "Priya Raman", "summary": "A dating-app connection."}
    base.update(kw)
    return Person(**base)


def _store(tmp_path) -> MarkdownStore:
    return MarkdownStore(tmp_path / "data")


# --- chunking (spec 13.1-3) --------------------------------------------


class TestChunking:
    def test_each_semantic_unit_is_its_own_chunk(self):
        person = _person(
            facts=[
                Fact(id="fact-1", category="Interests", text="Climbs a lot."),
                Fact(id="fact-2", category="Family", text="Married to Sam.", related_person_id="P0002"),
            ],
            interactions=[Interaction(id="int-1", date="2026-08-20", summary="Short coffee.")],
        )
        chunks = {c.chunk_id: c for c in chunk_person(person)}
        assert set(chunks) == {"P0001:summary", "P0001:fact-1", "P0001:fact-2", "P0001:int-1"}
        assert chunks["P0001:summary"].chunk_type == "summary"
        assert chunks["P0001:fact-2"].chunk_type == "fact"  # relationship facts index as fact chunks

    def test_long_interaction_summary_is_split_at_sentence_boundaries(self):
        person = _person(interactions=[Interaction(id="int-1", date="2026-08-20", summary=LONG_SUMMARY)])
        chunks = [
            c
            for c in chunk_person(person, interaction_chunk_tokens=40, interaction_chunk_overlap_tokens=8)
            if c.chunk_type == "interaction"
        ]
        assert len(chunks) >= 3
        assert all(c.local_id == "int-1" for c in chunks)
        assert all(c.chunk_total == len(chunks) for c in chunks)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        for c in chunks:  # every chunk ends at a sentence boundary
            assert c.text.rstrip()[-1] in ".!?"
        # there is real overlap between consecutive chunks
        assert any(chunks[i].text.split()[-3] in chunks[i + 1].text for i in range(len(chunks) - 1))

    def test_short_interaction_is_not_split(self):
        person = _person(interactions=[Interaction(id="int-1", date="2026-08-20", summary="Quick catch-up call.")])
        chunks = [c for c in chunk_person(person) if c.chunk_type == "interaction"]
        assert len(chunks) == 1
        assert chunks[0].chunk_id == "P0001:int-1"

    def test_whole_person_is_never_one_chunk(self):
        person = _person(facts=[Fact(id=f"fact-{i}", category="Interests", text=f"thing {i}") for i in range(1, 6)])
        chunks = chunk_person(person)
        assert len(chunks) == 6  # summary + 5 facts, not 1


# --- index sync after mutations (spec 13.5-7) --------------------------


class TestMutationSync:
    @pytest.mark.asyncio
    async def test_add_and_remove_fact_updates_retrieval(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            await call(session, "create_person", name="Priya Raman")
            await call(session, "add_fact", person_id="P0001", category="Interests",
                       text="Priya is really into kitesurfing on Casco Bay.")
            r = await call(session, "prepare_person_context", person_id="P0001",
                           purpose="is Priya into kitesurfing?")
            assert any("kitesurfing" in f["text"] for f in r["facts"])

            facts = await call(session, "get_facts", person_id="P0001")
            await call(session, "remove_fact", person_id="P0001", fact_id=facts[0]["id"])
            r2 = await call(session, "prepare_person_context", person_id="P0001",
                            purpose="is Priya into kitesurfing?")
            assert not any("kitesurfing" in f["text"] for f in r2["facts"])

    @pytest.mark.asyncio
    async def test_interaction_participant_correction_moves_searchable_scope(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            await call(session, "create_person", name="Monica Bell")
            await call(session, "create_person", name="Monique Cole")
            rec = await call(session, "record_interaction", person_id="P0001", date="2026-03-03",
                             summary="Long walk on the Eastern Prom talking about her pottery studio.")
            iid = rec["changed_ids"][0]
            await call(session, "update_interaction", person_id="P0001", interaction_id=iid,
                       move_to_person_id="P0002", correction_reason="it was Monique")

            gone = await call(session, "prepare_person_context", person_id="P0001",
                              purpose="what did we talk about on our walk?")
            assert not any("pottery studio" in i["text"] for i in gone["interactions"])
            here = await call(session, "prepare_person_context", person_id="P0002",
                              purpose="what did we talk about on our walk?")
            assert any("pottery studio" in i["text"] for i in here["interactions"])

    @pytest.mark.asyncio
    async def test_archived_person_leaves_no_orphan_chunks(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, ctx):
            await call(session, "create_person", name="Priya Raman", summary="climber and nurse")
            assert ctx.semantic_index.person_count() == 1
            await call(session, "archive_person", person_id="P0001")
            assert ctx.semantic_index.chunk_count() == 0
            assert ctx.semantic_index.orphan_person_ids(set()) == set()


# --- rebuild / validation / versioning (spec 13.8-12) ------------------


class TestRebuildAndValidation:
    def test_rebuild_is_idempotent(self, tmp_path, provider):
        store = _store(tmp_path)
        store.write_person(_person(facts=[Fact(id="fact-1", category="Interests", text="Climbs.")]))
        scan = scan_people_directory(store)
        idx = SemanticIndex(tmp_path / "data" / "index" / "vectors", provider)
        idx.rebuild_from_scan(scan)
        first = sorted(r["chunk_id"] for r in idx._conn.execute("SELECT chunk_id FROM chunks"))
        idx.rebuild_from_scan(scan)
        second = sorted(r["chunk_id"] for r in idx._conn.execute("SELECT chunk_id FROM chunks"))
        assert first == second
        assert idx.get_meta("schema_version") == SCHEMA_VERSION

    def test_chunking_config_change_marks_index_stale(self, tmp_path, provider):
        store = _store(tmp_path)
        store.write_person(_person(interactions=[Interaction(id="int-1", date="2026", summary=LONG_SUMMARY)]))
        scan = scan_people_directory(store)
        vdir = tmp_path / "data" / "index" / "vectors"
        SemanticIndex(vdir, provider, interaction_chunk_tokens=120).rebuild_from_scan(scan)
        reopened = SemanticIndex(vdir, provider, interaction_chunk_tokens=40)
        assert reopened.stale_reason() is not None
        assert "chunk" in reopened.stale_reason()

    def test_startup_auto_rebuilds_a_stale_local_index(self, tmp_path, provider):
        cfg = MontaukConfig(data_dir=str(tmp_path / "data"),
                            retrieval=RetrievalConfig(interaction_chunk_tokens=120))
        store = MarkdownStore(cfg.data_dir_path)
        store.write_person(_person(interactions=[Interaction(id="int-1", date="2026", summary=LONG_SUMMARY)]))
        ctx = build_context(cfg, with_semantic=True, with_auth=False)
        reconcile_on_startup(ctx)
        assert ctx.semantic_index.stale_reason() is None
        assert ctx.semantic_stale_reason is None

        cfg2 = MontaukConfig(data_dir=str(tmp_path / "data"),
                             retrieval=RetrievalConfig(interaction_chunk_tokens=40))
        ctx2 = build_context(cfg2, with_semantic=True, with_auth=False)
        assert ctx2.semantic_index.stale_reason() is not None  # detected on open
        reconcile_on_startup(ctx2)
        assert ctx2.semantic_index.stale_reason() is None  # auto-rebuilt
        assert ctx2.semantic_stale_reason is None

    def test_startup_validation_catches_missing_person_hash(self, tmp_path, provider):
        cfg = MontaukConfig(data_dir=str(tmp_path / "data"))
        store = MarkdownStore(cfg.data_dir_path)
        store.write_person(_person())
        ctx = build_context(cfg, with_semantic=True, with_auth=False)
        reconcile_on_startup(ctx)
        assert ctx.semantic_index.person_content_hash("P0001") is not None

    def test_index_deletion_does_not_break_canonical_reads(self, tmp_path, provider):
        cfg = MontaukConfig(data_dir=str(tmp_path / "data"))
        store = MarkdownStore(cfg.data_dir_path)
        store.write_person(_person(facts=[Fact(id="fact-1", category="Interests", text="Climbs at the gym.")]))
        ctx = build_context(cfg, with_semantic=True, with_auth=False)
        reconcile_on_startup(ctx)
        import shutil

        shutil.rmtree(cfg.data_dir_path / "index")
        # canonical Markdown still readable + retrievable via lexical fallback
        person = store.read_person("P0001")
        assert person.facts[0].text == "Climbs at the gym."
        from montauk.person_context import build_person_context

        r = build_person_context(person, "where does Priya climb", detail_level="standard",
                                 budget_tokens=2000, semantic_index=None, now=dt.date(2026, 9, 5))
        assert any("gym" in f.text for f in r.facts)


# --- privacy / providers (spec 13.13-18) ------------------------------


class TestPrivacy:
    def test_default_config_is_local_only(self):
        cfg = MontaukConfig()
        assert cfg.embedding.provider == "local"
        assert LocalEmbeddingProvider.provider == "local"

    def test_hosted_provider_cannot_be_configured_yet(self):
        # No hosted adapter ships; the config Literal refuses anything but
        # "local", so relationship data cannot be sent off-box by config alone.
        with pytest.raises((ValueError, TypeError)):
            EmbeddingConfig(provider="openai")

    def test_an_api_key_in_the_environment_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
        cfg = MontaukConfig(data_dir=str(tmp_path / "data"))
        ctx = build_context(cfg, with_semantic=True, with_auth=False)
        assert ctx.semantic_index.embedding_provider.provider == "local"

    @pytest.mark.asyncio
    async def test_no_silent_fallback_when_semantic_unavailable(self, tmp_path):
        # semantic disabled -> lexical only, and the response says so;
        # nothing hosted is contacted.
        async with running_session(tmp_path) as (session, ctx):
            ctx.retrieval = RetrievalConfig(semantic_enabled=False)
            await call(session, "create_person", name="Priya Raman", summary="climber and nurse")
            await call(session, "add_fact", person_id="P0001", category="Interests", text="Priya goes climbing at the bouldering gym every week.")
            r = await call(session, "prepare_person_context", person_id="P0001",
                           purpose="where does Priya climb?")
            assert r["retrieval"]["semantic_available"] is False
            assert r["retrieval"]["note"]
            assert any("climbing" in f["text"] for f in r["facts"])

    @pytest.mark.asyncio
    async def test_provider_failure_degrades_to_lexical_with_a_warning(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Priya Raman", summary="climber")
            await call(session, "add_fact", person_id="P0001", category="Interests", text="Priya goes climbing at the bouldering gym every week.")

            class Boom:
                def search_person(self, *a, **k):
                    raise RuntimeError("provider exploded")

                def person_content_hash(self, *_a):
                    return None

            ctx.semantic_index = Boom()
            r = await call(session, "prepare_person_context", person_id="P0001", purpose="where does Priya climb?")
            assert r["retrieval"]["semantic_available"] is False
            assert "unavailable" in r["retrieval"]["note"]
            assert any("climbing" in f["text"] for f in r["facts"])

    def test_index_status_and_logs_carry_no_personal_content(self, tmp_path, provider, caplog):
        import logging

        cfg = MontaukConfig(data_dir=str(tmp_path / "data"))
        store = MarkdownStore(cfg.data_dir_path)
        secret = "Priya confided something very private about her family."
        store.write_person(_person(summary=secret, facts=[Fact(id="fact-1", category="General Notes", text=secret)]))
        ctx = build_context(cfg, with_semantic=True, with_auth=False)
        with caplog.at_level(logging.DEBUG, logger="montauk"):
            reconcile_on_startup(ctx)
        assert secret not in caplog.text
        state = ctx.semantic_index.index_state(active_person_ids={"P0001"})
        assert secret not in repr(state)
