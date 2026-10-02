import pytest

from config_snapshots import SnapshotStore


@pytest.fixture()
def store(tmp_path):
    with SnapshotStore(str(tmp_path / "snapshots.db")) as s:
        yield s
