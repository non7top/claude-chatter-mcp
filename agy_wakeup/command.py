"""The shell command an agent runs to prime push."""
import os
import sys
import shlex
import shutil
from typing import Optional

from . import paths


class PrimeCommand:
    def __init__(self, script_name: str = "agy-wakeup", module: str = "agy_wakeup.cli", stable_link: Optional[str] = None):
        self._script_name = script_name
        self._module = module
        self._stable_link = stable_link or os.path.join(paths.STABLE_LINK_DIR, script_name)

    def build(self) -> str:
        """The shortest shell command that runs this install's own `--prime`."""
        bin_dir = os.path.dirname(sys.executable)
        script = os.path.join(bin_dir, self._script_name)
        if not os.path.exists(script):
            return f"{shlex.quote(sys.executable)} -m {self._module} --prime"
        # `uv run` puts the project venv's bin on this process's PATH only; the
        # agent's shell won't have it, so don't count that directory as "on PATH".
        other_dirs = [
            d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and os.path.realpath(d) != os.path.realpath(bin_dir)
        ]
        on_path = shutil.which(self._script_name, path=os.pathsep.join(other_dirs))
        if on_path and os.path.realpath(on_path) == os.path.realpath(script):
            return f"{self._script_name} --prime"
        # A uvx cache path changes on every rebuild, which defeats a permission
        # allow-list entry for the command; this link keeps the command stable.
        try:
            self._refresh_stable_link(script)
            return f"{shlex.quote(self._stable_link)} --prime"
        except OSError:
            return f"{shlex.quote(script)} --prime"

    def _refresh_stable_link(self, script: str):
        os.makedirs(os.path.dirname(self._stable_link), exist_ok=True)
        tmp_path = f"{self._stable_link}.{os.getpid()}"
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        os.symlink(script, tmp_path)
        os.replace(tmp_path, self._stable_link)
