"""B027 回归：真实深层文件存在时，容量观测不能误报为文件丢失。"""

from pathlib import Path

from s16_repository_search.cache import _io_path, cache_directory_bytes


def test_cache_capacity_handles_long_paths_without_changing_logical_identity(tmp_path):
    root = tmp_path / "cache"
    deepest = root
    while len(str(deepest / "code.py")) < 310:
        deepest /= "deep_directory_1234567890"
    _io_path(deepest).mkdir(parents=True)
    source = deepest / "code.py"
    content = b"def add(a, b): return a + b\n"
    _io_path(source).write_bytes(content)
    assert source.resolve() == source
    assert cache_directory_bytes(root) == len(content)
    assert root == Path(str(root))
