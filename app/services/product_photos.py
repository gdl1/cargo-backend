import json
import re
import socket
from concurrent.futures import ThreadPoolExecutor, wait
from email.message import Message
from html.parser import HTMLParser
from ipaddress import ip_address
from itertools import islice
from threading import BoundedSemaphore
from time import monotonic
from unicodedata import normalize
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID

from urllib3 import HTTPSConnectionPool, Timeout
from urllib3.exceptions import HTTPError

from app.schemas.products import ProductCandidateData

_LOOKUP_SECONDS = 2.0
_MAX_SOURCE_PAGES = 6
_MAX_HTML_BYTES = 512 * 1024
_WORKER_COUNT = 8
_WORKERS = ThreadPoolExecutor(max_workers=_WORKER_COUNT, thread_name_prefix="product-photo")
_WORKER_SLOTS = BoundedSemaphore(_WORKER_COUNT)
_IDENTITY_FIELDS = ("name", "model", "sku", "mpn", "color", "size", "material", "pattern")


def _https_host(url: str) -> str | None:
    if len(url) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f-\x9f\ud800-\udfff]", url):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.port not in (None, 443)
        ):
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    labels = host.split(".")
    if len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ):
        return None
    return host


def _public_address(host: str) -> str | None:
    addresses = [
        ip_address(sockaddr[0])
        for _, _, _, _, sockaddr in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ]
    if not addresses or any(
        not address.is_global or address.is_multicast or address.is_reserved
        for address in addresses
    ):
        return None
    return str(min(addresses, key=lambda address: address.version))


def _read_public_html(url: str, deadline: float) -> tuple[str, str] | None:
    # 자동 리디렉션과 프록시를 사용하지 않습니다. 이동할 때마다 새 목적지를 다시 검증합니다.
    for _ in range(2):
        host = _https_host(url)
        address = _public_address(host) if host is not None and monotonic() < deadline else None
        if address is None or monotonic() >= deadline:
            return None

        parsed = urlsplit(url)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        # 검증한 IP에 바로 연결하고 원래 호스트로 TLS 인증서와 SNI를 확인합니다.
        with HTTPSConnectionPool(
            address, server_hostname=host, assert_hostname=host, cert_reqs="CERT_REQUIRED"
        ) as pool:
            response = pool.urlopen(
                "GET",
                target,
                headers={"Host": host, "Accept": "text/html", "Accept-Encoding": "identity"},
                timeout=Timeout(connect=0.8, read=0.8),
                retries=False,
                redirect=False,
                preload_content=False,
                decode_content=False,
            )
            try:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                content_type = Message()
                content_type["Content-Type"] = response.headers.get("Content-Type", "")
                if response.status != 200 or content_type.get_content_type() not in (
                    "text/html",
                    "application/xhtml+xml",
                ):
                    return None
                body = bytearray()
                while len(body) < _MAX_HTML_BYTES and monotonic() < deadline:
                    chunk = response.read1(min(16384, _MAX_HTML_BYTES - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                charset = content_type.get_content_charset() or "utf-8"
                try:
                    html = body.decode(charset, errors="replace")
                except LookupError:
                    html = body.decode("utf-8", errors="replace")
                return url, html
            finally:
                response.close()
    return None


class _ProductPage(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.products = []
        self.meta = {}
        self._script = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self._script = []
        property_name = attrs.get("property") or ""
        if tag == "meta" and property_name.startswith("og:"):
            self.meta.setdefault(property_name, attrs.get("content") or "")

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag != "script" or self._script is None:
            return
        try:
            nodes = [json.loads("".join(self._script))]
        except (ValueError, RecursionError):
            return
        finally:
            self._script = None
        while nodes:
            node = nodes.pop()
            if isinstance(node, list):
                nodes.extend(reversed(node))
            if isinstance(node, dict):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if any(
                    isinstance(kind, str) and kind.rsplit("/", 1)[-1] == "Product" for kind in types
                ):
                    self.products.append(node)
                nodes.extend((node.get("@graph"), node.get("hasVariant")))


def _words(text: str) -> str:
    text = normalize("NFKC", text).casefold().replace("×", "x")
    text = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "x", text)
    return " ".join(re.findall(r"[^\W_]+", text))


def _contains(identity: str, text: str) -> bool:
    words = _words(text)
    return bool(words) and f" {words} " in identity


def _image_urls(value):
    for image in value if isinstance(value, list) else [value]:
        if isinstance(image, dict):
            image = image.get("contentUrl") or image.get("url")
        if isinstance(image, str) and image:
            yield image


def _matching_image_url(
    page: _ProductPage, url: str, candidate: ProductCandidateData
) -> str | None:
    products = page.products
    if not products and page.meta.get("og:type", "").strip().lower() == "product":
        products = [{"name": page.meta.get("og:title"), "image": page.meta.get("og:image")}]
    images = set()
    for product in products:
        name = product.get("name")
        if not isinstance(name, str) or not _contains(f" {_words(name)} ", candidate.name):
            continue
        fields = [product.get(field) for field in _IDENTITY_FIELDS]
        identity = " " + _words(" ".join(value for value in fields if isinstance(value, str))) + " "
        if any(
            value is not None and not _contains(identity, value)
            for value in (candidate.model, candidate.variant)
        ):
            continue
        for raw_url in _image_urls(product.get("image")):
            try:
                image_url = urljoin(url, raw_url)
            except ValueError:
                continue
            if _https_host(image_url):
                images.add(image_url)
                break
    return next(iter(images)) if len(images) == 1 else None


def _photos_on_page(
    url: str, candidates: list[ProductCandidateData], deadline: float
) -> dict[UUID, str]:
    try:
        loaded = _read_public_html(url, deadline)
        if loaded is None or monotonic() >= deadline:
            return {}
        final_url, html = loaded
        page = _ProductPage(html)
    except (HTTPError, OSError, ValueError, AssertionError, RecursionError):
        return {}

    photos = {}
    public_hosts = {}
    for candidate in candidates:
        if monotonic() >= deadline:
            break
        photo = _matching_image_url(page, final_url, candidate)
        host = _https_host(photo) if photo is not None else None
        if host is None:
            continue
        if host not in public_hosts:
            try:
                public_hosts[host] = _public_address(host) is not None
            except OSError:
                public_hosts[host] = False
        if public_hosts[host]:
            photos[candidate.candidate_id] = photo
    return photos


def _release_worker(_future):
    _WORKER_SLOTS.release()


def attach_product_photos(candidates: list[ProductCandidateData]) -> None:
    sources: dict[str, list[ProductCandidateData]] = {}
    for candidate in candidates:
        for source in candidate.sources:
            if _https_host(source.url):
                sources.setdefault(source.url, []).append(candidate)

    deadline = monotonic() + _LOOKUP_SECONDS
    lookups = {}
    for url, matches in islice(sources.items(), _MAX_SOURCE_PAGES):
        if not _WORKER_SLOTS.acquire(blocking=False):
            break
        future = _WORKERS.submit(_photos_on_page, url, matches, deadline)
        future.add_done_callback(_release_worker)
        lookups[url] = future

    done, _ = wait(lookups.values(), timeout=max(0, deadline - monotonic()))
    for url, future in lookups.items():
        if future in done:
            photos = future.result()
            for candidate in sources[url]:
                if candidate.image_url is None and candidate.candidate_id in photos:
                    candidate.image_url = photos[candidate.candidate_id]
                    candidate.image_source_url = url
