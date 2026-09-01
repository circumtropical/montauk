import pytest

from montauk.ids import (
    PERSON_ID_RE,
    format_person_id,
    next_fact_id,
    next_interaction_id,
    normalize_alias,
    person_id_number,
    slugify,
)


class TestSlugify:
    def test_simple_name(self):
        assert slugify("Mike Chen") == "mike-chen"

    def test_extra_whitespace_and_punctuation(self):
        assert slugify("  Mike  O'Chen! ") == "mike-o-chen"

    def test_rejects_empty_result(self):
        with pytest.raises(ValueError):
            slugify("!!!")


class TestGenericPersonId:
    def test_format_zero_pads_to_four_digits(self):
        assert format_person_id(1) == "P0001"
        assert format_person_id(42) == "P0042"

    def test_format_grows_past_four_digits(self):
        assert format_person_id(10000) == "P10000"

    def test_format_rejects_non_positive(self):
        with pytest.raises(ValueError):
            format_person_id(0)

    def test_regex_accepts_canonical_ids(self):
        for pid in ("P0001", "P0042", "P9999", "P10000", "P123456"):
            assert PERSON_ID_RE.match(pid)

    def test_regex_rejects_name_derived_and_malformed_ids(self):
        for pid in ("mike-chen", "p0001", "P001", "P-0001", "P0001a", "0001", "PXXXX"):
            assert not PERSON_ID_RE.match(pid)

    def test_person_id_number_round_trips(self):
        assert person_id_number("P0042") == 42
        assert person_id_number("P10000") == 10000

    def test_person_id_number_none_for_non_canonical(self):
        assert person_id_number("mike-chen") is None
        assert person_id_number("p0001") is None


class TestNormalizeAlias:
    def test_folds_case_and_whitespace(self):
        assert normalize_alias("  Mike   Chen ") == "mike chen"
        assert normalize_alias("MIKE CHEN") == normalize_alias("mike chen")


class TestNextFactId:
    def test_first_fact(self):
        assert next_fact_id([]) == "fact-1"

    def test_increments_from_max(self):
        assert next_fact_id(["fact-1", "fact-2"]) == "fact-3"

    def test_does_not_reuse_gap_after_removal(self):
        assert next_fact_id(["fact-1", "fact-3"]) == "fact-4"

    def test_ignores_unrelated_ids(self):
        assert next_fact_id(["int-5"]) == "fact-1"


class TestNextInteractionId:
    def test_first_interaction(self):
        assert next_interaction_id([]) == "int-1"

    def test_increments_from_max(self):
        assert next_interaction_id(["int-1", "int-2", "int-4"]) == "int-5"
