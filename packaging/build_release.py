"""Build local release artifacts from one clean, exact Git tag."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path


def run(arguments, *, cwd):
    return subprocess.run(arguments, cwd=cwd, check=True, capture_output=True).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--out-dir", type=Path, default=Path("build/release"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if args.tag not in {version, "v" + version}:
        raise SystemExit("Release tag must match the declared project version.")
    run(["git", "check-ref-format", "refs/tags/" + args.tag], cwd=root)
    commit = run(["git", "rev-parse", "--verify", "refs/tags/" + args.tag + "^{commit}"], cwd=root).decode().strip()
    head = run(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    if commit != head or run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root):
        raise SystemExit("Release tag must identify the current clean source commit.")
    if not any((root / name).is_file() for name in ("LICENSE", "LICENSE.md", "COPYING")):
        raise SystemExit("Select and record the owner project license before building a release.")
    if run(["git", "cat-file", "-t", "refs/tags/" + args.tag], cwd=root).strip() != b"tag":
        raise SystemExit("Release tag must be annotated.")
    run(["git", "verify-commit", commit], cwd=root)
    fingerprint = run(["git", "show", "-s", "--format=%GF", commit], cwd=root).decode().strip()
    if not fingerprint:
        raise SystemExit("Release commit must have a verifiable signing fingerprint.")
    title = run(["git", "for-each-ref", "--format=%(contents:subject)", "refs/tags/" + args.tag], cwd=root).decode().strip()
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit("Release output directory must be empty to prevent mixed provenance.")
    archive = run(["git", "archive", "--format=tar", commit], cwd=root)
    with tempfile.TemporaryDirectory(prefix="mpv-tagged-build-") as temporary:
        staging = Path(temporary)
        source_archive = staging / "source.tar"
        source_archive.write_bytes(archive)
        source = staging / "source"
        source.mkdir()
        with tarfile.open(source_archive) as bundle:
            bundle.extractall(source, filter="data")
        subprocess.run(["npm", "ci"], cwd=source / "frontend", check=True)
        subprocess.run(["npm", "run", "build"], cwd=source / "frontend", check=True)
        subprocess.run([sys.executable, "-m", "build", "--sdist", "--outdir", str(output)], cwd=source, check=True)
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(output)], cwd=source, check=True)
    source_name = f"mpv-web-suite-{version}-source.tar.gz"
    (output / source_name).write_bytes(run(["git", "archive", "--format=tar.gz", commit], cwd=root))
    artifacts = [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                 for path in sorted(output.iterdir()) if path.is_file() and path.suffix in {".whl", ".gz"}]
    if not artifacts:
        raise SystemExit("No release artifacts were built.")
    (output / "release-provenance.json").write_text(json.dumps({
        "schema_version": 1, "tag": args.tag, "title": title, "version": version, "commit": commit,
        "commit_signature_verified": True, "signing_fingerprint": fingerprint,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "artifacts": artifacts,
    }, indent=2) + "\n")
    checksums = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                 for path in sorted(output.iterdir()) if path.is_file()]
    (output / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in checksums))


if __name__ == "__main__":
    main()
