"""Installing a certificate must not become a way of reading root's files.

The staging directory is the one place in this appliance where two privilege
levels share a path. The unprivileged control plane writes a certificate and a
key there; the helper, which runs as root, reads them and installs them where
the console will serve from.

A security review pointed out that the helper read that path twice: once to
check the pair matched, and again to copy it into place, with the account that
owns the directory free to change what is there in between.

That is two faults wearing one description, and only one of them is a race.

The one that needs no race at all is demonstrated below and was measured
against the code as it stood: stage a public certificate honestly, link the key
beside it at a private key root holds, and the pair matches, because a
certificate and its key match whoever is holding them. The helper copied that
private key out at mode six four zero owned by root and the service group,
which is to say it handed the service account a key it could not otherwise
read. No timing, no window, and no second process — just a link.

The other is the race the review named, and it is closed by construction rather
than by a test that would have to win a timing contest to mean anything: the
material is copied into a directory only root can enter before anything looks
at it, and every read afterwards is of that copy. There is no second read of a
path the caller can still write, so there is nothing left to swap.

Asserting that the helper refused is not enough on its own: a refusal proves a
branch was taken, and the finding was about what was copied. So the installed
bytes are read back and searched for the material by name.

The helper is sourced rather than run, because running it needs a service
manager and an appliance underneath it. Sourcing reaches the one operation
under examination and leaves the rest alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import REPOSITORY_ROOT

HELPER = REPOSITORY_ROOT / "scripts" / "myipbx-privileged-helper.sh"

#: What a stolen file would contain. Distinctive enough that finding it
#: anywhere in the installed material is unambiguous.
SECRET = "root:$6$stolen$THIS-IS-THE-SHADOW-FILE:19000:0:99999:7:::"


def _openssl_available() -> bool:
    return shutil.which("openssl") is not None


def _stage_a_real_pair(directory: Path) -> None:
    """Generate a certificate and key that actually match.

    Generated rather than pasted, so that the honest path is exercised against
    material openssl agrees with rather than against a fixture that happens to
    parse.
    """
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(directory / "appliance.key"),
            "-out", str(directory / "appliance.crt"),
            "-days", "1", "-subj", "/CN=appliance.invalid",
        ],
        check=True,
        capture_output=True,
    )


class CertificateStagingTests(unittest.TestCase):
    """What the helper will and will not copy out of the staging directory."""

    def setUp(self) -> None:
        if not _openssl_available():
            self.skipTest("openssl is not installed on this machine")
        self.directory = tempfile.TemporaryDirectory(prefix="myipbx-tls-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.staged = self.root / "staged"
        self.installed = self.root / "installed"
        self.staged.mkdir()
        self.secret_file = self.root / "shadow"
        self.secret_file.write_text(SECRET + "\n", encoding="utf-8")
        self.secret_file.chmod(0o600)

    def _environment(self, **extra: str) -> dict[str, str]:
        """The helper's world, with the service manager stood in for.

        The operation restarts the console when it succeeds, and a test that
        restarted the console of the machine running it would be a poor test.
        A stub in front of the real one records the call and returns success,
        so the honest path runs to its end without touching this machine.
        """
        shim = self.root / "shim"
        shim.mkdir(exist_ok=True)
        stub = shim / "systemctl"
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "{self.root / "systemctl.calls"}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

        environment = dict(os.environ)
        environment["STAGED_TLS_DIR"] = str(self.staged)
        environment["INSTALLED_TLS_DIR"] = str(self.installed)
        environment["PATH"] = str(shim) + os.pathsep + environment.get("PATH", "")
        environment.update(extra)
        return environment

    def _apply(self, **extra: str) -> subprocess.CompletedProcess[str]:
        """Source the helper and run the one operation, nothing else."""
        script = (
            f'source "{HELPER}"\n'
            'operation_certificate_apply\n'
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=self._environment(**extra),
            cwd=str(REPOSITORY_ROOT),
        )

    def _installed_text(self) -> str:
        """Everything the helper left behind, read as one body of text."""
        if not self.installed.exists():
            return ""
        pieces = []
        for path in sorted(self.installed.rglob("*")):
            if path.is_file():
                pieces.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(pieces)

    def test_an_honest_pair_is_installed(self) -> None:
        """The guard must not stop the operation it exists to protect."""
        _stage_a_real_pair(self.staged)
        result = self._apply()
        self.assertEqual(
            result.returncode, 0,
            f"a matching pair was refused:\n{result.stdout}\n{result.stderr}",
        )
        self.assertTrue((self.installed / "appliance.crt").is_file())
        self.assertTrue((self.installed / "appliance.key").is_file())
        self.assertIn("BEGIN CERTIFICATE", (self.installed / "appliance.crt").read_text())

    def test_a_staged_certificate_that_is_a_link_installs_nothing(self) -> None:
        _stage_a_real_pair(self.staged)
        (self.staged / "appliance.crt").unlink()
        (self.staged / "appliance.crt").symlink_to(self.secret_file)

        result = self._apply()

        self.assertNotEqual(result.returncode, 0, "a linked certificate was accepted")
        self.assertNotIn(
            SECRET, self._installed_text(),
            "the contents of a file the caller pointed at were copied into the "
            "directory the console serves from",
        )

    def test_a_staged_key_that_is_a_link_installs_nothing(self) -> None:
        _stage_a_real_pair(self.staged)
        (self.staged / "appliance.key").unlink()
        (self.staged / "appliance.key").symlink_to(self.secret_file)

        result = self._apply()

        self.assertNotEqual(result.returncode, 0, "a linked key was accepted")
        self.assertNotIn(SECRET, self._installed_text())

    def test_a_key_linked_at_root_held_material_is_not_copied_out(self) -> None:
        """The disclosure that needs no race.

        A certificate and its key match whoever holds them, so staging a public
        certificate and linking the key at one root keeps privately passed the
        pairing check and installed the private key at a mode the service
        account can read. Measured against the code as it stood before this
        guard: the key was copied out, mode six four zero.
        """
        private = self.root / "root-held"
        private.mkdir()
        private.chmod(0o700)
        _stage_a_real_pair(private)
        (private / "appliance.key").chmod(0o600)

        shutil.copyfile(private / "appliance.crt", self.staged / "appliance.crt")
        (self.staged / "appliance.key").symlink_to(private / "appliance.key")

        result = self._apply()

        self.assertNotEqual(
            result.returncode, 0,
            "a key linked at material the caller cannot read was accepted",
        )
        installed_key = self.installed / "appliance.key"
        self.assertFalse(
            installed_key.exists() and
            installed_key.read_text() == (private / "appliance.key").read_text(),
            "a private key root holds was copied into the directory the "
            "service account reads",
        )

    def test_a_staging_directory_that_is_a_link_installs_nothing(self) -> None:
        """The directory is as substitutable as the files inside it."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        _stage_a_real_pair(elsewhere)
        shutil.rmtree(self.staged)
        self.staged.symlink_to(elsewhere, target_is_directory=True)

        result = self._apply()

        self.assertNotEqual(
            result.returncode, 0,
            "the helper read its inputs through a link the caller controls",
        )

    def test_nothing_is_installed_when_the_pair_does_not_match(self) -> None:
        """The check that was already there still has to work after the copy."""
        _stage_a_real_pair(self.staged)
        other = self.root / "other"
        other.mkdir()
        _stage_a_real_pair(other)
        shutil.copyfile(other / "appliance.key", self.staged / "appliance.key")

        result = self._apply()

        self.assertNotEqual(result.returncode, 0, "a mismatched pair was installed")
        self.assertFalse((self.installed / "appliance.crt").exists())

    def test_the_private_copy_does_not_outlive_the_operation(self) -> None:
        """A second copy of a private key is not left lying in the temporary area."""
        _stage_a_real_pair(self.staged)
        scratch = self.root / "scratch"
        scratch.mkdir()

        self._apply(TMPDIR=str(scratch))

        left_behind = sorted(path.name for path in scratch.iterdir())
        self.assertEqual(
            left_behind, [],
            f"the helper left its working copy behind: {left_behind}",
        )


if __name__ == "__main__":
    unittest.main()
