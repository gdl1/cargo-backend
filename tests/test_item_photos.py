from io import BytesIO
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from PIL import Image

from app.api.dependencies import get_current_user_id
from app.db.session import get_db
from app.services import item_photos, items
from app.services.item_photos import ITEM_PHOTO_MAX_BYTES, ItemPhotoStorage


def _image(format: str = "JPEG", color: str = "red") -> bytes:
    with BytesIO() as stream, Image.new("RGB", (12, 12), color) as image:
        image.save(stream, format=format)
        return stream.getvalue()


def _upload(client, path, content, key=None, media_type="image/jpeg"):
    return client.put(
        path,
        content=content,
        headers={"Content-Type": media_type, "Idempotency-Key": str(key or uuid4())},
    )


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.put_count = 0
        self.fail_put = False

    def put_object(self, *, Bucket, Key, Body, ContentType):
        assert Bucket == "private-item-photos"
        if self.fail_put:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
        self.put_count += 1
        self.objects[Key] = (Body, ContentType)

    def get_object(self, *, Bucket, Key):
        assert Bucket == "private-item-photos"
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body, content_type = self.objects[Key]
        return {"Body": BytesIO(body), "ContentType": content_type}

    def delete_object(self, *, Bucket, Key):
        assert Bucket == "private-item-photos"
        self.objects.pop(Key, None)


@pytest.fixture
def photo_client(application, model_session):
    application.dependency_overrides[get_db] = lambda: model_session
    s3 = MemoryS3()
    with TestClient(application, raise_server_exceptions=False) as client:
        storage = ItemPhotoStorage("private-item-photos", "ap-northeast-2")
        storage.client = s3
        application.state.item_photo_storage = storage
        yield client, s3


def _register(client):
    response = client.post(
        "/api/v1/items",
        json={
            "name": "캠핑 의자",
            "width_cm": 20.5,
            "depth_cm": 30,
            "height_cm": 40,
            "dimension_source": "AR",
        },
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_photo_openapi_documents_binary_upload_and_download(application):
    spec = application.openapi()
    paths = spec["paths"]["/api/v1/items/{saved_item_id}/photo"]
    assert set(paths["put"]["requestBody"]["content"]) == {"image/jpeg", "image/png"}
    assert set(paths["get"]["responses"]["200"]["content"]) == {"image/jpeg", "image/png"}
    schemas = spec["components"]["schemas"]
    assert "photo_url" not in schemas["ItemCreate"]["properties"]
    assert "photo_url" in schemas["ItemData"]["properties"]


def test_rejects_unsupported_oversized_or_invalid_image(application, monkeypatch):
    path = f"/api/v1/items/{uuid4()}/photo"
    with TestClient(application) as client:
        assert _upload(client, path, _image(), media_type="application/json").status_code == 415
        assert _upload(client, path, b"x" * (ITEM_PHOTO_MAX_BYTES + 1)).status_code == 413
        for corrupt in (b"not an image", _image("PNG"), _image()[:-30]):
            result = _upload(client, path, corrupt)
            assert result.status_code == 422
            assert result.json()["code"] == "ITEM_PHOTO_INVALID"
        monkeypatch.setattr(item_photos, "ITEM_PHOTO_MAX_PIXELS", 100)
        assert _upload(client, path, _image()).status_code == 413


@pytest.mark.integration
def test_upload_retry_list_and_download_item_photo(photo_client):
    client, s3 = photo_client
    saved = _register(client)
    assert saved["photo_url"] is None
    path = f"/api/v1/items/{saved['saved_item_id']}/photo"
    assert client.get(path).json()["code"] == "ITEM_PHOTO_NOT_FOUND"
    content, request_key = _image(), uuid4()
    result = _upload(client, path, content, request_key)
    assert result.status_code == 200, result.text
    assert result.json()["data"] == {"saved_item_id": saved["saved_item_id"], "photo_url": path}
    assert _upload(client, path, content, request_key).json() == result.json()
    conflict = _upload(client, path, _image(color="blue"), request_key)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert s3.put_count == len(s3.objects) == 1
    photo = client.get(path)
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["cache-control"] == "private, no-store"
    assert photo.content == content
    listed = client.get("/api/v1/items").json()["data"]["items"]
    assert listed == [{**saved, "photo_url": path}]


@pytest.mark.integration
def test_failed_replacement_preserves_photo_and_can_be_retried(photo_client, monkeypatch):
    client, s3 = photo_client
    item_id = _register(client)["saved_item_id"]
    path = f"/api/v1/items/{item_id}/photo"
    original, replacement = _image(), _image("PNG", "blue")
    assert _upload(client, path, original).status_code == 200
    previous_key = next(iter(s3.objects))
    key = uuid4()
    s3.fail_put = True
    response = _upload(client, path, replacement, key, "image/png")
    assert response.status_code == 503
    assert response.json()["code"] == "ITEM_PHOTO_STORAGE_UNAVAILABLE"
    assert client.get(path).content == original
    s3.fail_put = False

    def fail_to_save(*args):
        raise RuntimeError("저장 실패")

    with monkeypatch.context() as context:
        context.setattr(items, "save_response", fail_to_save)
        assert _upload(client, path, replacement, key, "image/png").status_code == 500
    assert list(s3.objects) == [previous_key]
    assert client.get(path).content == original
    assert _upload(client, path, replacement, key, "image/png").status_code == 200
    assert len(s3.objects) == 1 and previous_key not in s3.objects
    photo = client.get(path)
    assert photo.headers["content-type"] == "image/png"
    assert photo.content == replacement


@pytest.mark.integration
def test_storage_can_be_disabled_and_photo_access_is_scoped(photo_client, application):
    client, s3 = photo_client
    item_id = _register(client)["saved_item_id"]
    path = f"/api/v1/items/{item_id}/photo"
    content, request_key = _image(), uuid4()
    configured_storage = application.state.item_photo_storage
    application.state.item_photo_storage = ItemPhotoStorage("", "")
    unavailable = _upload(client, path, content, request_key)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "ITEM_PHOTO_NOT_CONFIGURED"
    application.state.item_photo_storage = configured_storage
    assert _upload(client, path, content, request_key).status_code == 200
    application.dependency_overrides[get_current_user_id] = lambda: "other-user"
    for response in (client.get(path), _upload(client, path, content, request_key)):
        assert response.status_code == 404
        assert response.json()["code"] == "ITEM_NOT_FOUND"
    assert s3.put_count == 1
