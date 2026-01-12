from storages.backends.s3boto3 import S3Boto3Storage
from django.conf import settings


class R2StaticStorage(S3Boto3Storage):
    location = "static"
    default_acl = None
    file_overwrite = True
    custom_domain = settings.AWS_S3_CUSTOM_DOMAIN
    object_parameters = {
        "CacheControl": "public, max-age=31536000, immutable",
    }

    def __init__(self, *args, **kwargs):
        kwargs["custom_domain"] = self.custom_domain
        super().__init__(*args, **kwargs)


class R2MediaStorage(S3Boto3Storage):
    location = "media"
    default_acl = None
    file_overwrite = False
    custom_domain = settings.AWS_S3_CUSTOM_DOMAIN
    
    # Shorter TTL for changing user uploads — but use versioning on update
    object_parameters = {
        "CacheControl": "public, max-age=3600, s-maxage=3600, must-revalidate",
    }

    def __init__(self, *args, **kwargs):
        kwargs["custom_domain"] = self.custom_domain
        super().__init__(*args, **kwargs)

    def url(self, name):
        return super().url(name)
