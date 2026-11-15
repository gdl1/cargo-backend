import logging
from contextlib import closing
from functools import cached_property
from io import BytesIO

from boto3.session import Session as AwsSession
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image

from app.core.exceptions import AppError

ITEM_PHOTO_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}
ITEM_PHOTO_MAX_BYTES = 5 * 1024 * 1024
ITEM_PHOTO_MAX_PIXELS = 20_000_000

logger = logging.getLogger(__name__)


def photo_too_large() -> AppError:
    return AppError(413, "ITEM_PHOTO_TOO_LARGE", "사진은 5 MiB 및 2천만 픽셀 이하여야 합니다.")


def validate_item_photo(content: bytes, content_type: str) -> None:
    if len(content) > ITEM_PHOTO_MAX_BYTES:
        raise photo_too_large()
    try:
        with Image.open(BytesIO(content), formats=tuple(ITEM_PHOTO_FORMATS.values())) as image:
            if image.format != ITEM_PHOTO_FORMATS[content_type]:
                raise ValueError("파일 형식과 Content-Type이 다릅니다.")
            if image.width * image.height > ITEM_PHOTO_MAX_PIXELS:
                raise photo_too_large()
            image.load()
    except Image.DecompressionBombError as exc:
        raise photo_too_large() from exc
    except (OSError, SyntaxError, ValueError) as exc:
        raise AppError(
            422, "ITEM_PHOTO_INVALID", "유효한 JPEG 또는 PNG 사진을 보내 주세요."
        ) from exc


def _storage_unavailable() -> AppError:
    return AppError(503, "ITEM_PHOTO_STORAGE_UNAVAILABLE", "사진 저장소에 연결할 수 없습니다.")


class ItemPhotoStorage:
    def __init__(self, bucket: str, region: str) -> None:
        self.bucket = bucket.strip()
        self.region = region.strip()

    @cached_property
    def client(self):
        if not self.bucket or not self.region:
            raise AppError(503, "ITEM_PHOTO_NOT_CONFIGURED", "사진 저장소 설정이 필요합니다.")
        return AwsSession().client(
            "s3",
            region_name=self.region,
            config=Config(
                connect_timeout=2,
                read_timeout=3,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )

    def put(self, key: str, content: bytes, content_type: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=content, ContentType=content_type
            )
        except (BotoCoreError, ClientError) as exc:
            logger.warning("내 짐 사진 저장 실패 (%s)", type(exc).__name__)
            raise _storage_unavailable() from exc

    def get(self, key: str) -> tuple[bytes, str]:
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
            with closing(result["Body"]) as body:
                content_type = result.get("ContentType")
                if content_type not in ITEM_PHOTO_FORMATS:
                    raise _storage_unavailable()
                content = body.read(ITEM_PHOTO_MAX_BYTES + 1)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("내 짐 사진 조회 실패 (%s)", type(exc).__name__)
            raise _storage_unavailable() from exc
        if not content or len(content) > ITEM_PHOTO_MAX_BYTES:
            raise _storage_unavailable()
        return content, content_type

    def discard(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("사용하지 않는 내 짐 사진 정리 실패 (%s)", type(exc).__name__)
