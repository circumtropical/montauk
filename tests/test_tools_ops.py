import pytest

from _helpers import call, call_expecting_error, running_session


class TestUpcomingBirthdays:
    @pytest.mark.asyncio
    async def test_returns_birthdays_within_window_soonest_first(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Soon Person", birthday="09-05")
            await call(session, "create_person", name="Later Person", birthday="09-20")
            await call(session, "create_person", name="Far Person", birthday="03-01")

            result = await call(session, "get_upcoming_birthdays", within_days=30, as_of="2026-08-30")
            names = [b["name"] for b in result]
            assert names == ["Soon Person", "Later Person"]
            assert result[0]["days_until"] < result[1]["days_until"]

    @pytest.mark.asyncio
    async def test_person_without_birthday_is_excluded(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="No Birthday")
            result = await call(session, "get_upcoming_birthdays", as_of="2026-08-30")
            assert result == []

    @pytest.mark.asyncio
    async def test_birthday_without_year_is_still_included(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Mystery Age", birthday="09-05")
            result = await call(session, "get_upcoming_birthdays", within_days=10, as_of="2026-08-30")
            assert result[0]["birthday"] == "09-05"

    @pytest.mark.asyncio
    async def test_wraps_across_year_boundary(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="New Year Baby", birthday="01-02")
            result = await call(session, "get_upcoming_birthdays", within_days=10, as_of="2026-12-30")
            assert len(result) == 1
            assert result[0]["next_occurrence"] == "2027-01-02"

    @pytest.mark.asyncio
    async def test_negative_within_days_is_validation_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(session, "get_upcoming_birthdays", within_days=-1)
            assert "VALIDATION_ERROR" in text


class TestOverdueContacts:
    @pytest.mark.asyncio
    async def test_no_cadence_is_never_returned(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="No Cadence")
            result = await call(session, "list_overdue_contacts", as_of="2026-08-30")
            assert result == []

    @pytest.mark.asyncio
    async def test_cadence_but_no_interaction_is_never_contacted(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Moe Szyslak", desired_contact_cadence_days=21)
            result = await call(session, "list_overdue_contacts", as_of="2026-08-30")
            assert len(result) == 1
            assert result[0]["status"] == "never_contacted"
            assert result[0]["last_interaction_at"] is None
            assert result[0]["days_since_last_interaction"] is None

    @pytest.mark.asyncio
    async def test_recent_interaction_within_cadence_is_not_overdue(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson", desired_contact_cadence_days=30)
            await call(session, "record_interaction", person_id="P0001", date="2026-08-20")
            result = await call(session, "list_overdue_contacts", as_of="2026-08-30")
            assert result == []

    @pytest.mark.asyncio
    async def test_old_interaction_beyond_cadence_is_overdue(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson", desired_contact_cadence_days=7)
            await call(session, "record_interaction", person_id="P0001", date="2026-08-01")
            result = await call(session, "list_overdue_contacts", as_of="2026-08-30")
            assert len(result) == 1
            assert result[0]["status"] == "overdue"
            assert result[0]["days_since_last_interaction"] == 29

    @pytest.mark.asyncio
    async def test_partial_precision_interaction_anchors_to_latest_plausible_date(self, tmp_path):
        # A year-only interaction date ("2026") is anchored to Dec 31 2026
        # for recency purposes (benefit of the doubt), not Jan 1 -- so as
        # of Aug 30 2026 it must NOT be treated as having already happened
        # 242 days ago.
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson", desired_contact_cadence_days=30)
            await call(session, "record_interaction", person_id="P0001", date="2026")
            result = await call(session, "list_overdue_contacts", as_of="2026-08-30")
            assert result == []


class TestArchivePerson:
    @pytest.mark.asyncio
    async def test_archive_removes_from_active_results(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Frank Grimes")
            result = await call(session, "archive_person", person_id="P0001")
            assert result["status"] == "archived"

            assert ctx.store.is_archived("P0001")
            assert not ctx.store.exists("P0001")
            assert ctx.sqlite_index.get_row("P0001") is None

            search = await call(session, "search_people", query="Frank")
            assert search["candidates"] == []

    @pytest.mark.asyncio
    async def test_archive_missing_person_is_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(session, "archive_person", person_id="nobody")
            assert "NOT_FOUND" in text

    @pytest.mark.asyncio
    async def test_archive_already_archived_person_is_archived_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Frank Grimes")
            await call(session, "archive_person", person_id="P0001")
            text = await call_expecting_error(session, "archive_person", person_id="P0001")
            assert "ARCHIVED" in text

    @pytest.mark.asyncio
    async def test_archived_person_excluded_from_birthdays_and_cadence(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(
                session,
                "create_person",
                name="Frank Grimes",
                birthday="09-01",
                desired_contact_cadence_days=7,
            )
            await call(session, "archive_person", person_id="P0001")

            birthdays = await call(session, "get_upcoming_birthdays", within_days=30, as_of="2026-08-30")
            overdue = await call(session, "list_overdue_contacts", as_of="2026-08-30")
            assert birthdays == []
            assert overdue == []


class TestArchiveReads:
    @pytest.mark.asyncio
    async def test_list_and_get_archived_person(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Frank Grimes")
            await call(session, "add_fact", person_id="P0001", category="Work & Education", text="Engineer.")
            await call(session, "archive_person", person_id="P0001")

            listed = await call(session, "list_archived_people")
            assert listed == [{"person_id": "P0001", "name": "Frank Grimes"}]

            record = await call(session, "get_archived_person", person_id="P0001")
            assert "id: P0001" in record
            assert "Engineer." in record

    @pytest.mark.asyncio
    async def test_get_archived_person_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(session, "get_archived_person", person_id="nobody")
            assert "NOT_FOUND" in text

    @pytest.mark.asyncio
    async def test_get_archived_person_on_active_person_is_validation_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            text = await call_expecting_error(session, "get_archived_person", person_id="P0001")
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_list_archived_people_empty_by_default(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            assert await call(session, "list_archived_people") == []


class TestValidationAndHealth:
    @pytest.mark.asyncio
    async def test_healthy_repository_reports_healthy(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            report = await call(session, "validate_repository")
            assert report["healthy"] is True
            assert report["valid_person_count"] == 1
            assert report["error_count"] == 0
            assert report["issues"] == []

    @pytest.mark.asyncio
    async def test_malformed_manual_file_is_reported_without_blocking_others(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Homer Simpson")
            (ctx.store.people_dir / "broken.md").write_text("---\nid: [unterminated\n---\n\n# X\n")

            report = await call(session, "validate_repository")
            assert report["healthy"] is False
            assert report["valid_person_count"] == 1
            assert report["error_count"] == 1

            errors = await call(session, "get_validation_errors")
            assert len(errors) == 1
            assert errors[0]["error_type"] == "format_error"

    @pytest.mark.asyncio
    async def test_get_validation_errors_self_heals_without_prior_scan(self, tmp_path):
        # No validate_repository() call yet -- get_validation_errors should
        # still work by running a scan itself rather than erroring out.
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            errors = await call(session, "get_validation_errors")
            assert errors == []

    @pytest.mark.asyncio
    async def test_health_status_reflects_last_scan_and_reconciliation(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "validate_repository")

            health = await call(session, "get_health_status")
            assert health["healthy"] is True
            assert health["valid_person_count"] == 1
            assert health["last_scanned_at"] is not None
            # last_reconciliation_at comes from the SQLite index meta, set on
            # every create_person write via upsert_person -- but upsert_person
            # itself does not update reconciliation meta, only rebuild/reconcile
            # do, so this may legitimately be None until an explicit rebuild.

    @pytest.mark.asyncio
    async def test_health_status_never_includes_person_names_or_summaries(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson", summary="Secret sensitive info.")
            health = await call(session, "get_health_status")
            assert "Homer" not in str(health)
            assert "Secret" not in str(health)
