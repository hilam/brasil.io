from pathlib import Path

from .settings import *  # noqa

for queue in RQ_QUEUES.values():  # noqa
    queue["ASYNC"] = False


SAMPLE_SPREADSHEETS_DATA_DIR = Path(BASE_DIR).joinpath("covid19", "tests", "data")  # noqa
CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache",}}  # noqa

RATELIMIT_ENABLE = False  # noqa
