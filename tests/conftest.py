"""Shared pytest configuration for comsol-support tests."""

import io



def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_comsol: tests requiring a real COMSOL installation (skip with -m 'not real_comsol')",
    )


class FakePopen:
    """Minimal Popen-shaped test double for mphgen's streaming path.

    Provides the subset of the subprocess.Popen API mphgen actually uses:
    - .stdout (a line-iterable text stream)
    - .wait(timeout=...)
    - .returncode
    - .kill() / .poll()

    Use via patch("comsol_support.mphgen.subprocess.Popen",
                   side_effect=make_fake_popen_factory(...)).
    """

    def __init__(self, stdout_text: str = "", returncode: int = 0):
        self.stdout = io.StringIO(stdout_text)
        self._target_rc = returncode
        self.returncode = None

    def wait(self, timeout=None):
        # Stdout is already drained by the caller by the time wait() is
        # invoked; we settle the returncode here to mirror real Popen.
        self.returncode = self._target_rc
        return self._target_rc

    def poll(self):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass
