from montauk.tokens import estimate_tokens, split_sentences


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_never_below_word_count(self):
        text = "one two three four five six seven eight nine ten"
        assert estimate_tokens(text) >= 10

    def test_grows_with_length(self):
        assert estimate_tokens("a short phrase") < estimate_tokens("a considerably longer phrase " * 20)

    def test_conservative_for_dense_text(self):
        # long words / punctuation: char-based estimate should dominate
        text = "antidisestablishmentarianism," * 5
        assert estimate_tokens(text) >= len(text) / 4


class TestSplitSentences:
    def test_splits_on_terminal_punctuation(self):
        assert split_sentences("First one. Second one! Third?") == ["First one.", "Second one!", "Third?"]

    def test_splits_on_newlines(self):
        assert split_sentences("line one\nline two") == ["line one", "line two"]

    def test_normalizes_whitespace(self):
        assert split_sentences("  a   b .  c  d ") == ["a b .", "c d"]

    def test_single_sentence_stays_whole(self):
        assert split_sentences("just one clause here") == ["just one clause here"]
