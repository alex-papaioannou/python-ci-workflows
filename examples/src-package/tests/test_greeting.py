import os

import pytest
from greeting import greet


def test_greet():
    assert greet("Ada") == "Hello, Ada!"


@pytest.mark.skipif("GITHUB_ACTIONS" not in os.environ, reason="set by examples.yml")
def test_variables_from_the_env_input():
    # examples.yml sets this with the workflow's env input.
    assert os.environ["GREETING_EXAMPLE"] == "line one\nline two"
