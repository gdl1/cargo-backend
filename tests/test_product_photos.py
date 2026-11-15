import json
import socket
from concurrent.futures import Future
from time import monotonic
from uuid import uuid4

import pytest

from app.schemas.products import ProductCandidateData, ProductSource
from app.services import product_photos

SOURCE_URL = "https://shop.example.com/products/chair"
IMAGE_URL = "https://cdn.example.com/chair-white.jpg"


def candidate(**values):
    return ProductCandidateData(
        candidate_id=uuid4(),
        name="캠핑 의자",
        transport_state="FOLDED",
        sources=[{"url": SOURCE_URL, "title": "제품 상세"}],
        **values,
    )


def test_structured_product_photo_requires_matching_model_and_option(monkeypatch):
    products = [
        {
            "@type": "Product",
            "name": "캠핑 의자",
            "model": "CHAIR-10",
            "color": "화이트",
            "size": "80 x 40 cm",
            "image": [{"@type": "ImageObject", "contentUrl": IMAGE_URL}],
        },
        {
            "@type": "Product",
            "name": "캠핑 의자",
            "model": "CHAIR-10",
            "color": "블랙",
            "size": "80 x 40 cm",
            "image": "/chair-black.jpg",
        },
    ]
    metadata = json.dumps({"@graph": products}, ensure_ascii=False)
    html = f'<script type="application/ld+json">{metadata}</script>'
    pages_read = []

    def read_page(url, deadline):
        pages_read.append(url)
        return url, html

    monkeypatch.setattr(product_photos, "_read_public_html", read_page)
    monkeypatch.setattr(product_photos, "_public_address", lambda host: "93.184.215.14")
    white = candidate(model="CHAIR-10", variant="화이트, 80×40 cm")
    wrong_model = candidate(model="CHAIR-100", variant="화이트")
    ambiguous = candidate(model="CHAIR-10")
    black = candidate(model="CHAIR-10", variant="블랙")
    product_photos.attach_product_photos([white, wrong_model, ambiguous, black])

    assert (white.image_url, white.image_source_url) == (IMAGE_URL, SOURCE_URL)
    assert (black.image_url, black.image_source_url) == (
        "https://shop.example.com/chair-black.jpg",
        SOURCE_URL,
    )
    for item in (wrong_model, ambiguous):
        assert item.image_url is None
        assert item.image_source_url is None
    assert pages_read == [SOURCE_URL]


@pytest.mark.parametrize(
    ("page_type", "photo", "secure_photo", "expected"),
    [
        ("product", IMAGE_URL, None, IMAGE_URL),
        ("website", IMAGE_URL, None, None),
        ("product", "http://cdn.example.com/chair.jpg", None, None),
        ("product", "http://cdn.example.com/chair.jpg", IMAGE_URL, IMAGE_URL),
        ("website", "http://cdn.example.com/chair.jpg", IMAGE_URL, None),
        ("product", IMAGE_URL, "https://cdn.example.com/alternate.jpg", IMAGE_URL),
        ("product", "https://unavailable.example.com/chair.jpg", None, None),
    ],
)
def test_open_graph_uses_public_https_product_photo(
    monkeypatch, page_type, photo, secure_photo, expected
):
    html = (
        '<meta property><script type="application/ld+json">invalid</script>'
        f'<meta property="og:type" content="{page_type}">'
        '<meta property="og:title" content="캠핑 의자 CHAIR-10">'
        f'<meta property="og:image" content="{photo}">'
    )
    if secure_photo:
        html += f'<meta property="og:image:secure_url" content="{secure_photo}">'
    monkeypatch.setattr(product_photos, "_read_public_html", lambda url, deadline: (url, html))
    monkeypatch.setattr(
        product_photos,
        "_public_address",
        lambda host: "93.184.215.14" if host == "cdn.example.com" else None,
    )
    product = candidate(model="CHAIR-10")
    product_photos.attach_product_photos([product])
    assert product.image_url == expected
    assert product.image_source_url == (SOURCE_URL if expected else None)


@pytest.mark.parametrize("preferred_ready", [True, False])
def test_waits_only_for_sources_still_needed_by_candidates(monkeypatch, preferred_ready):
    product = candidate()
    alternate_url = "https://alternate.example.com/products/chair"
    product.sources.append(ProductSource(url=alternate_url, title="다른 제품 상세"))

    ready = Future()
    ready.set_result({product.candidate_id: IMAGE_URL})
    waiting = Future()
    if preferred_ready:
        ready_url, waiting_url = SOURCE_URL, alternate_url
    else:
        ready_url, waiting_url = alternate_url, SOURCE_URL
    lookups = {ready_url: ready, waiting_url: waiting}

    def submit_lookup(_lookup, url, _matches, _deadline):
        return lookups[url]

    monkeypatch.setattr(product_photos._WORKERS, "submit", submit_lookup)
    monkeypatch.setattr(product_photos, "_LOOKUP_SECONDS", 0.01)
    try:
        product_photos.attach_product_photos([product])
        assert (product.image_url, product.image_source_url) == (IMAGE_URL, ready_url)
        assert waiting.cancelled() is preferred_ready
    finally:
        waiting.cancel()


def test_waits_for_shared_source_when_another_candidate_needs_a_photo(monkeypatch):
    shared_url = "https://alternate.example.com/products"
    shared_source = ProductSource(url=shared_url, title="제품 상세")
    first = candidate()
    first.sources.append(shared_source)
    second = candidate()
    second.sources = [shared_source]
    second_image = "https://cdn.example.com/chair-blue.jpg"

    preferred = Future()
    preferred.set_result({first.candidate_id: IMAGE_URL})
    shared = Future()
    shared.set_result({first.candidate_id: second_image, second.candidate_id: second_image})
    lookups = {SOURCE_URL: preferred, shared_url: shared}

    def submit_lookup(_lookup, url, _matches, _deadline):
        return lookups[url]

    monkeypatch.setattr(product_photos._WORKERS, "submit", submit_lookup)
    product_photos.attach_product_photos([first, second])

    assert (first.image_url, first.image_source_url) == (IMAGE_URL, SOURCE_URL)
    assert (second.image_url, second.image_source_url) == (second_image, shared_url)


def test_redirect_destination_is_checked_before_connecting(monkeypatch):
    def resolve(host, port, *, type):
        address = "93.184.215.14" if host == "shop.example.com" else "192.168.0.5"
        return [(socket.AF_INET, type, 0, "", (address, port))]

    connections = []

    class Redirect:
        status = 302
        headers = {"Location": "https://redirect.example.com/elsewhere"}

        def close(self):
            pass

    class Pool:
        def __init__(self, address, **options):
            connections.append((address, options))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def urlopen(self, method, target, **options):
            assert (method, target) == ("GET", "/products/chair")
            assert options["headers"]["Host"] == "shop.example.com"
            assert options["redirect"] is False
            return Redirect()

    monkeypatch.setattr(product_photos.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(product_photos, "HTTPSConnectionPool", Pool)
    assert product_photos._read_public_html(SOURCE_URL, monotonic() + 2) is None
    assert connections == [
        (
            "93.184.215.14",
            {
                "server_hostname": "shop.example.com",
                "assert_hostname": "shop.example.com",
                "cert_reqs": "CERT_REQUIRED",
            },
        )
    ]


def test_legacy_candidate_payload_has_nullable_photo():
    payload = candidate().model_dump(exclude={"image_url", "image_source_url"})
    stored = ProductCandidateData.model_validate(payload)
    assert stored.image_url is None
    assert stored.image_source_url is None
