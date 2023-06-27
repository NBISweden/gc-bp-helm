import base64
import logging
import os
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_registry_auth_config():
    if settings.COMPONENTS_REGISTRY_INSECURE:
        logger.warning("Refusing to provide credentials to insecure registry")
        return None
    else:
        # Matches format used in docker\api\image.py::ImageApiMixin:pull
        return {
            "username": os.environ.get("COMPONENTS_REGISTRY_USERNAME"),
            "password": os.environ.get("COMPONENTS_REGISTRY_PASSWORD"),
        }
