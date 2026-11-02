"""Security checks for the sandbox's fixed historical-data source URL."""

from urllib.request import Request

import pytest

from critical_consensus.betting_sandbox.data import (
    BASE_URL,
    DatasetError,
    _NoRedirectHandler,
    _validate_source_url,
)


def test_official_historical_csv_url_is_canonical_https():
    url = f"{BASE_URL}/2425/E0.csv"
    assert _validate_source_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://www.football-data.co.uk/mmz4281/2425/E0.csv",
        "https://attacker.example/mmz4281/2425/E0.csv",
        "https://www.football-data.co.uk.attacker.example/mmz4281/2425/E0.csv",
        "https://user@www.football-data.co.uk/mmz4281/2425/E0.csv",
        "https://www.football-data.co.uk:444/mmz4281/2425/E0.csv",
        "https://www.football-data.co.uk/mmz4281/../2425/E0.csv",
        "https://www.football-data.co.uk/mmz4281/2425/E0.csv?redirect=1",
        "https://www.football-data.co.uk/mmz4281/2425/E0.csv#fragment",
    ],
)
def test_noncanonical_source_urls_are_rejected(url):
    with pytest.raises(DatasetError, match="non-canonical"):
        _validate_source_url(url)


def test_source_opener_refuses_all_redirects():
    request = Request(f"{BASE_URL}/2425/E0.csv")
    handler = _NoRedirectHandler()
    assert handler.redirect_request(
        request, None, 302, "Found", {}, "https://attacker.example/collect"
    ) is None
    assert handler.redirect_request(
        request, None, 301, "Moved", {}, "http://www.football-data.co.uk/collect"
    ) is None
