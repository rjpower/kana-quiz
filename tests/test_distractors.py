"""Adaptive distractor selection: phonetic bias grows with target maturity."""

import random
import sqlite3

import pytest

from kana_quiz.grading import gloss_core, normalize_english
from kana_quiz.models import word_from_row
from kana_quiz.session import build_choices


@pytest.fixture
def deck(tmp_path, monkeypatch):
    """Deck with deliberate phonetic structure so we can assert distractor mix.

    Five words share the い prefix with the target (いぬ), and five more are
    tag-mates without the prefix. A mature target should pull from the い set;
    a learning target should stay on the semantic tag set.
    """
    from kana_quiz.db import connect, init_schema

    path = tmp_path / "db.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    conn = connect()
    deck_id = conn.execute(
        "SELECT id FROM decks WHERE name = 'Default'"
    ).fetchone()["id"]
    rows = [
        # target
        ("いぬ", "dog", "animal"),
        # same-first-kana distractor candidates
        ("いち", "one", "number"),
        ("いえ", "house", "place"),
        ("いし", "stone", "nature"),
        ("いろ", "color", "abstract"),
        ("いす", "chair", "furniture"),
        # semantic (tag-mate) distractor candidates, no い prefix
        ("ねこ", "cat", "animal"),
        ("うま", "horse", "animal"),
        ("とり", "bird", "animal"),
        ("さかな", "fish", "animal"),
        ("くま", "bear", "animal"),
    ]
    for kana, english, tag in rows:
        conn.execute(
            """
            INSERT INTO words (kana, english, tags, deck_id)
            VALUES (?, ?, ?, ?)
            """,
            (kana, english, tag, deck_id),
        )
    yield conn
    conn.close()


def _target_row(conn: sqlite3.Connection, kana: str):
    return conn.execute("SELECT * FROM words WHERE kana = ?", (kana,)).fetchone()


def test_en2ja_learning_target_gets_two_phonetic_distractors(deck):
    target = word_from_row(_target_row(deck, "いぬ"))

    # Seed with a fixed RNG so the assertion is stable across runs.
    choices = build_choices(
        deck, target, direction="en2ja",
        target_interval_days=0.0, rng=random.Random(0),
    )
    distractors = [c for c in choices if c.id != target.id]
    assert len(distractors) == 3
    # en2ja shows kana as choices, so phonetic confusion is the point — we
    # push 2 phonetic distractors even at zero interval to keep the kana
    # actively involved instead of letting the user solve by elimination.
    phonetic = [d for d in distractors if d.kana.startswith("い")]
    assert len(phonetic) == 2


def test_en2ja_mature_target_is_fully_phonetic(deck):
    target = word_from_row(_target_row(deck, "いぬ"))

    choices = build_choices(
        deck, target, direction="en2ja",
        target_interval_days=30.0, rng=random.Random(0),
    )
    distractors = [c for c in choices if c.id != target.id]
    assert len(distractors) == 3
    # Mature en2ja quota is 3 phonetic + 0 tag.
    phonetic = [d for d in distractors if d.kana.startswith("い")]
    assert len(phonetic) == 3


def test_ja2en_target_stays_semantic_regardless_of_maturity(deck):
    """Reading direction reverses the calculus: choices are English glosses,
    so kana similarity is invisible to the picker. Stay tag-heavy so the
    user is forced to reason about meaning, not just exclude unfamiliar
    characters."""
    target = word_from_row(_target_row(deck, "いぬ"))

    choices = build_choices(
        deck, target, direction="ja2en",
        target_interval_days=30.0, rng=random.Random(0),
    )
    distractors = [c for c in choices if c.id != target.id]
    assert len(distractors) == 3
    assert all("animal" in d.tags for d in distractors)
    assert all(not d.kana.startswith("い") for d in distractors)


@pytest.fixture
def dup_gloss_deck(tmp_path, monkeypatch):
    """Deck with several words that share the gloss 'only', plus distinct fillers.

    Mirrors the real deck where ただ / だけ / のみ / しか all reduce to "only" —
    without label-dedup a ja2en question can seat two identical "only" options.
    """
    from kana_quiz.db import connect, init_schema

    path = tmp_path / "db.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    conn = connect()
    deck_id = conn.execute(
        "SELECT id FROM decks WHERE name = 'Default'"
    ).fetchone()["id"]
    rows = [
        ("ただ", "only", "adv"),
        ("だけ", "only", "adv"),
        ("のみ", "only", "adv"),
        ("しか", "only.", "adv"),   # trailing punctuation -> normalizes to "only"
        ("たった", "Only", "adv"),  # case -> normalizes to "only"
        # distinct-gloss fillers so the picker CAN still reach four options
        ("いぬ", "dog", "adv"),
        ("ねこ", "cat", "adv"),
        ("やま", "mountain", "adv"),
        ("うみ", "sea", "adv"),
    ]
    for kana, english, tag in rows:
        conn.execute(
            "INSERT INTO words (kana, english, tags, deck_id) VALUES (?, ?, ?, ?)",
            (kana, english, tag, deck_id),
        )
    yield conn
    conn.close()


def test_ja2en_choices_never_repeat_a_gloss(dup_gloss_deck):
    """No two options render the same English gloss (the "two 'only's" bug)."""
    conn = dup_gloss_deck
    target = word_from_row(_target_row(conn, "ただ"))
    # Distractor selection is randomized; a single seed could dodge the
    # collision by luck, so the invariant must hold across many seeds.
    for seed in range(50):
        choices = build_choices(
            conn, target, direction="ja2en",
            target_interval_days=0.0, rng=random.Random(seed),
        )
        labels = [normalize_english(c.english) for c in choices]
        assert len(labels) == len(set(labels)), f"duplicate label at seed {seed}: {labels}"
        # The correct gloss appears exactly once, and the deck is rich enough
        # in distinct glosses to still fill all four slots.
        assert labels.count("only") == 1
        assert len(choices) == 4


@pytest.fixture
def mixed_pos_deck(tmp_path, monkeypatch):
    """Half verbs ("to X" glosses), half nouns, so POS filtering is observable."""
    from kana_quiz.db import connect, init_schema

    path = tmp_path / "db.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    conn = connect()
    deck_id = conn.execute(
        "SELECT id FROM decks WHERE name = 'Default'"
    ).fetchone()["id"]
    rows = [
        # verb target + verb peers
        ("たべる", "to eat", "verb"),
        ("のむ", "to drink", "verb"),
        ("みる", "to see", "verb"),
        ("いく", "to go", "verb"),
        ("くる", "to come", "verb"),
        ("はなす", "to speak", "verb"),
        # nouns — should not show up as distractors for a verb target
        ("いぬ", "dog", "animal"),
        ("ねこ", "cat", "animal"),
        ("うま", "horse", "animal"),
        ("とり", "bird", "animal"),
    ]
    for kana, english, tag in rows:
        conn.execute(
            """
            INSERT INTO words (kana, english, tags, deck_id)
            VALUES (?, ?, ?, ?)
            """,
            (kana, english, tag, deck_id),
        )
    yield conn
    conn.close()


@pytest.fixture
def disambiguated_gloss_deck(tmp_path, monkeypatch):
    """Deck where several glosses share a core and differ only in the hint.

    The rigorized deck splits 順/注文/命令 into "order (…)" variants. Seating
    two of them on one ja2en card would be a coin flip, so dedup has to work
    on the core, not the full displayed string.
    """
    from kana_quiz.db import connect, init_schema

    path = tmp_path / "db.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    conn = connect()
    deck_id = conn.execute(
        "SELECT id FROM decks WHERE name = 'Default'"
    ).fetchone()["id"]
    rows = [
        ("じゅん", "order (relative sequence or ranking)", "noun"),
        ("ちゅうもん", "order (placing an order for goods)", "noun"),
        ("めいれい", "order (a command)", "noun"),
        ("じゅんじょ", "order (sequence)", "noun"),
        ("いぬ", "dog", "noun"),
        ("ねこ", "cat", "noun"),
        ("やま", "mountain", "noun"),
        ("うみ", "sea", "noun"),
    ]
    for kana, english, tag in rows:
        conn.execute(
            "INSERT INTO words (kana, english, tags, deck_id) VALUES (?, ?, ?, ?)",
            (kana, english, tag, deck_id),
        )
    yield conn
    conn.close()


def test_ja2en_choices_never_repeat_a_gloss_core(disambiguated_gloss_deck):
    """Two "order (…)" options on one card is a coin flip, not discrimination."""
    conn = disambiguated_gloss_deck
    target = word_from_row(_target_row(conn, "めいれい"))
    for seed in range(50):
        choices = build_choices(
            conn, target, direction="ja2en",
            target_interval_days=0.0, rng=random.Random(seed),
        )
        cores = [gloss_core(c.english) for c in choices]
        assert len(cores) == len(set(cores)), f"duplicate core at seed {seed}: {cores}"


def test_verb_target_gets_only_verb_distractors(mixed_pos_deck):
    target = word_from_row(_target_row(mixed_pos_deck, "たべる"))
    # Run several RNG seeds — POS filtering must hold regardless of shuffling.
    for seed in range(5):
        choices = build_choices(
            mixed_pos_deck, target, direction="en2ja", rng=random.Random(seed)
        )
        distractors = [c for c in choices if c.id != target.id]
        assert len(distractors) == 3
        assert all(d.english.lower().startswith("to ") for d in distractors), (
            f"non-verb distractor leaked for seed={seed}: "
            f"{[d.english for d in distractors]}"
        )


def test_noun_target_avoids_verb_distractors(mixed_pos_deck):
    target = word_from_row(_target_row(mixed_pos_deck, "いぬ"))
    for seed in range(5):
        choices = build_choices(
            mixed_pos_deck, target, direction="en2ja", rng=random.Random(seed)
        )
        distractors = [c for c in choices if c.id != target.id]
        assert len(distractors) == 3
        assert all(not d.english.lower().startswith("to ") for d in distractors), (
            f"verb distractor leaked for noun target, seed={seed}: "
            f"{[d.english for d in distractors]}"
        )


def test_verb_filter_falls_back_when_pool_starved(tmp_path, monkeypatch):
    # Only one verb in the deck — we must still hand back 4 distinct choices
    # even though that forces a cross-POS backfill.
    from kana_quiz.db import connect, init_schema

    path = tmp_path / "db.sqlite"
    monkeypatch.setenv("KANA_QUIZ_DB", str(path))
    init_schema()
    conn = connect()
    deck_id = conn.execute(
        "SELECT id FROM decks WHERE name = 'Default'"
    ).fetchone()["id"]
    for kana, english, tag in [
        ("たべる", "to eat", "verb"),
        ("いぬ", "dog", "animal"),
        ("ねこ", "cat", "animal"),
        ("うま", "horse", "animal"),
        ("とり", "bird", "animal"),
    ]:
        conn.execute(
            "INSERT INTO words (kana, english, tags, deck_id) VALUES (?, ?, ?, ?)",
            (kana, english, tag, deck_id),
        )
    target = word_from_row(_target_row(conn, "たべる"))
    choices = build_choices(conn, target, direction="en2ja", rng=random.Random(0))
    assert len({c.id for c in choices}) == 4
    conn.close()


def test_phonetic_shortfall_falls_back_to_tag_or_random(deck):
    # Drop the phonetic pool: remove every い-prefix distractor. Mature target
    # should still get 3 distractors, just all semantic.
    deck.execute("DELETE FROM words WHERE kana LIKE 'い%' AND kana != 'いぬ'")
    target = word_from_row(_target_row(deck, "いぬ"))

    choices = build_choices(
        deck, target, direction="en2ja",
        target_interval_days=30.0, rng=random.Random(0),
    )
    distractors = [c for c in choices if c.id != target.id]
    assert len(distractors) == 3
    assert len({d.id for d in distractors}) == 3  # no duplicates


def test_prefer_ids_seats_session_words_as_distractors(deck):
    # Burndown confusers: the round's other misses (same POS) should be seated
    # as distractors ahead of the adaptive pools, up to prefer_take slots.
    target = word_from_row(_target_row(deck, "いぬ"))  # dog (noun)
    cat = _target_row(deck, "ねこ")["id"]
    horse = _target_row(deck, "うま")["id"]

    choices = build_choices(
        deck, target, direction="ja2en",
        target_interval_days=30.0,  # mature -> would normally lean phonetic
        prefer_ids={cat, horse},
        rng=random.Random(0),
    )
    ids = {c.id for c in choices}
    # Both preferred (same-POS) words land in the choice set despite maturity.
    assert cat in ids and horse in ids
    assert target.id in ids
    assert len(choices) == 4


def test_prefer_take_still_returns_three_distinct(deck):
    # A big prefer pool seats prefer_take up front and the adaptive pools finish
    # the set — the total is always 3 distinct distractors, never padded or
    # short. (The remaining slot may also land on a session word when the pools
    # overlap, e.g. same-tag animals here — that's fine; the cap only bounds the
    # dedicated prefer step, not incidental overlap.)
    target = word_from_row(_target_row(deck, "いぬ"))
    prefer = {
        _target_row(deck, k)["id"]
        for k in ("ねこ", "うま", "とり", "さかな", "くま")
    }
    choices = build_choices(
        deck, target, direction="ja2en",
        target_interval_days=0.0,
        prefer_ids=prefer, prefer_take=2,
        rng=random.Random(0),
    )
    distractors = [c for c in choices if c.id != target.id]
    assert len(distractors) == 3
    assert len({c.id for c in distractors}) == 3  # no duplicates
    from_pool = [c for c in distractors if c.id in prefer]
    assert len(from_pool) >= 2  # at least prefer_take seated from the session pool


def test_prefer_ids_empty_pool_is_a_noop(deck):
    # A 1-miss burndown (no other words to pull) must still return 4 choices.
    target = word_from_row(_target_row(deck, "いぬ"))
    choices = build_choices(
        deck, target, direction="en2ja",
        prefer_ids=set(), rng=random.Random(0),
    )
    assert len(choices) == 4
    assert len({c.id for c in choices}) == 4
