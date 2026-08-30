"""Real-Git regression coverage for sparse-cone symlink repair."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.models.apm_package import DependencyReference
from apm_cli.utils.git_sparse import (
    apply_sparse_cone,
    repair_dangling_cone_symlinks,
    validate_materialized_symlinks,
)
from apm_cli.utils.path_security import PathTraversalError

pytestmark = pytest.mark.component


def _commit_symlink_repo(tmp_path: Path, target: str) -> Path:
    """Create a bare repo with a tracked symlink inside the package cone."""
    work = tmp_path / "work"
    package = work / "packages" / "tool"
    shared = work / "shared"
    package.mkdir(parents=True)
    shared.mkdir()
    (package / "apm.yml").write_text("name: tool\nversion: 1.0.0\n", encoding="utf-8")
    (shared / "reference.md").write_text("shared content\n", encoding="utf-8")
    (package / "reference.md").symlink_to(target)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "APM Test"], check=True)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "fixture"], check=True)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


def _checkout_sparse(tmp_path: Path, bare: Path) -> Path:
    consumer = tmp_path / "consumer"
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", str(bare), str(consumer)],
        check=True,
    )
    apply_sparse_cone("git", consumer, ["packages/tool"], env=os.environ.copy())
    subprocess.run(["git", "-C", str(consumer), "checkout", "-q", "HEAD"], check=True)
    return consumer


@pytest.mark.skipif(os.name == "nt", reason="Git materializes plain files by default on Windows")
def test_legacy_downloader_repairs_real_out_of_cone_symlink(tmp_path: Path) -> None:
    """The no-cache downloader path must widen and return a live package link."""
    bare = _commit_symlink_repo(tmp_path, "../../shared/reference.md")
    checkout = tmp_path / "legacy"
    downloader = object.__new__(GitHubPackageDownloader)
    downloader.git_env = {}
    downloader.github_token = None
    downloader.auth_resolver = MagicMock()
    downloader.auth_resolver.uses_public_github_anonymous_first.return_value = False
    downloader._resolve_dep_auth_ctx = lambda dep: None
    downloader._build_repo_url = lambda *args, **kwargs: str(bare)
    dep = DependencyReference(repo_url="owner/repo", reference="main")

    assert downloader._try_sparse_checkout(dep, checkout, "packages/tool", "main") is True
    installed_link = checkout / "packages" / "tool" / "reference.md"
    assert installed_link.is_symlink()
    assert installed_link.resolve().read_text(encoding="utf-8") == "shared content\n"
    assert (checkout / "shared" / "reference.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="Git materializes plain files by default on Windows")
def test_repair_rejects_link_that_remains_broken(tmp_path: Path) -> None:
    """Full-tree fallback must explain a link whose target is absent from Git."""
    bare = _commit_symlink_repo(tmp_path, "../../missing/reference.md")
    consumer = _checkout_sparse(tmp_path, bare)

    with pytest.raises(RuntimeError, match="repair its target in the package repository"):
        repair_dangling_cone_symlinks(
            "git",
            consumer,
            ["packages/tool"],
            env=os.environ.copy(),
        )


@pytest.mark.skipif(os.name == "nt", reason="Git materializes plain files by default on Windows")
def test_materialization_rejects_symlink_outside_checkout(tmp_path: Path) -> None:
    """Remote package copies must remain within the pinned checkout."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    bare = _commit_symlink_repo(tmp_path, str(outside))
    consumer = _checkout_sparse(tmp_path, bare)

    with pytest.raises(PathTraversalError, match="outside the allowed base directory"):
        validate_materialized_symlinks(
            "git",
            consumer,
            ["packages/tool"],
            env=os.environ.copy(),
        )
