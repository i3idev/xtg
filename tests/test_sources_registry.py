from xtg.sources import get_source_docs
from xtg.sources.registry import SOURCE_DOCS


def test_source_docs_include_x_com() -> None:
    assert SOURCE_DOCS["x.com"] == "https://docs.x.com/x-api/getting-started/about-x-api"
    assert get_source_docs("x.com") == "https://docs.x.com/x-api/getting-started/about-x-api"
    assert get_source_docs("https://x.com/") == "https://docs.x.com/x-api/getting-started/about-x-api"
    assert get_source_docs("reddit") == "https://praw.readthedocs.io/en/stable/"
