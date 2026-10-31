from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import compile_resume


class CompileResumeTests(unittest.TestCase):
    def test_runtime_is_mandatory(self):
        with mock.patch.object(compile_resume.shutil, "which", return_value=None):
            with self.assertRaisesRegex(compile_resume.CompileError, "Docker or Podman"):
                compile_resume._runtime()

    def test_docker_is_preferred(self):
        def which(name):
            return f"/usr/bin/{name}"

        version = mock.Mock(stdout="Docker version 28", stderr="")
        with mock.patch.object(compile_resume.shutil, "which", side_effect=which), mock.patch.object(
            compile_resume.subprocess, "run", return_value=version
        ):
            self.assertEqual(compile_resume._runtime(), "/usr/bin/docker")

    def test_podman_wrapper_uses_native_runtime(self):
        def which(name):
            return f"/usr/bin/{name}"

        version = mock.Mock(stdout="podman version 5", stderr="")
        with mock.patch.object(compile_resume.shutil, "which", side_effect=which), mock.patch.object(
            compile_resume.subprocess, "run", return_value=version
        ):
            self.assertEqual(compile_resume._runtime(), "/usr/bin/podman")

    def test_invalid_page_limit_stops_before_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "resume.tex"
            source.write_text("\\documentclass{article}")
            with self.assertRaisesRegex(compile_resume.CompileError, "positive integer"):
                compile_resume.compile_resume(source, source.with_suffix(".pdf"), 0, "image")


if __name__ == "__main__":
    unittest.main()
