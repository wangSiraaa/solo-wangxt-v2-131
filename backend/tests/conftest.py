import os
import tempfile

from fastapi.testclient import TestClient

_TEMP_DIR = tempfile.mkdtemp(prefix="pq-test-")
os.environ.setdefault("PQ_DATABASE_URL", f"sqlite+pysqlite:///{os.path.join(_TEMP_DIR, 'test.db')}")
os.environ.setdefault("PQ_OBJECT_STORE", "memory")
os.environ.setdefault("PQ_SPOOL_DIR", os.path.join(_TEMP_DIR, "spool"))

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.storage import MemoryObjectStore, set_object_store  # noqa: E402

init_db()
set_object_store(MemoryObjectStore())


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    set_object_store(MemoryObjectStore())
    yield


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
