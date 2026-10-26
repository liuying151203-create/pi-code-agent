import pytest

from pi_code_agent.cli import main


def test_environment_smoke(capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "--workspace" in capsys.readouterr().out
