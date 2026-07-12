"""Canonical task identifiers for unified SRS state rows."""

from typing import Literal, TypeGuard

CardTask = Literal["en2ja", "ja2en"]
Task = Literal["en2ja", "ja2en", "cloze", "cloze_choice", "sentence_listen"]

TASK_EN2JA: CardTask = "en2ja"
TASK_JA2EN: CardTask = "ja2en"
TASK_CLOZE: Task = "cloze"
# Selection-style cloze: pick the missing word from four kana choices. A
# recognition rung that sits below the type-in cloze (``cloze``) lane on the
# difficulty ladder — it unlocks from recall mastery, and mastering it is what
# unlocks the type-in lane. See session.py for the unlock wiring.
TASK_CLOZE_CHOICE: Task = "cloze_choice"
TASK_SENTENCE_LISTEN: Task = "sentence_listen"

CARD_TASKS: tuple[CardTask, ...] = (TASK_EN2JA, TASK_JA2EN)
TASKS: tuple[Task, ...] = (
    TASK_EN2JA,
    TASK_JA2EN,
    TASK_CLOZE,
    TASK_CLOZE_CHOICE,
    TASK_SENTENCE_LISTEN,
)


def sibling_task(task: str) -> CardTask | None:
    """Return the opposite recall direction, or ``None`` for non-recall tasks.

    ``en2ja`` ↔ ``ja2en``. Supplemental lanes (cloze / cloze_choice /
    sentence_listen) have no recall sibling and return ``None`` — callers use
    this to snooze the *other* direction of a word after a recall answer
    without touching supplemental state.
    """
    if task == TASK_EN2JA:
        return TASK_JA2EN
    if task == TASK_JA2EN:
        return TASK_EN2JA
    return None


def is_card_task(task: str) -> TypeGuard[CardTask]:
    """Return whether ``task`` is one of the bidirectional vocabulary tasks.

    Args:
        task: Task identifier to classify.

    Returns:
        True when ``task`` is ``"en2ja"`` or ``"ja2en"``.
    """
    return task in CARD_TASKS


def is_task(task: str) -> TypeGuard[Task]:
    """Return whether ``task`` is a known unified task identifier.

    Args:
        task: Task identifier to validate.

    Returns:
        True when ``task`` can be stored in ``task_state.task``.
    """
    return task in TASKS
