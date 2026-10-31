#!/usr/bin/env python3
"""Compile a resume in a mandatory TeX Live container and validate its pages."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DEFAULT_IMAGE = "docker.io/texlive/texlive:latest"


class CompileError(Exception):
    pass


def _runtime() -> str:
    docker = shutil.which("docker")
    if docker:
        version = subprocess.run([docker, "--version"], check=False, capture_output=True, text=True)
        output = f"{version.stdout}\n{version.stderr}".lower()
        if "podman" not in output:
            return docker
    podman = shutil.which("podman")
    if podman:
        return podman
    if docker:
        return docker
    raise CompileError("Docker or Podman is required for resume compilation")


def _page_count(pdf: Path) -> int:
    pdfinfo = shutil.which("pdfinfo")
    if not pdfinfo:
        raise CompileError("pdfinfo is required for page-count validation")
    result = subprocess.run([pdfinfo, str(pdf)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise CompileError(f"pdfinfo failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise CompileError("pdfinfo returned a non-numeric page count") from exc
    raise CompileError("pdfinfo output did not contain a page count")


def _image_digest(runtime: str, image: str) -> str:
    result = subprocess.run(
        [runtime, "image", "inspect", "--format", "{{json .RepoDigests}}", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        try:
            digests = json.loads(result.stdout.strip())
            if digests:
                return str(digests[0])
        except json.JSONDecodeError:
            pass
    result = subprocess.run(
        [runtime, "image", "inspect", "--format", "{{.Id}}", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise CompileError(f"could not resolve the container image digest for {image}")


def compile_resume(source: Path, output: Path, page_limit: int, image: str) -> dict[str, object]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_file() or source.suffix.lower() != ".tex":
        raise CompileError(f"LaTeX source does not exist or is not a .tex file: {source}")
    if page_limit < 1:
        raise CompileError("page limit must be a positive integer")

    runtime = _runtime()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="resume-compile-") as temporary:
        build_dir = Path(temporary)
        command = [
            runtime,
            "run",
            "--rm",
            "--network=none",
            "--volume",
            f"{source.parent}:/work:ro",
            "--volume",
            f"{build_dir}:/output",
            "--workdir",
            "/work",
        ]
        if hasattr(os, "getuid") and Path(runtime).name != "podman":
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.extend(
            [
                image,
                "pdflatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-output-directory=/output",
                source.name,
            ]
        )
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            details = (result.stdout + "\n" + result.stderr).strip()
            raise CompileError(f"containerized pdflatex failed:\n{details[-6000:]}")
        built_pdf = build_dir / f"{source.stem}.pdf"
        if not built_pdf.is_file() or built_pdf.stat().st_size == 0:
            raise CompileError("pdflatex completed without producing a non-empty PDF")
        shutil.copy2(built_pdf, output)

    pages = _page_count(output)
    if pages > page_limit:
        output.unlink(missing_ok=True)
        raise CompileError(f"compiled resume has {pages} pages; configured limit is {page_limit}")
    return {
        "runtime": Path(runtime).name,
        "image": image,
        "image_digest": _image_digest(runtime, image),
        "pages": pages,
        "page_limit": page_limit,
        "pdf_path": str(output),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="generated .tex file")
    parser.add_argument("output", type=Path, help="destination .pdf file")
    parser.add_argument("--page-limit", type=int, required=True)
    parser.add_argument("--image", default=os.environ.get("LATEX_CONTAINER_IMAGE", "").strip() or DEFAULT_IMAGE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = compile_resume(args.source, args.output, args.page_limit, args.image)
    except (CompileError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
