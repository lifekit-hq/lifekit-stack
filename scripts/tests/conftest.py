"""Suite-wide isolation for scripts/tests.

Every install-*/ensure-* script ends by writing an install stamp
(scripts/lib/install-stamp.sh, issue 689) to /var/lib/lifekit/installed unless
LIFEKIT_INSTALLED_DIR names another directory. CI runs this suite on the VPS as
the lifekit account, which can write there, so a test that runs an installer
would otherwise record a commit the host never installed. Point the variable at
a throwaway directory before any test starts; a test that checks the default
location drops it from its own subprocess environment.
"""

from __future__ import annotations

import os
import shutil
import tempfile

_STAMPS = tempfile.mkdtemp(prefix="lifekit-installed-")


def pytest_configure(config):
    os.environ["LIFEKIT_INSTALLED_DIR"] = _STAMPS


def pytest_unconfigure(config):
    shutil.rmtree(_STAMPS, ignore_errors=True)
