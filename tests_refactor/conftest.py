from pathlib import Path
from shutil import copytree

import pytest

from funagent.conf import ConfMgr


@pytest.fixture
def conf_dir(tmp_path):
    copytree(Path(__file__).parents[1] / "examples" / "refactor", tmp_path / "conf")
    return tmp_path / "conf"


@pytest.fixture(autouse=True)
def close_config():
    ConfMgr.Close()
    yield
    ConfMgr.Close()
