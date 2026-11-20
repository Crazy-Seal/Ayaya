from pathlib import Path

WORKDIR = Path.cwd() / "agent_workspace"

def safe_path(p: str) -> Path:
    """把工具相对路径解析到 Agent 工作区，并拒绝解析后越过工作区的路径。

    Args:
        p (str): 工作区内的相对文件路径。

    Returns:
        Path: 工作区内的绝对路径。

    Raises:
        ValueError: 解析结果位于工作区之外。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
