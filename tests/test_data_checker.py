import pytest

from data_checker import DataChecker


class FakeProgressBar:
    def __init__(self, used=None, total=None):
        self.used = used
        self.total = total

    async def get_attribute(self, name):
        if name == "aria-valuenow":
            return self.used
        if name == "aria-valuemax":
            return self.total
        return None


class FakePage:
    def __init__(self, progressbar=None, texts=None, query_error=None, text_error=None):
        self.progressbar = progressbar
        self.texts = texts or {}
        self.query_error = query_error
        self.text_error = text_error

    async def query_selector(self, selector):
        if self.query_error:
            raise self.query_error
        return self.progressbar

    async def inner_text(self, selector):
        if self.text_error:
            raise self.text_error
        return self.texts[selector]


@pytest.mark.asyncio
async def test_get_usage_prefers_aria_values():
    page = FakePage(progressbar=FakeProgressBar("100", "200"))

    checker = DataChecker(page)
    used_kb, total_kb = await checker.get_usage()

    assert used_kb == 100
    assert total_kb == 200


@pytest.mark.asyncio
async def test_get_usage_falls_back_to_visible_text():
    page = FakePage(
        progressbar=FakeProgressBar(None, None),
        texts={
            ".font-weight-bold.pr-1": "98,30 GB",
            ".l-txt-small.pr-2": "von 100,00 GB",
        },
    )

    checker = DataChecker(page)
    used_kb, total_kb = await checker.get_usage()

    assert used_kb == int(98.30 * 1024 * 1024)
    assert total_kb == int(100.00 * 1024 * 1024)


@pytest.mark.asyncio
async def test_get_usage_returns_none_when_all_methods_fail():
    page = FakePage(query_error=RuntimeError("no progress"), text_error=RuntimeError("no text"))

    checker = DataChecker(page)

    assert await checker.get_usage() == (None, None)


def test_parse_german_gb_handles_expected_formats():
    assert DataChecker._parse_german_gb("98,30 GB") == 98.30
    assert DataChecker._parse_german_gb("von 100,00 GB") == 100.00
    assert DataChecker._parse_german_gb("not-a-size") is None