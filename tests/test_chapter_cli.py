import json
import subprocess
import sys
from pathlib import Path

import pytest

from easy_writing.cli import main
from easy_writing.storage.database import Database
from easy_writing.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[1]


def call(capsys, args):
    assert main(args) == 0
    output = capsys.readouterr()
    return json.loads(output.out)


def test_full_chapter_cli_flow(capsys, tmp_path):
    project = call(capsys, ["project", "create", "小说"])["id"]
    fixture = ROOT / "examples/stage2/01.json"
    chapter = ROOT / "examples/stage2/01.txt"
    args = ["ingest", str(chapter), "--project", project, "--chapter", "1", "--provider", "fake", "--fixture", str(fixture)]
    assert call(capsys, args)["model_calls"] == 1
    assert call(capsys, args)["cached"]
    facts = call(capsys, ["knowledge", "--project", project, "--kind", "fact"])
    fact = facts[0]
    source = call(capsys, ["source", "--kind", "fact", "--id", fact["id"]])
    assert source[0]["quote"] in chapter.read_text(encoding="utf-8")
    edit = ["edit", "--kind", "fact", "--id", fact["id"], "--field", "object_value", "--value", "林舟", "--reason", "修正"]
    assert "generation_ids" in call(capsys, edit + ["--preview"])
    assert call(capsys, ["history", "--project", project]) == []
    call(capsys, edit)
    assert call(capsys, ["history", "--project", project])[0]["after_json"]["object_value"] == "林舟"
    chapters = call(capsys, ["chapters", "--project", project])
    assert chapters[0]["latest_job"]["completed_blocks"] == 1
    assert chapters[0]["knowledge_is_current"]


def test_readonly_commands_dont_require_deepseek_key(monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    project = call(capsys, ["project", "create", "离线管理"])
    assert call(capsys, ["project", "list"])[0]["id"] == project["id"]


def test_fake_import_without_fixture_is_actionable(capsys):
    project = call(capsys, ["project", "create", "小说"])["id"]
    assert main(["ingest", str(ROOT / "examples/stage2/01.txt"), "--project", project, "--chapter", "1"]) == 1
    assert "--fixture" in capsys.readouterr().err


@pytest.mark.parametrize("file_name,content", [("bad.bin", b"hello"), ("bad.txt", b"\xff\xfe")])
def test_import_file_errors_are_clean(capsys, tmp_path, file_name, content):
    project = call(capsys, ["project", "create", "小说"])["id"]
    path = tmp_path / file_name
    path.write_bytes(content)
    code = main(["ingest", str(path), "--project", project, "--chapter", "1", "--fixture", str(ROOT / "examples/stage2/01.json")])
    assert code == 1
    assert "Traceback" not in capsys.readouterr().err


def test_explicit_database_path_and_alias_commands(capsys, tmp_path):
    db = tmp_path / "custom/book.db"
    project = call(capsys, ["project", "create", "小说", "--db", str(db)])["id"]
    call(capsys, ["ingest", str(ROOT / "examples/stage2/01.txt"), "--project", project, "--chapter", "1", "--fixture", str(ROOT / "examples/stage2/01.json"), "--db", str(db)])
    entities = call(capsys, ["knowledge", "--project", project, "--kind", "entity", "--db", str(db)])
    person = next(e for e in entities if e["name"] == "林舟")
    call(capsys, ["alias", "--id", person["id"], "--name", "阿舟", "--reason", "作者设定", "--db", str(db)])
    assert "阿舟" in next(e for e in Repository(Database(db)).records(project, "entity") if e["id"] == person["id"])["aliases"]


def test_five_chapter_script_reimport_is_fully_cached(tmp_path):
    db = tmp_path / "demo.db"
    env = tmp_path / "test.env"
    env.write_text(f"LLM_USAGE_LOG={tmp_path.as_posix()}/test_usage.jsonl\n", encoding="utf-8")
    command = [sys.executable, str(ROOT / "scripts/demo_stage2.py"), "--db", str(db), "--env-file", str(env)]
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(first.stdout)["chapters"] == 5
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True)
    result = json.loads(second.stdout)
    assert result["cached_chapters"] == 5 and result["model_calls"] == 0
