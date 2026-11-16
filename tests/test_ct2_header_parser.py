import pytest

from freshness.parser import (freshness_lifetime, lm_uninformative, parse_cache_control, parse_etag, parse_http_date)

CC_CASES = [
    ("max-age=60", {"max-age": 60}, []),
    ("MAX-AGE=60", {"max-age": 60}, []),
    ("Max-Age=60", {"max-age": 60}, []),
    ("  max-age = 60  ", {"max-age": 60}, []),
    ('max-age="60"', {"max-age": 60}, []),
    ("max-age=abc", {}, ["max-age"]),
    ("max-age=-5", {}, ["max-age"]),
    ("max-age=", {}, ["max-age"]),
    ("max-age", {}, ["max-age"]),
    ("max-age=1.5", {}, ["max-age"]),
    ("public, max-age=300", {"public": True, "max-age": 300}, []),
    ("max-age=300,s-maxage=60", {"max-age": 300, "s-maxage": 60}, []),
    ("no-cache", {"no-cache": True}, []),
    ("no-store", {"no-store": True}, []),
    ("private, no-cache, no-store, must-revalidate", {"private": True, "no-cache": True, "no-store": True, "must-revalidate": True}, []),
    ('no-cache="Set-Cookie, Foo"', {"no-cache": "Set-Cookie, Foo"}, []),
    ("max-age=60, max-age=600", {"max-age": 60}, []),
    ("immutable, max-age=31536000", {"immutable": True, "max-age": 31536000}, []),
    ("stale-while-revalidate=30, max-age=10", {"stale-while-revalidate": 30, "max-age": 10}, []),
    (",,max-age=5,,", {"max-age": 5}, []),
    ("", {}, []),
    (None, {}, []),
]


@pytest.mark.parametrize("value,directives,invalid", CC_CASES)
def test_cache_control(value, directives, invalid):
    r = parse_cache_control(value)
    assert r["directives"] == directives
    assert r["invalid"] == invalid


def test_cache_control_duplicates_reported():
    assert parse_cache_control("max-age=60, max-age=600")["duplicates"] == ["max-age"]


ETAG_CASES = [
    ('"abc"', "abc", False, True),
    ('W/"abc"', "abc", True, True),
    ('w/"abc"', "abc", True, False),      
    ('""', "", False, True),
    ("abc", "abc", False, False),          
    ('  "abc"  ', "abc", False, True),
    ('W/""', "", True, True),
    ('"5f3a-1b2c"', "5f3a-1b2c", False, True),
]


@pytest.mark.parametrize("value,opaque,weak,valid", ETAG_CASES)
def test_etag(value, opaque, weak, valid):
    r = parse_etag(value)
    assert (r["opaque"], r["weak"], r["valid"]) == (opaque, weak, valid)


def test_etag_missing():
    assert parse_etag(None)["weak"] is None


DATE = "Tue, 03 Nov 2026 16:00:00 GMT"
LIFETIME_CASES = [
    ({"cache-control": "max-age=600"}, 600, "max-age"),
    ({"cache-control": "s-maxage=60, max-age=600"}, 60, "s-maxage"),
    ({"cache-control": "max-age=600", "expires": "Tue, 03 Nov 2026 17:00:00 GMT", "date": DATE}, 600, "max-age"),
    ({"expires": "Tue, 03 Nov 2026 17:00:00 GMT", "date": DATE}, 3600, "expires"),
    ({"expires": "0", "date": DATE}, 0, "expires-invalid"),
    ({"expires": "Mon, 02 Nov 2026 16:00:00 GMT", "date": DATE}, 0, "expires"),
    ({"cache-control": "no-store, max-age=600"}, 0, "no-store"),
    ({"cache-control": "no-cache"}, 0, "no-cache"),
    ({"cache-control": "max-age=abc", "expires": "Tue, 03 Nov 2026 16:10:00 GMT", "date": DATE}, 600, "expires"),
    ({}, None, "none"),
    ({"cache-control": "public"}, None, "none"),
]


@pytest.mark.parametrize("headers,lifetime,source", LIFETIME_CASES)
def test_lifetime(headers, lifetime, source):
    assert freshness_lifetime(headers) == (lifetime if lifetime is None else float(lifetime), source)


def test_lifetime_private_cache_ignores_s_maxage():
    assert freshness_lifetime({"cache-control": "s-maxage=60, max-age=600"}, shared=False) == (600.0, "max-age")


DATE_CASES = [
    ("Tue, 03 Nov 2026 16:00:00 GMT", 1793721600.0),
    ("Tuesday, 03-Nov-26 16:00:00 GMT", 1793721600.0),
    ("Tue Nov  3 16:00:00 2026", 1793721600.0),
    ("not a date", None),
    ("", None),
]


@pytest.mark.parametrize("value,expected", DATE_CASES)
def test_http_date(value, expected):
    assert parse_http_date(value) == expected


@pytest.mark.parametrize("lm,date,expected", [
    ("Tue, 03 Nov 2026 16:00:00 GMT", DATE, True),
    ("Tue, 03 Nov 2026 15:59:30 GMT", DATE, True),
    ("Tue, 03 Nov 2026 15:58:00 GMT", DATE, False),
    ("Mon, 01 Jan 2024 00:00:00 GMT", DATE, False),
    (None, DATE, None),
    ("garbage", DATE, None),
])
def test_lm_uninformative(lm, date, expected):
    assert lm_uninformative(lm, date) == expected
