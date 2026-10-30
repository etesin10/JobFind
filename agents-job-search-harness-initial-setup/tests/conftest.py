import pytest

from jsh.paths import get_paths
from jsh.services import AppServices


@pytest.fixture
def app(tmp_path):
    services = AppServices(get_paths(tmp_path / "portable"))
    yield services
    services.close()
