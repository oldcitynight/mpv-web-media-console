"""Preserve vendored source symlinks when creating a source distribution."""
import os
from pathlib import Path

from setuptools import setup
from setuptools.command.sdist import sdist


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        root = Path(__file__).resolve().parent
        for source in (root / "vendor").rglob("*"):
            if not source.is_symlink():
                continue
            if not source.resolve().is_relative_to(root):
                raise ValueError("vendored source link leaves the source tree")
            target = Path(base_dir) / source.relative_to(root)
            target.unlink()
            target.symlink_to(os.readlink(source))


setup(cmdclass={"sdist": SourceDistribution})
