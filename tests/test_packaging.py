"""Packaging checks that twine check doesn't do, so build.sh fails before uploading."""

from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")
trove_classifiers = pytest.importorskip("trove_classifiers")

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_classifiers_are_valid():
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    invalid = [c for c in project["classifiers"] if c not in trove_classifiers.classifiers]
    assert not invalid, f"PyPI rejects these classifiers: {invalid}"
    deprecated = [c for c in project["classifiers"] if c in trove_classifiers.deprecated_classifiers]
    assert not deprecated, f"Deprecated classifiers: {deprecated}"
