"""把每章复制到独立目录，阻断产品和其他教程导入后运行真实入口。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import load_runtime

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = ["s01_agent_loop", "s02_tools", "s03_models", "s04_events", "s05_streaming"]


@pytest.mark.parametrize("chapter", CHAPTERS)
def test_copied_chapter_runs_without_product_or_other_chapters(chapter, tmp_path):
    destination = tmp_path / "lesson"
    shutil.copytree(
        ROOT / "tutorials" / chapter,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("independent snapshot", encoding="utf-8")
    # -I 去掉 PYTHONPATH/用户路径；拦截器也拒绝已安装产品包，避免隔离测试假通过。
    script = """
import importlib.abc, runpy, sys
class BlockMutableSources(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pi_code_agent', 'tutorials', 'archive'}:
            raise ImportError('snapshot cannot import mutable sources: ' + fullname)
sys.meta_path.insert(0, BlockMutableSources())
sys.path.insert(0, sys.argv.pop(1))
runpy.run_module('lesson.code', run_name='__main__')
"""
    arguments = [sys.executable, "-I", "-X", "utf8", "-B", "-c", script, str(tmp_path)]
    if chapter.startswith(("s03", "s04", "s05")):
        arguments += ["--demo", "--workspace", str(workspace), "--prompt", "read README"]
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        arguments,
        cwd=tmp_path,
        env=environment,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "RuntimeWarning" not in completed.stderr
    expected = (
        "independent snapshot"
        if chapter.startswith(("s03", "s04", "s05"))
        else ("5" if chapter.startswith("s02") else tmp_path.name)
    )
    assert expected in completed.stdout
    assert ("[read 开始]" in completed.stdout) == chapter.startswith(("s04", "s05"))


def test_s01_and_s02_do_not_contain_future_model_or_event_components():
    for chapter in CHAPTERS[:2]:
        folder = ROOT / "tutorials" / chapter
        assert not any(
            (folder / name).exists()
            for name in ("providers.py", "config.py", "cli.py", "events.py")
        )
        api = load_runtime("tutorials." + chapter)
        assert "usage" not in api.AssistantMessage.__dataclass_fields__
        assert "error_message" not in api.AssistantMessage.__dataclass_fields__
    first = load_runtime("tutorials.s01_agent_loop")
    assert "parameters" not in first.Tool.__dataclass_fields__


def test_product_sdk_runs_when_all_tutorial_imports_are_blocked(tmp_path):
    script = """
import asyncio, importlib.abc, sys
class BlockTutorials(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'tutorials', 'archive'}:
            raise ImportError('product cannot import tutorials: ' + fullname)
sys.meta_path.insert(0, BlockTutorials())
sys.path.insert(0, sys.argv[1])
from pi_code_agent import UserMessage, agent_loop
from pi_code_agent.demo import DemoModel, add_tool
history = asyncio.run(agent_loop([UserMessage('sum')], model=DemoModel('add', {'a':2,'b':3}), tools=[add_tool()]))
assert history[-2].content == '5'
print('5')
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-B", "-c", script, str(ROOT / "src")],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "5"
