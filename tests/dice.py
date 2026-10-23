"""A generator whose ``random()`` is fixed, for tests that force a dice roll."""

import random


class FixedDice(random.Random):
    def __init__(self, value: float) -> None:
        super().__init__(0)
        self._value = value

    def random(self) -> float:
        return self._value
