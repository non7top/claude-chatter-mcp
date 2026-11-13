"""
Prime: the one object a host or a CLI configures and uses.

It wraps the parts of push priming (the shell command, the PreInvocation hook,
the primer that runs in agy's shell, the poker that holds the endpoint) so they
share one configuration. Imports of the heavier parts are lazy because the hook
path runs before every model invocation.
"""
from typing import Any, Callable, Dict, List, Optional

from .command import PrimeCommand
from .hook import DEFAULT_NOTICE, PrimeHook


class Prime:
    def __init__(
        self,
        script_name: str = "agy-wakeup",
        module: str = "agy_wakeup.cli",
        notice: str = DEFAULT_NOTICE,
        wake_text: Optional[Callable[[List[str]], str]] = None,
        title: str = "agy-wakeup",
        pin_agy_process: bool = False,
        stable_link: Optional[str] = None,
        sessions_dir: Optional[str] = None,
    ):
        self._command = PrimeCommand(script_name, module, stable_link)
        self._hook = PrimeHook(notice)
        self._wake_text = wake_text
        self._title = title
        self._pin = pin_agy_process
        self._sessions_dir = sessions_dir

    def command(self) -> str:
        return self._command.build()

    def run_hook(self, stdin_text: str) -> str:
        return self._hook.run(stdin_text)

    def prime_from_shell(self) -> Dict[str, Any]:
        from .shell import ShellPrimer
        return ShellPrimer(self._sessions_dir).prime()

    def new_poker(self, **overrides):
        from .agentapi import AgentApi
        from .poker import AgyPoker, default_wake_text
        options = {
            "wake_text": self._wake_text or default_wake_text,
            "pin_agy_process": self._pin,
            "api": AgentApi(title=self._title),
        }
        options.update(overrides)
        return AgyPoker(**options)
