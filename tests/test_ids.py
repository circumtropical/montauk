import pytest

from montauk.ids import next_fact_id, next_interaction_id, next_person_id, slugify


class TestSlugify:
    def test_simple_name(self):
        assert slugify("Mike Chen") == "mike-chen"

    def test_extra_whitespace_and_punctuation(self):
        assert slugify("  Mike  O'Chen! ") == "mike-o-chen"

    def test_already_slug_like(self):
        assert slugify("homer-simpson") == "homer-simpson"

    def test_rejects_empty_result(self):
        with pytest.raises(ValueError):
            slugify("!!!")


class TestNextPersonId:
    def test_first_person_gets_bare_slug(self):
        assert next_person_id("Mike Chen", existing_ids=[]) == "mike-chen"

    def test_collision_gets_suffix_2(self):
        assert next_person_id("Mike Chen", existing_ids=["mike-chen"]) == "mike-chen-2"

    def test_third_collision_gets_suffix_3(self):
        existing = ["mike-chen", "mike-chen-2"]
        assert next_person_id("Mike Chen", existing_ids=existing) == "mike-chen-3"

    def test_lowest_available_suffix_is_reused_after_gap(self):
        # mike-chen-2 was archived/freed elsewhere but mike-chen and
        # mike-chen-3 still exist -> lowest available suffix is 2.
        existing = ["mike-chen", "mike-chen-3"]
        assert next_person_id("Mike Chen", existing_ids=existing) == "mike-chen-2"

    def test_unrelated_existing_ids_dont_collide(self):
        assert next_person_id("Sarah Jones", existing_ids=["mike-chen"]) == "sarah-jones"


class TestNextFactId:
    def test_first_fact(self):
        assert next_fact_id([]) == "fact-1"

    def test_increments_from_max(self):
        assert next_fact_id(["fact-1", "fact-2"]) == "fact-3"

    def test_does_not_reuse_gap_after_removal(self):
        # fact-2 was removed; next id still continues from the max seen,
        # never reissuing fact-2.
        assert next_fact_id(["fact-1", "fact-3"]) == "fact-4"

    def test_ignores_unrelated_ids(self):
        assert next_fact_id(["int-5"]) == "fact-1"


class TestNextInteractionId:
    def test_first_interaction(self):
        assert next_interaction_id([]) == "int-1"

    def test_increments_from_max(self):
        assert next_interaction_id(["int-1", "int-2", "int-4"]) == "int-5"
