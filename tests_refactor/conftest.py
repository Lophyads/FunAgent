from pathlib import Path
from shutil import copytree

import pytest

from funagent.conf import ConfMgr


@pytest.fixture
def conf_dir(tmp_path):
    copytree(Path(__file__).parents[1] / "examples" / "refactor", tmp_path / "conf")
    return tmp_path / "conf"


@pytest.fixture(autouse=True)
def close_resources():
    from funagent.model import ModelRuntime
    from funagent.tool import ToolMgr

    try:
        yield
    finally:
        # The production close APIs are also the test isolation boundary.
        try:
            ModelRuntime.Close()
        finally:
            ToolMgr.Close()
            ConfMgr.Close()


@pytest.fixture
def assemble(conf_dir):
    from funagent.contracts import ToolDefinition
    from funagent.model import ModelRuntime
    from funagent.tool import ToolMgr
    from fakes import ChatAdapter, BlocksAdapter, SpyToolHandler

    ConfMgr.Initialize(conf_dir)
    conf = ConfMgr.GetInstance()

    def bind(provider, *, handler=None, secondary=None, adapters=None):
        handler = handler or SpyToolHandler()
        ToolMgr.Initialize({"echo": ToolDefinition("echo", "Echo", {"type": "object"})}, {"echo": handler})
        profiles = {name: conf.Model().Profile(name) for name in ("main", "fallback")}
        providers = {"fake": provider}
        if secondary is not None:
            from dataclasses import replace
            profiles["fallback"] = replace(profiles["fallback"], provider_id="other")
            providers["other"] = secondary
        ModelRuntime.Initialize(profiles, providers, adapters or {"chat": ChatAdapter, "blocks": BlocksAdapter})
        return ModelRuntime.GetInstance(), handler

    yield bind
    try:
        ModelRuntime.Close()
    finally:
        ToolMgr.Close()
