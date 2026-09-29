"""This repository's corpus: a body of claims, what backs each, and what retired it."""

import lup.harness.models as models
from lup.harness.content.application import ApplicationLayout


def document(layout: ApplicationLayout) -> models.PromptDocument:
    """The corpus page, naming the modules it describes where the layout puts them."""
    corpus = models.code(layout.path("corpus.py"))
    return models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "corpus_py": corpus,
                    "kinds_py": models.code(layout.path("kinds.py")),
                    "corpus_py_2": corpus,
                    "writeups_py": models.code(layout.path("writeups.py")),
                },
            ),
        ],
    )
