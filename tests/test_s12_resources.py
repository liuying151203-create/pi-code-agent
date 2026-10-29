"""s12：真实资源文件、加载顺序、渐进读取和模板替换边界。"""

import asyncio

import pytest
from conftest import ScriptedModel, load_runtime


@pytest.fixture(params=["pi_code_agent", "tutorials.s12_resources", "tutorials.s13_reload"])
def api(request):
    return load_runtime(request.param)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_context_order_and_business_root_boundary(api, tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "business"
    cwd = root / "child"
    cwd.mkdir(parents=True)
    write(tmp_path / "AGENTS.md", "harness must stay outside")
    write(home / "AGENTS.md", "global")
    write(root / "AGENTS.md", "root")
    write(cwd / "AGENTS.md", "child normal")
    write(cwd / "AGENTS.override.md", "child override")
    loader = api.ResourceLoader(cwd, context_root=root, global_dir=home).load()
    assert [body for _, body in loader.context_files] == ["global", "root", "child override"]
    assert "harness" not in loader.system_prompt("base")
    assert api.ResourceLoader(cwd).load().context_files[0][1] == "child override"


def test_skill_metadata_is_lazy_and_disable_only_hides_model_listing(api, tmp_path):
    path = write(
        tmp_path / ".pi-code-agent/skills/review/SKILL.md",
        "---\nname: review\ndescription: >\n  Inspect carefully\ndisable-model-invocation: true\n---\nsecret body",
    )
    loader = api.ResourceLoader(tmp_path).load()
    assert len(loader.skills) == 1 and "review" not in loader.system_prompt("base")
    assert "secret body" in loader.expand("/skill:review target")
    path.write_text(
        path.read_text(encoding="utf-8").replace("secret body", "latest body"), encoding="utf-8"
    )
    assert "latest body" in loader.expand("/skill:review target")
    assert loader.expand("/unknown") == "/unknown"


@pytest.mark.parametrize(
    "content,args,expected",
    [
        ("$1 / $@", ["$2", "x"], "$2 / $2 x"),
        ("${2:-fallback}", ["one"], "fallback"),
        ("${@:2:2}", ["1", "2", "3", "4"], "2 3"),
        ("${ARGUMENTS:-empty}", [], "empty"),
        ("$0", ["one"], ""),
    ],
)
def test_template_substitution_matches_pi_nonrecursive(api, content, args, expected):
    assert api.substitute_args(content, args) == expected
    assert api.parse_command_args("one \"two three\" 'four five'") == [
        "one",
        "two three",
        "four five",
    ]


def test_input_hook_precedes_resource_expansion_and_system_has_metadata(api, tmp_path):
    async def scenario():
        write(tmp_path / "AGENTS.md", "business policy")
        write(
            tmp_path / ".pi-code-agent/prompts/review.md",
            "---\ndescription: Review files\n---\nReview $1 and ${2:-tests}",
        )
        write(
            tmp_path / ".pi-code-agent/skills/a/SKILL.md",
            "---\nname: a\ndescription: Skill A\n---\nFull skill body",
        )
        loader = api.ResourceLoader(tmp_path).load()
        runtime = api.ExtensionRuntime()
        await runtime.register(
            lambda ext: ext.on(
                "input", lambda e, c: {"action": "transform", "text": "/review code"}
            )
        )
        model = ScriptedModel(api.AssistantMessage((), "stop"))
        session = api.AgentSession(model=model, resources=loader, extensions=runtime)
        await session.prompt("original")
        assert session.messages[0].content == "Review code and tests"
        assert (
            "business policy" in model.calls[0]["system"] and "Skill A" in model.calls[0]["system"]
        )
        assert "Full skill body" not in model.calls[0]["system"]
        runtime.close()

    asyncio.run(scenario())


def test_bad_metadata_is_diagnostic_and_duplicate_first_wins(api, tmp_path):
    home = tmp_path / "home"
    write(home / "skills/a/SKILL.md", "---\nname: a\ndescription: first\n---\nbody")
    write(
        tmp_path / ".pi-code-agent/skills/a/SKILL.md",
        "---\nname: a\ndescription: second\n---\nbody",
    )
    write(tmp_path / ".pi-code-agent/skills/b/SKILL.md", "---\nname: b\n---\nmissing")
    write(tmp_path / ".pi-code-agent/prompts/bad.md", "---\nkey: [\n---\nbad")
    loader = api.ResourceLoader(tmp_path, global_dir=home).load()
    assert len(loader.skills) == 1 and loader.skills[0].description == "first"
    assert len(loader.diagnostics) == 3


def test_read_tool_allows_only_selected_skill_root(api, tmp_path):
    async def scenario():
        workspace = tmp_path / "project"
        workspace.mkdir()
        resource = tmp_path / "home/skills/a"
        resource.mkdir(parents=True)
        write(resource / "SKILL.md", "skill body")
        write(resource / "ref.md", "reference")
        write(tmp_path / "home/private.txt", "private")
        tool = api.read_tool(workspace, resource_roots=[resource])

        async def read(path):
            return await api.execute_tool(
                api.ToolCall("1", "read", {"path": str(path)}), {"read": tool}
            )

        assert (await read(resource / "ref.md")).content == "reference"
        assert (await read(tmp_path / "home/private.txt")).is_error
        write(resource / ".env", "secret")
        assert (await read(resource / ".env")).is_error

    asyncio.run(scenario())


def test_nested_reference_markdown_is_not_discovered_as_another_skill(api, tmp_path):
    root = tmp_path / "skills"
    write(root / "loose.md", "---\nname: loose\ndescription: root skill\n---\nbody")
    write(root / "group/reference.md", "---\nname: ref\ndescription: reference document\n---\nbody")
    write(root / "group/actual/SKILL.md", "---\nname: actual\ndescription: actual skill\n---\nbody")
    resources = api.ResourceLoader(tmp_path, skill_paths=[root], defaults=False).load()
    assert {s.name for s in resources.skills} == {"loose", "actual"}
