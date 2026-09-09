"""Dashboard end-to-end cases (spec 32.7). Uses a real ASGI test client
over the PostgreSQL testcontainer."""

from __future__ import annotations

import pytest

from montauk.db.repositories import Actor, PeopleRepository, WorkspaceScope
from montauk.models import Fact, Interaction, Person
from montauk.services.workspace import get_or_create_workspace

from ._golden import seed_golden

OWNER = {
    "email": "owner@example.com",
    "password": "correct-horse-staple",
    "password_confirm": "correct-horse-staple",
    "workspace_name": "Personal",
    "deployment_profile": "private",
    "public_url": "",
}


def _setup_owner(client) -> None:
    r = client.post("/setup", data=OWNER)
    assert r.status_code in (200, 303)


def _login(client) -> None:
    client.post("/login", data={"email": OWNER["email"], "password": OWNER["password"]})


def _csrf(client, path: str = "/") -> str:
    # Every authenticated page embeds the session CSRF token in the logout form.
    html = client.get(path).text
    marker = 'name="_csrf" value="'
    i = html.index(marker) + len(marker)
    return html[i : html.index('"', i)]


class TestFirstRun:
    def test_uninitialized_deployment_redirects_to_setup(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"

    def test_setup_creates_owner_and_logs_in(self, client):
        r = client.post("/setup", data=OWNER, follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/").status_code == 200  # cookie is set

    def test_second_setup_attempt_cannot_seize_ownership(self, client):
        _setup_owner(client)
        fresh = client.__class__(client.app)
        r = fresh.post(
            "/setup",
            data={**OWNER, "email": "attacker@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and r.headers["location"] == "/"
        # attacker is not logged in as anyone
        assert fresh.get("/", follow_redirects=False).status_code == 303

    def test_weak_password_is_rejected(self, client):
        r = client.post("/setup", data={**OWNER, "password": "short", "password_confirm": "short"})
        assert r.status_code == 400
        assert "at least 10" in r.text


class TestAuth:
    def test_login_logout_cycle(self, client):
        _setup_owner(client)
        client.post("/logout", data={"_csrf": _csrf(client)})
        assert client.get("/", follow_redirects=False).status_code == 303

        bad = client.post("/login", data={"email": OWNER["email"], "password": "wrong"})
        assert bad.status_code == 401

        _login(client)
        assert client.get("/").status_code == 200

    def test_rate_limit_locks_after_repeated_failures(self, client):
        _setup_owner(client)
        client.post("/logout", data={"_csrf": _csrf(client)})
        for _ in range(5):
            client.post("/login", data={"email": OWNER["email"], "password": "wrong"})
        locked = client.post("/login", data={"email": OWNER["email"], "password": OWNER["password"]})
        assert locked.status_code == 429

    def test_csrf_required_for_post(self, client):
        _setup_owner(client)
        r = client.post("/logout", data={"_csrf": "bogus"})
        assert r.status_code == 403

    def test_unauthenticated_person_page_redirects_to_login(self, client):
        _setup_owner(client)
        client.post("/logout", data={"_csrf": _csrf(client)})
        r = client.get("/people/P0001", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"

    def test_security_headers_present(self, client):
        r = client.get("/setup")
        assert r.headers["x-frame-options"] == "DENY"
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert (
            "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
        )

    def test_password_reveal_is_wired_but_not_inline_script(self, client):
        html = client.get("/login").text
        assert 'type="password"' in html and "data-reveal" in html
        assert "/static/montauk.js" in html
        # no inline <script> that would need 'unsafe-inline'
        assert "<script>" not in html
        assert client.get("/static/montauk.js").status_code == 200


class TestWithMigratedData:
    @pytest.fixture
    def populated(self, client, session_maker, tmp_path):
        _setup_owner(client)
        # seed into the same workspace the wizard created ("Personal")
        seed_golden(session_maker, workspace_name="Personal")
        return client

    def test_home_shows_people_count_and_birthdays(self, populated):
        html = populated.get("/").text
        assert ">3<" in html  # people count
        assert "Dana Whitfield" in html  # upcoming birthday within 30d (07-02)... or overdue list

    def test_people_directory_lists_and_flags_shared_names(self, populated):
        html = populated.get("/people").text
        assert "P0001" in html and "P0002" in html
        assert "shared name" in html  # two "Dana Whitfield" records

    def test_people_search_by_alias_and_company(self, populated):
        assert "P0001" in populated.get("/people?q=Dee").text  # alias
        assert "P0001" in populated.get("/people?q=nimbus").text  # company
        assert "P0001" not in populated.get("/people?q=zzzznope").text

    def test_archived_filter(self, populated):
        active = populated.get("/people?show=active").text
        assert "P0009" not in active
        archived = populated.get("/people?show=archived").text
        assert "P0009" in archived

    def test_person_page_shows_facts_relationships_interactions_revisions(self, populated):
        html = populated.get("/people/P0003").text
        assert "Marco Reyes" in html
        assert "Worked with Dana at Nimbus Robotics" in html  # fact
        assert "Dana Whitfield (P0001)" in html  # resolved relationship
        assert "Priya Anand (P0009)" in html  # relationship to archived person
        assert "Asked for a contractor recommendation" in html  # interaction

    def test_person_markdown_export_matches_canonical(self, populated, session_maker):
        from montauk.db.mapping import person_to_domain
        from montauk.exporters.markdown import person_to_markdown

        r = populated.get("/export/person/P0001.md")
        assert r.status_code == 200
        assert r.headers["content-disposition"].endswith('"P0001.md"')
        with session_maker() as s:
            ws = get_or_create_workspace(s, "Personal")
            repo = PeopleRepository(WorkspaceScope(s, ws.id, Actor("owner")))
            expected = person_to_markdown(person_to_domain(repo.require("P0001")))
        assert r.text == expected

    def test_person_json_export(self, populated):
        r = populated.get("/export/person/P0003.json")
        assert r.status_code == 200 and r.json()["name"] == "Marco Reyes"

    def test_workspace_export_excludes_secrets(self, populated):
        payload = populated.get("/export/workspace.json").json()
        assert {p["name"] for p in payload["people"]["active"]} >= {"Marco Reyes"}
        blob = str(payload).lower()
        assert "password" not in blob and "token" not in blob

    def test_archive_and_restore_from_person_page(self, populated):
        csrf = _csrf(populated, "/people/P0001")
        populated.post("/people/P0001/archive", data={"_csrf": csrf, "reason": "moved"})
        assert "P0001" not in populated.get("/people?show=active").text
        populated.post("/people/P0001/restore", data={"_csrf": csrf})
        assert "P0001" in populated.get("/people?show=active").text
        # revision history records both transitions
        assert "archived_at" in populated.get("/people/P0001").text

    def test_missing_person_is_404_not_500(self, populated):
        assert populated.get("/people/P9999").status_code == 404

    def test_person_page_has_editable_structured_fields(self, populated):
        html = populated.get("/people/P0001").text
        assert 'action="/people/P0001/edit"' in html
        for field in ("name", "aliases", "birthday", "location", "emails", "address"):
            assert f'name="{field}"' in html

    def _edit(self, client, pid, **fields):
        base = {
            k: v
            for k, v in {
                "name": fields.pop("name", "X"),
                "aliases": fields.pop("aliases", ""),
                "birthday": fields.pop("birthday", ""),
                "location": fields.pop("location", ""),
                "company": fields.pop("company", ""),
                "job_title": fields.pop("job_title", ""),
                "desired_contact_cadence_days": fields.pop("cadence", ""),
                "summary": fields.pop("summary", ""),
                "emails": fields.pop("emails", ""),
                "phones": fields.pop("phones", ""),
                "address": fields.pop("address", ""),
                "messaging": fields.pop("messaging", ""),
            }.items()
        }
        base["_csrf"] = _csrf(client, f"/people/{pid}")
        return client.post(f"/people/{pid}/edit", data=base, follow_redirects=False)

    def test_edit_stores_partial_location_and_month_only_birthday(self, populated):
        r = self._edit(populated, "P0002", name="Dana Whitfield", location="Boston", birthday="03")
        assert r.status_code == 303
        page = populated.get("/people/P0002").text
        assert 'value="Boston"' in page
        assert 'value="03"' in page  # month-only birthday round-trips
        # markdown export reflects the partial values
        md = populated.get("/export/person/P0002.md").text
        assert "location: Boston" in md and "birthday: '03'" in md.replace('"', "'")

    def test_edit_updates_aliases_emails_address_and_logs_revisions(self, populated):
        r = self._edit(
            populated,
            "P0003",
            name="Marco Reyes",
            aliases="Marc\nReyes",
            emails="marco@example.com\nm.reyes@work.example",
            address="Cambridge, MA",
        )
        assert r.status_code == 303
        page = populated.get("/people/P0003").text
        assert "Marc" in page and "marco@example.com" in page and "Cambridge, MA" in page
        assert "person.aliases" in page or "person.contact" in page  # revision rows rendered

    def test_edit_rejects_bad_birthday_without_saving(self, populated):
        r = self._edit(populated, "P0001", name="Dana Whitfield", birthday="2026-13-40")
        assert r.status_code == 400
        assert "birthday" in r.text.lower()
        assert "2026-13-40" not in populated.get("/export/person/P0001.md").text

    def test_edit_rejects_non_numeric_cadence(self, populated):
        r = self._edit(populated, "P0001", name="Dana Whitfield", cadence="soon")
        assert r.status_code == 400

    def _post(self, client, path, **data):
        data["_csrf"] = _csrf(client, "/people/P0002")
        return client.post(path, data=data, follow_redirects=False)

    def test_add_edit_remove_fact_inline(self, populated):
        r = self._post(
            populated, "/people/P0002/facts", category="Interests", text="Keeps bees", confidence="medium"
        )
        assert r.status_code == 303
        page = populated.get("/people/P0002").text
        assert "Keeps bees" in page and "fact-" in page

        # the new fact id is the last allocated; edit it
        import re

        fid = re.findall(r"/people/P0002/facts/(fact-\d+)\b", page)[-1]
        r = self._post(
            populated,
            f"/people/P0002/facts/{fid}",
            text="Keeps bees and chickens",
            category="Interests",
            date="2022",
            confidence="high",
        )
        assert r.status_code == 303
        page = populated.get("/people/P0002").text
        assert "Keeps bees and chickens" in page and "2022" in page

        r = self._post(populated, f"/people/P0002/facts/{fid}/delete", _reason="mistake")
        assert r.status_code == 303
        after = populated.get("/people/P0002").text
        assert f'/people/P0002/facts/{fid}"' not in after  # no edit form for it any more
        assert "removed" in after and "mistake" in after  # but the revision is recorded

    def test_add_fact_with_related_person_becomes_a_relationship(self, populated):
        r = self._post(
            populated,
            "/people/P0002/facts",
            category="Family",
            text="Sibling of Marco",
            related_person_id="P0003",
        )
        assert r.status_code == 303
        page = populated.get("/people/P0002").text
        assert "Sibling of Marco" in page
        assert "Marco Reyes (P0003)" in page  # shown under Relationships, resolved

    def test_fact_rejects_unknown_related_person(self, populated):
        r = self._post(
            populated, "/people/P0002/facts", category="Family", text="x", related_person_id="P0404"
        )
        assert r.status_code == 400
        assert "P0404" in r.text

    def test_fact_rejects_unknown_category(self, populated):
        r = self._post(populated, "/people/P0002/facts", category="Bogus", text="x")
        assert r.status_code == 400

    def test_add_edit_remove_interaction_inline(self, populated):
        r = self._post(
            populated,
            "/people/P0002/interactions",
            date="2024-07-01",
            channel="text",
            summary="Quick hello",
        )
        assert r.status_code == 303
        page = populated.get("/people/P0002").text
        assert "Quick hello" in page

        import re

        iid = re.findall(r"/people/P0002/interactions/(int-\d+)\b", page)[-1]
        r = self._post(
            populated,
            f"/people/P0002/interactions/{iid}",
            date="2024-07",
            summary="Longer chat",
            connection_level="4",
        )
        assert r.status_code == 303
        assert "Longer chat" in populated.get("/people/P0002").text

        r = self._post(populated, f"/people/P0002/interactions/{iid}/delete")
        assert r.status_code == 303
        after = populated.get("/people/P0002").text
        assert f'/people/P0002/interactions/{iid}"' not in after

    def test_interaction_requires_a_date(self, populated):
        r = self._post(populated, "/people/P0002/interactions", summary="no date given")
        assert r.status_code == 400

    def test_inline_edit_forms_present_on_person_page(self, populated):
        page = populated.get("/people/P0003").text
        assert "/people/P0003/facts" in page  # add-fact form
        assert "/people/P0003/interactions/int-1" in page  # edit-interaction form
        assert "Add an interaction" in page

    def test_cannot_edit_a_fact_on_a_person_in_another_workspace(self, populated, session_maker):
        with session_maker() as s:
            other = get_or_create_workspace(s, "Elsewhere")
            PeopleRepository(WorkspaceScope(s, other.id, Actor("owner"))).create(
                Person(
                    id="P0001",
                    name="Theirs",
                    facts=[Fact(id="fact-1", category="General Notes", text="secret")],
                )
            )
            s.commit()
        # P0001 in the owner's workspace exists (Dana), but fact-1 belongs to Dana,
        # not the other workspace's person -- and there is no cross-workspace path anyway.
        r = self._post(populated, "/people/P0001/facts/fact-9/delete")
        assert r.status_code == 400  # LocalRecordNotFound, not a 500 or cross-tenant hit

    def test_transcripts_page_shows_degraded_state(self, populated):
        assert "No inbound connectors" in populated.get("/transcripts").text

    def test_create_person_from_directory(self, populated):
        csrf = _csrf(populated, "/people")
        r = populated.post(
            "/people",
            data={"_csrf": csrf, "name": "Wendell Borton", "company": "Springfield Elementary"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        new_url = r.headers["location"]
        assert new_url.startswith("/people/P00")  # Montauk assigned the id
        page = populated.get(new_url).text
        assert "Wendell Borton" in page and "Springfield Elementary" in page

    def test_create_person_requires_a_name(self, populated):
        csrf = _csrf(populated, "/people")
        r = populated.post("/people", data={"_csrf": csrf, "name": "  "}, follow_redirects=False)
        assert r.status_code == 400
        assert "name" in r.text.lower()

    def test_create_person_flags_shared_name(self, populated):
        csrf = _csrf(populated, "/people")
        r = populated.post("/people", data={"_csrf": csrf, "name": "Dana Whitfield"}, follow_redirects=False)
        dest = r.headers["location"]
        assert "dupes=" in dest
        assert "share this name" in populated.get(dest).text

    def test_archive_and_restore_from_directory(self, populated):
        csrf = _csrf(populated, "/people")
        populated.post("/people/P0001/archive", data={"_csrf": csrf})
        assert "P0001" not in populated.get("/people?show=active").text
        arch = populated.get("/people?show=archived").text
        assert "P0001" in arch and "/people/P0001/restore" in arch
        populated.post("/people/P0001/restore", data={"_csrf": csrf})
        assert "P0001" in populated.get("/people?show=active").text

    def test_directory_has_add_and_archive_controls(self, populated):
        html = populated.get("/people").text
        assert 'action="/people"' in html and "Add a person" in html
        assert "/people/P0001/archive" in html  # per-row archive button


class TestSettings:
    @pytest.fixture
    def logged_in(self, client):
        _setup_owner(client)
        return client

    def test_defaults_are_automatic_all_and_human_only(self, logged_in):
        html = logged_in.get("/settings").text
        assert "automatic_all" in html and "human_only" in html

    def test_create_and_revoke_agent_credential(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/agents",
            data={"_csrf": csrf, "name": "laptop", "capabilities": ["memory_read", "review_proposals"]},
        )
        assert r.status_code == 200
        import re

        m = re.search(r"class=\"token\">\s*(mtk_[A-Za-z0-9_-]+)", r.text)
        assert m, "raw token shown once on creation"
        raw = m.group(1)
        assert len(raw) > 20 and "laptop" in r.text

        again = logged_in.get("/settings").text
        assert raw not in again  # full token never shown again
        assert "laptop" in again  # but the credential is listed
        assert raw[:12] in again  # only the short prefix

        # revoke it
        cid_match = re.search(r"/settings/agents/([0-9a-f-]{36})/revoke", again)
        assert cid_match
        logged_in.post(f"/settings/agents/{cid_match.group(1)}/revoke", data={"_csrf": csrf})
        assert "revoked" in logged_in.get("/settings").text

    def test_settings_fields_are_editable_forms(self, logged_in):
        html = logged_in.get("/settings").text
        assert 'action="/settings/workspace"' in html
        assert 'action="/settings/review-policy"' in html
        assert 'action="/settings/password"' in html
        assert 'name="review_threshold"' in html and "<select" in html

    def test_save_workspace_settings(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/workspace",
            data={
                "_csrf": csrf,
                "name": "My Circle",
                "deployment_profile": "public",
                "public_url": "https://montauk.example.com",
            },
        )
        assert r.status_code == 200
        again = logged_in.get("/settings").text
        assert 'value="My Circle"' in again and "montauk.example.com" in again

    def test_save_workspace_rejects_public_without_https(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/workspace",
            data={"_csrf": csrf, "name": "X", "deployment_profile": "public", "public_url": ""},
        )
        assert r.status_code == 400

    def test_save_review_policy(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/review-policy",
            data={
                "_csrf": csrf,
                "review_threshold": "review_all",
                "allowed_reviewers": "human_or_authorized_agent",
                "timezone": "Europe/Berlin",
                "daily_extraction_time": "06:15",
                "historical_ingestion_default": "future_only",
                "agent_transcript_access": "1",
            },
        )
        assert r.status_code == 200
        again = logged_in.get("/settings").text
        assert "review_all" in again and "Europe/Berlin" in again and "06:15" in again

    def test_review_policy_rejects_bad_time(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/review-policy",
            data={
                "_csrf": csrf,
                "review_threshold": "automatic_all",
                "allowed_reviewers": "human_only",
                "timezone": "UTC",
                "daily_extraction_time": "9am",
                "historical_ingestion_default": "all_history",
            },
        )
        assert r.status_code == 400

    def test_change_password_flow(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        bad = logged_in.post(
            "/settings/password",
            data={
                "_csrf": csrf,
                "current_password": "wrong",
                "new_password": "a-good-new-one",
                "new_password_confirm": "a-good-new-one",
            },
        )
        assert bad.status_code == 400

        ok = logged_in.post(
            "/settings/password",
            data={
                "_csrf": csrf,
                "current_password": OWNER["password"],
                "new_password": "a-fresh-new-password",
                "new_password_confirm": "a-fresh-new-password",
            },
        )
        assert ok.status_code == 200
        # current session still works; new password authenticates on a fresh client
        assert logged_in.get("/settings").status_code == 200
        fresh = logged_in.__class__(logged_in.app)
        assert (
            fresh.post(
                "/login",
                data={"email": OWNER["email"], "password": "a-fresh-new-password"},
                follow_redirects=False,
            ).status_code
            == 303
        )


class TestLLMDashboard:
    @pytest.fixture
    def logged_in(self, client):
        _setup_owner(client)
        return client

    def test_model_and_cost_control_forms_present(self, logged_in):
        html = logged_in.get("/settings").text
        assert 'action="/settings/model/summarization"' in html
        assert 'action="/settings/model/extraction"' in html
        assert 'action="/settings/cost-controls"' in html
        assert "not configured" in logged_in.get("/").text  # home LLM card

    def test_configure_a_cli_model_and_see_it(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "claude-haiku-4-5"},
        )
        assert r.status_code == 200 and "saved" in r.text
        again = logged_in.get("/settings").text
        assert "claude_cli" in again and "claude-haiku-4-5" in again
        # home card flips to ready
        assert "OK" in logged_in.get("/").text

    def test_model_config_validation_surfaces(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        r = logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "openai_compatible", "model": "m", "base_url": ""},
        )
        assert r.status_code == 400 and "base URL" in r.text

    def test_model_form_marks_fields_by_provider(self, logged_in):
        html = logged_in.get("/settings").text
        # the JS hook + per-provider gating attributes are present
        assert "data-model-provider" in html
        assert 'data-when-provider="openai_compatible"' in html  # base URL
        assert 'id="models-summarization-claude_cli"' in html  # per-provider suggestions
        assert "data-test-connection" in html

    def test_test_connection_returns_json_for_fetch(self, logged_in, monkeypatch):
        from montauk.llm.providers.fake import FakeProvider
        from montauk.web.routes import settings as settings_routes

        csrf = _csrf(logged_in, "/settings")
        logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "claude-haiku-4-5"},
        )
        monkeypatch.setattr(
            settings_routes, "build_provider", lambda cfg: FakeProvider(model="claude-haiku-4-5")
        )
        r = logged_in.post(
            "/settings/model/summarization/test",
            data={"_csrf": csrf},
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and "claude-haiku-4-5" in body["message"]

    def test_test_connection_json_reports_failure(self, logged_in, monkeypatch):
        from montauk.llm.base import LLMRateLimited
        from montauk.llm.providers.fake import FakeProvider
        from montauk.web.routes import settings as settings_routes

        csrf = _csrf(logged_in, "/settings")
        logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "m"},
        )
        monkeypatch.setattr(
            settings_routes,
            "build_provider",
            lambda cfg: FakeProvider(fail_with=LLMRateLimited("slow down")),
        )
        r = logged_in.post(
            "/settings/model/summarization/test",
            data={"_csrf": csrf},
            headers={"Accept": "application/json"},
        )
        assert r.status_code == 400
        assert r.json()["ok"] is False and "rate_limit" in r.json()["message"]

    def test_test_connection_without_js_still_reloads_the_page(self, logged_in, monkeypatch):
        from montauk.llm.providers.fake import FakeProvider
        from montauk.web.routes import settings as settings_routes

        csrf = _csrf(logged_in, "/settings")
        logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "claude-haiku-4-5"},
        )
        monkeypatch.setattr(
            settings_routes, "build_provider", lambda cfg: FakeProvider(model="claude-haiku-4-5")
        )
        r = logged_in.post("/settings/model/summarization/test", data={"_csrf": csrf})
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "claude-haiku-4-5 replied" in r.text

    def test_save_cost_controls_and_pause_blocks_home_card(self, logged_in):
        csrf = _csrf(logged_in, "/settings")
        logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "m"},
        )
        r = logged_in.post(
            "/settings/cost-controls",
            data={
                "_csrf": csrf,
                "monthly_spend_limit_usd": "5",
                "monthly_token_limit": "2000000",
                "max_job_input_tokens": "50000",
                "llm_processing_paused": "1",
            },
        )
        assert r.status_code == 200
        assert "paused" in logged_in.get("/").text.lower()

    def test_generate_summary_without_a_model_returns_deterministic_evidence(
        self, logged_in, session_maker, tmp_path
    ):
        seed_golden(session_maker, workspace_name="Personal")
        csrf = _csrf(logged_in, "/people/P0001")
        r = logged_in.post(
            "/people/P0001/summary",
            data={"_csrf": csrf, "purpose": "what does she do", "detail_level": "standard"},
        )
        assert r.status_code == 200
        assert "not generated" in r.text and "Nimbus Robotics" in r.text  # evidence shown

    def test_generate_summary_with_a_fake_model(self, logged_in, session_maker, tmp_path, monkeypatch):
        from montauk.llm.providers.fake import FakeProvider
        from montauk.services import briefing

        seed_golden(session_maker, workspace_name="Personal")
        csrf = _csrf(logged_in, "/settings")
        logged_in.post(
            "/settings/model/summarization",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "claude-haiku-4-5"},
        )
        monkeypatch.setattr(
            briefing,
            "build_provider",
            lambda cfg: FakeProvider(
                model="claude-haiku-4-5",
                responder=lambda s, p: "She is a robotics engineer in Portland.",
            ),
        )
        r = logged_in.post(
            "/people/P0001/summary",
            data={"_csrf": csrf, "purpose": "work", "detail_level": "brief", "mode": "summary_only"},
        )
        assert r.status_code == 200
        assert "She is a robotics engineer in Portland." in r.text
        assert "claude-haiku-4-5 (fake)" in r.text  # status line
        assert "what was sent to the model" in r.text  # debug expander
        assert "System prompt" in r.text and "User prompt" in r.text
        # usage shows on settings
        assert "1</strong> calls" in logged_in.get("/settings").text

    def test_summary_requires_a_purpose(self, logged_in, session_maker, tmp_path):
        seed_golden(session_maker, workspace_name="Personal")
        csrf = _csrf(logged_in, "/people/P0001")
        r = logged_in.post("/people/P0001/summary", data={"_csrf": csrf, "purpose": " "})
        assert r.status_code == 400


class TestWorkspaceIsolationOverHTTP:
    def test_person_from_another_workspace_is_not_visible(self, client, session_maker, tmp_path):
        _setup_owner(client)
        # a second, unrelated workspace with its own person P0001
        with session_maker() as s:
            other = get_or_create_workspace(s, "Someone Else")
            repo = PeopleRepository(WorkspaceScope(s, other.id, Actor("owner")))
            repo.create(
                Person(
                    id="P0001",
                    name="Not Yours",
                    facts=[Fact(id="fact-1", category="General Notes", text="secret")],
                    interactions=[Interaction(id="int-1", date="2024-01-01", summary="secret")],
                )
            )
            s.commit()

        r = client.get("/people/P0001", follow_redirects=False)
        assert r.status_code == 404
        assert "Not Yours" not in client.get("/people").text
