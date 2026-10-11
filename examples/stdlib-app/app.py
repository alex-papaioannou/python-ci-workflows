"""A tiny standard-library-only module."""


def slugify(text: str) -> str:
    """Lower-case text and join its words with hyphens.

    >>> slugify("Hello  World")
    'hello-world'
    """
    return "-".join(text.lower().split())
