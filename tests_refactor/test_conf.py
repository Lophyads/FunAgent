import pytest

from funagent.conf import ConfMgr
from funagent.contracts import DomainError


def test_get_before_initialize_does_not_load_files():
    with pytest.raises(DomainError) as error:
        ConfMgr.GetInstance()
    assert error.value.code == "NOT_READY"


def test_load_five_snapshots_and_cache_without_disk_io(conf_dir, monkeypatch):
    ConfMgr.Initialize(conf_dir)
    conf = ConfMgr.GetInstance()
    assert conf.Model().Profile("main").model == "main-model"
    assert "echo" in conf.Tool().Definitions()
    assert conf.Memory().Policy("recall")["sources"] == ("facts",)
    assert conf.Log().Options()["level"] == "INFO"
    assert conf.Serv().Options()["capacity"] == 16
    monkeypatch.setattr("pathlib.Path.read_bytes", lambda *_: pytest.fail("cached read touched disk"))
    assert ConfMgr.GetInstance() is conf
    assert conf.Model().Profile("main") is conf.Model().Profile("main")
    with pytest.raises(TypeError):
        conf.Tool().Definitions()["echo"]["parameters"]["properties"]["value"]["type"] = "number"
    with pytest.raises(AttributeError):
        conf.Model().Profile("main").model = "changed"


def test_failure_does_not_publish_partial_config_and_can_retry(conf_dir):
    original = (conf_dir / "serv.conf").read_text()
    (conf_dir / "serv.conf").write_text("[broken", encoding="utf8")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code == "CONFIG_INVALID"
    with pytest.raises(DomainError, match="not initialized"):
        ConfMgr.GetInstance()
    (conf_dir / "serv.conf").write_text(original, encoding="utf8")
    ConfMgr.Initialize(conf_dir)
    assert ConfMgr.GetInstance().Serv().Options()["capacity"] == 16


def test_close_and_reassemble_without_contaminating_old_snapshot(conf_dir):
    ConfMgr.Initialize(conf_dir)
    old = ConfMgr.GetInstance()
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code == "ALREADY_INITIALIZED"
    ConfMgr.Close()
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()
    file = conf_dir / "serv.conf"
    file.write_text(file.read_text().replace("capacity = 16", "capacity = 2"), encoding="utf8")
    ConfMgr.Initialize(conf_dir)
    assert ConfMgr.GetInstance().Serv().Options()["capacity"] == 2
    assert old.Serv().Options()["capacity"] == 16


@pytest.mark.parametrize("name", ["model", "tool", "memory", "log", "serv"])
def test_missing_file_reports_path_without_partial_state(conf_dir, name):
    file = conf_dir / f"{name}.conf"
    file.unlink()
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code == "CONFIG_MISSING"
    assert str(file) in str(error.value)
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()


@pytest.mark.parametrize("name, old, new", [
    ("model", 'provider_id = "fake"', 'provider_id = "SECRET_TOKEN"'),
    ("model", 'adapter_id = "chat"', 'adapter_id = "SECRET_TOKEN"'),
    ("model", '["fallback"]', '["SECRET_TOKEN"]'),
    ("model", 'timeout = 5.0', 'timeout = 0'),
    ("model", 'timeout = 5.0', 'timeout = nan'),
    ("model", 'max_retries = 1', 'max_retries = -1'),
    ("model", 'max_retries = 1', 'max_retries = true'),
    ("tool", 'main = ["echo"]', 'main = ["SECRET_TOKEN"]'),
    ("tool", 'handler = "echo"', 'handler = "echo"\nbackend_id = "SECRET_TOKEN"'),
    ("memory", 'profile = "main"', 'profile = "SECRET_TOKEN"'),
    ("memory", 'sources = ["facts"]', 'sources = ["SECRET_TOKEN"]'),
    ("memory", 'max_records = 10', 'max_records = 0'),
    ("memory", 'timeout = 2.0', 'timeout = -1'),
    ("serv", 'capacity = 16', 'capacity = 0'),
    ("serv", 'port = 8080', 'port = 70000'),
    ("serv", 'mode = "cli"', 'mode = "SECRET_TOKEN"'),
    ("serv", 'model_profile = "main"', 'model_profile = "SECRET_TOKEN"'),
    ("log", 'max_bytes = 1048576', 'max_bytes = -1'),
    ("log", 'level = "INFO"', 'level = "SECRET_TOKEN"'),
])
def test_invalid_values_and_references_fail_closed(conf_dir, caplog, name, old, new):
    file = conf_dir / f"{name}.conf"
    file.write_text(file.read_text().replace(old, new), encoding="utf8")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code in {"CONFIG_INVALID", "CONFIG_REFERENCE"}
    assert "SECRET_TOKEN" not in str(error.value)
    assert "SECRET_TOKEN" not in repr(error.value)
    assert "SECRET_TOKEN" not in caplog.text
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()


@pytest.mark.parametrize("name", ["model", "tool", "memory", "log", "serv"])
def test_wrong_table_shapes_have_public_errors(conf_dir, name):
    (conf_dir / f"{name}.conf").write_text('options = "SECRET_TOKEN"', encoding="utf8")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code == "CONFIG_INVALID"
    assert "SECRET_TOKEN" not in str(error.value)


def test_parse_error_never_echoes_secret(conf_dir, caplog):
    (conf_dir / "model.conf").write_text('password = "SECRET_TOKEN"\n[invalid', encoding="utf8")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert "SECRET_TOKEN" not in str(error.value) + caplog.text


def test_startup_only_checks_config_and_can_be_reused(conf_dir, capsys):
    from funagent.global_server import Initialize, Stop, main
    from funagent.conf import ModelConf, ToolConf, MemoryConf, LogConf, ServConf

    conf = Initialize(conf_dir)
    for snapshot, abstract in zip(
        (conf.Model(), conf.Tool(), conf.Memory(), conf.Log(), conf.Serv()),
        (ModelConf, ToolConf, MemoryConf, LogConf, ServConf),
    ):
        assert isinstance(snapshot, abstract)
    Stop()
    assert main(["--conf-dir", str(conf_dir), "--check-config"]) == 0
    assert "Configuration valid." in capsys.readouterr().out
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()


@pytest.mark.parametrize("old,new", [
    ('capabilities = ["search", "list", "write", "update", "delete"]', 'capabilities = ["invented"]'),
    ('path = "memory.sqlite3"', 'path = ""'),
    ('kind = "episodic"', 'kind = "semantic"'),
    ('enabled = false', 'enabled = "yes"'),
    ('max_records = 10', 'max_records = 101'),
    ('timeout = 2.0', 'timeout = 2.0\nmax_chars = 0'),
    ('path = "memory.sqlite3"', 'path = "memory.sqlite3"\nread_access = ""'),
])
def test_memory_configuration_rejects_invalid_capabilities_scope_and_budgets(conf_dir, old, new):
    file = conf_dir / "memory.conf"
    file.write_text(file.read_text().replace(old, new), encoding="utf8")
    with pytest.raises(DomainError) as error:
        ConfMgr.Initialize(conf_dir)
    assert error.value.code == "CONFIG_INVALID"
    with pytest.raises(DomainError):
        ConfMgr.GetInstance()
