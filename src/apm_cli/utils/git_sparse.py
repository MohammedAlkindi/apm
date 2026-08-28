"""Shared helper for sparse-checkout cone setup (perf #1433).

Extracted so the persistent git cache (``cache.git_cache``) and the
shared-bare materialization path (``deps.bare_cache``) configure
sparse-cone with identical subprocess semantics. Single place to evolve
sparse-checkout behavior (timeouts, additional flags, future
``--no-sparse-index``) without drift between the two call sites.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _find_dangling_symlink(base: Path) -> Path | None:
    """Return the first dangling symlink found under *base*, or ``None``.

    A sparse-cone checkout (perf #1433) only materializes the requested
    top-level paths. If the payload under ``base`` contains a symlink
    whose target lives outside the cone (e.g. ``ref.md ->
    ../../../shared/ref.md``), the symlink entry itself is checked out
    (it's inside the cone) but its target is not, leaving a dangling
    symlink (#2707).

    Uses ``os.path.islink``/``os.path.exists`` directly (rather than
    ``Path.is_symlink``/``Path.exists``) so tests can monkeypatch the
    check in isolation.
    """
    if os.path.islink(base):
        return None if os.path.exists(base) else base
    if not os.path.isdir(base):
        return None
    for root, dirnames, filenames in os.walk(base, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = Path(root) / name
            if os.path.islink(candidate) and not os.path.exists(candidate):
                return candidate
    return None


def repair_dangling_cone_symlinks(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
) -> Path | None:
    """Widen a cone checkout to a full tree if it left a dangling symlink.

    Call AFTER the cone checkout (``apply_sparse_cone`` + ``git
    checkout``) completes. Walks the requested ``paths`` looking for a
    symlink whose target was excluded by the cone. If one is found,
    falls back to ``git sparse-checkout disable`` so every symlink
    target that exists anywhere in the tree resolves (#2707). In a plain
    clone the full tree repopulates from objects already fetched; in a
    partial clone (``--filter=blob:none`` promisor remotes) the disable
    fetches the missing blobs from the remote at repair time.

    This trades the perf-#1433 disk savings for correctness on the repos
    that need it -- a dependency whose payload is mostly symlinks into
    the repo root loses the sparse win on every install. The common case
    (no cross-cone symlinks) pays only the cost of walking the small
    cone directory and never disables sparse-checkout.

    Returns:
        The first dangling symlink found (repo-relative resolution
        already applied by the caller's ``repo_dir``), or ``None`` if
        the cone had no dangling symlinks and no repair was needed.
    """
    dangling: Path | None = None
    for rel in paths:
        dangling = _find_dangling_symlink(repo_dir / rel)
        if dangling is not None:
            break
    if dangling is None:
        return None
    head = [git_exe, *(extra_git_args or [])]
    subprocess.run(
        [*head, "-C", str(repo_dir), "sparse-checkout", "disable"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=True,
    )
    return dangling


def apply_sparse_cone(
    git_exe: str,
    repo_dir: Path,
    paths: list[str],
    *,
    env: dict[str, str] | None,
    timeout: int = 30,
    extra_git_args: list[str] | None = None,
) -> None:
    """Initialize cone-mode sparse checkout and set the requested paths.

    Issues ``git sparse-checkout init --cone`` followed by
    ``git sparse-checkout set <paths...>`` inside ``repo_dir``. Both
    subprocesses run with ``check=True``; failures propagate to the
    caller so silent fallback to a full checkout (which would defeat
    the perf invariant from #1433) is impossible.

    Args:
        git_exe: Absolute path to the git executable.
        repo_dir: Repository working tree to configure.
        paths: Top-level cone paths to materialize. Must be non-empty.
        env: Subprocess environment (auth / safe.bareRepository etc.).
        timeout: Per-subprocess timeout in seconds.
        extra_git_args: Extra args inserted between the git executable
            and the first subcommand (e.g. ``["-c", "core.longpaths=true"]``
            on Windows so the long staged path under ``checkouts_v1/``
            does not trip MAX_PATH when git locks ``.git/config``).
    """
    if not paths:
        return
    head = [git_exe, *(extra_git_args or [])]
    subprocess.run(
        [*head, "-C", str(repo_dir), "sparse-checkout", "init", "--cone"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=True,
    )
    subprocess.run(
        [*head, "-C", str(repo_dir), "sparse-checkout", "set", *paths],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=True,
    )
