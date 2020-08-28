#!/usr/bin/python3

# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, Union, List

import git
import sh
from packit.specfile import Specfile
from yaml import dump

logger = logging.getLogger(__name__)

# build and test targets
TARGETS = ["centos-stream-x86_64"]
START_TAG = "sg-start"
POST_CLONE_HOOK = "post-clone"
AFTER_PREP_HOOK = "after-prep"
INCLUDE_SOURCES = "include-files"
TEMP_SG_BRANCH = "updates"

# would be better to have them externally (in a file at least)
# but since this is only for kernel, it should be good enough
KERNEL_DEBRAND_PATCH_MESSAGE = """\
Debranding CPU patch

present_in_specfile: true
location_in_specfile: 1000
patch_name: debrand-single-cpu.patch
"""
HOOKS: Dict[str, Dict[str, Any]] = {
    "kernel": {
        # %setup -c creates another directory level but patches don't expect it
        AFTER_PREP_HOOK: (
            "set -e; "
            "shopt -s dotglob nullglob && "  # so that * would match dotfiles as well
            "cd BUILD/kernel-4.18.0-*.el8/ && "
            "mv ./linux-4.18.0-*.el8.x86_64/* . && "
            "rmdir ./linux-4.18.0-*.el8.x86_64 && "
            "git add . && "
            "git commit --amend --no-edit"
            # the patch is already applied in %prep
            # "git apply ../../SOURCES/debrand-single-cpu.patch &&"
            # f"git commit -a -m '{KERNEL_DEBRAND_PATCH_MESSAGE}'"
        )
    },
    "pacemaker": {INCLUDE_SOURCES: ["nagios-agents-metadata-*.tar.gz"]},
}


def get_hook(package_name: str, hook_name: str) -> Optional[str]:
    """ get a hook's command for particular source-git repo """
    return HOOKS.get(package_name, {}).get(hook_name, None)


def _copy_files(
    origin: Path,
    dest: Path,
    glob: str,
    ignore_tarballs: bool = False,
    ignore_patches: bool = False,
    include_files: Optional[List[str]] = None,
) -> None:
    """
    Copy all glob files from origin to dest
    """
    dest.mkdir(parents=True, exist_ok=True)

    present_files = set(origin.glob(glob))
    files_to_copy = set()

    if include_files:
        for i in include_files:
            files_to_copy.update(set(origin.glob(i)))

    if ignore_tarballs:
        for e in ("*.tar.gz", "*.tar.xz", "*.tar.bz2"):
            present_files.difference_update(set(origin.glob(e)))
    if ignore_patches:
        present_files.difference_update(set(origin.glob("*.patch")))

    files_to_copy.update(present_files)

    for file_ in files_to_copy:
        file_dest = dest / file_.name
        logger.debug(f"copying {file_} to {file_dest}")
        shutil.copy2(file_, file_dest)


def get_build_dir(path: Path):
    build_dirs = [d for d in (path / "BUILD").iterdir() if d.is_dir()]
    if len(build_dirs) > 1:
        raise RuntimeError(f"More than one directory found in {path}")
    if len(build_dirs) < 1:
        raise RuntimeError(f"No subdirectory found in {path}")
    return build_dirs[0]


class GitRepo:
    """
    a wrapper on top of git.Repo for our convenience
    """

    def __init__(self, repo_path: Optional[Path], create: bool = False):
        self.repo_path = repo_path
        # some CLI commands don't pass both paths,
        # let's just set it to None and move on
        if not repo_path:
            self.repo = None
        elif create:
            repo_path.mkdir(parents=True, exist_ok=True)
            self.repo = git.Repo.init(repo_path)
        else:
            self.repo = git.Repo(repo_path)

    def __str__(self):
        ref = None
        if self.repo:
            ref = self.repo.active_branch
        return f"GitRepo(path={self.repo_path}, ref={ref})"

    def checkout(self, branch: str, orphan: bool = False, create_branch: bool = False):
        """
        Run `git checkout` in the git repo.

        @param branch: name of the branch to check out
        @param orphan: Create a branch with disconnected history.
        @param create_branch: Create branch if it doesn't exist (using -B)
        """
        options = {}
        if orphan:
            options["orphan"] = branch

        if options:
            self.repo.git.checkout(**options)
        elif create_branch:
            self.repo.git.checkout("-B", branch)
        else:
            self.repo.git.checkout(branch)

    def commit(self, message: str):
        """Commit staged changes in GITDIR."""
        # some of the commits may be empty and it's not an error, e.g. extra source files
        self.repo.git.commit("--allow-empty", m=message)

    def commit_all(self, message: str):
        if self.repo.is_dirty():
            self.stage()
            self.commit(message=message)
        else:
            logger.info("The repo is not dirty, nothing to commit.")

    def fetch(self, remote: Union[str, Path], refspec: str):
        """
        fetch refs from a remote to this repo

        @param remote: str or path of the repo we fetch from
        @param refspec: see man git-fetch
        """
        self.repo.git.fetch(remote, refspec)

    def stage(self, add=None, exclude=None):
        """ stage content in the repo (git add)"""
        if exclude:
            exclude = f":(exclude){exclude}"
            logger.debug(exclude)
        self.repo.git.add(add or ".", exclude)

    def create_tag(self, tag, branch):
        """Create a Git TAG at the tip of BRANCH"""
        self.repo.create_tag(tag, ref=branch, force=True)

    def cherry_pick_base(self, from_branch, to_branch):
        """Cherry-pick the first commit of a branch

        Cherry-pick the first commit of FROM_BRANCH to TO_BRANCH in the
        repository stored in GITDIR.
        """
        num_commits = sum(1 for _ in self.repo.iter_commits(from_branch))
        self.checkout(to_branch, create_branch=True)
        self.repo.git.cherry_pick(f"{from_branch}~{num_commits - 1}")


class Dist2Src:
    """
    A convertor for dist-git rpm repos into a source-git variant.
    """

    def __init__(
        self,
        dist_git_path: Optional[Path],
        source_git_path: Optional[Path],
        log_level: int = 1,
    ):
        """
        both dist_git_path and source_git_path are optional because not all operations require both

        @param dist_git_path: path to the dist-git repo we want to convert
        @param source_git_path: path to a source-git repo (doesn't need to exist)
                                where the conversion output will land
        @param log_level: int, 0 minimal output, 1 verbose, 2 debug
        """
        self.dist_git_path = dist_git_path
        self.dist_git = GitRepo(dist_git_path)
        self.source_git_path = source_git_path
        self.source_git = GitRepo(source_git_path, create=True)
        self.log_level = log_level

    @property
    def package_name(self):
        if self.dist_git_path:
            return self.dist_git_path.name
        elif self.source_git_path:
            return self.source_git_path.name
        raise RuntimeError(
            "I'm sorry but nor dist_git_path nor source_git_path are defined."
        )

    @property
    def relative_specfile_path(self):
        return f"SPECS/{self.package_name}.spec"

    def fetch_archive(
        self,
        get_sources_script_path: str = os.getenv(
            "DIST2SRC_GET_SOURCES", "get_sources.sh"
        ),
    ):
        """
        Fetch archive using get_sources.sh script in the dist-git repo.
        """
        command = sh.Command(get_sources_script_path)

        with sh.pushd(self.dist_git_path):
            logger.info(f"Running command {get_sources_script_path} in {os.getcwd()}")
            stdout = command()

        logger.debug(f"output = {stdout}")

    def _enforce_autosetup(self):
        """
        We are unable to get a git repo when a packages uses %setup + %patch
        so we need to turn %setup into %autosetup -N
        """
        s = Specfile(
            self.dist_git_path / self.relative_specfile_path,
            sources_dir=self.dist_git_path / "SOURCES/",
        )
        prep_lines = s.spec_content.section("%prep")

        for i, line in enumerate(prep_lines):
            if line.startswith(("%autosetup", "%autopatch")):
                logger.info("This package uses %autosetup or %autopatch.")
                # cool, we're good
                return
            elif line.startswith("%setup"):
                # %setup -> %autosetup -p1
                prep_lines[i] = line.replace("%setup", "%autosetup -N")
                # %autosetup does not accept -q, remove it
                prep_lines[i] = re.sub(r"\s+-q", r"", prep_lines[i])

            if prep_lines[i] != line:
                logger.debug(f"{line!r} -> {prep_lines[i]!r}")

        s.save()

    def run_prep(self):
        """
        run `rpmbuild -bp` in the dist-git repo to get a git-repo
        in the %prep phase so we can pick the commits in the source-git repo
        """
        rpmbuild = sh.Command("rpmbuild")

        with sh.pushd(self.dist_git_path):
            cwd = Path.cwd()
            logger.debug(f"Running rpmbuild in {cwd}")
            specfile_path = Path(f"SPECS/{cwd.name}.spec")

            rpmbuild_args = [
                "--nodeps",
                "--define",
                f"_topdir {cwd}",
                "-bp",
            ]
            if self.log_level:  # -vv can be super-duper verbose
                rpmbuild_args.append("-" + "v" * self.log_level)
            rpmbuild_args.append(specfile_path)

            self._enforce_autosetup()

            try:
                running_cmd = rpmbuild(*rpmbuild_args)
            except sh.ErrorReturnCode as e:
                for line in e.stderr.splitlines():
                    logger.debug(str(line))
                raise

            self.dist_git.repo.git.checkout(self.relative_specfile_path)

            logger.debug(f"rpmbuild stdout = {running_cmd}")  # this will print stdout
            logger.info(f"rpmbuild stderr = {running_cmd.stderr.decode()}")

            hook_cmd = get_hook(self.package_name, AFTER_PREP_HOOK)
            if hook_cmd:
                bash = sh.Command("bash")
                bash("-c", hook_cmd)

    def fetch_branch(self, source_branch: str, dest_branch: str):
        """Fetch the branch produced by 'rpmbuild -bp' from the dist-git
        repo to the source-git repo.

        @param source_branch: branch from which we fetch
        @param dest_branch: branch to which the history should be fetched.
        """
        logger.info(
            f"Fetch the dist-git %prep branch to source-git branch {dest_branch}."
        )
        # Make it absolute, so that it's easier to use it with 'fetch'
        # running from dest_dir
        dist_git_BUILD_path = get_build_dir(self.dist_git_path).absolute()

        self.source_git.fetch(dist_git_BUILD_path, f"+{source_branch}:{dest_branch}")

    def convert(self, origin_branch: str, dest_branch: str):
        """
        Convert a dist-git repository into a source-git repo.
        """
        self.dist_git.checkout(branch=origin_branch)
        self.source_git.checkout(branch=dest_branch, orphan=True)

        # expand dist-git and pull the history
        self.fetch_archive()
        self.run_prep()
        self.dist_git.commit_all(message="Changes after running %prep")
        self.fetch_branch(source_branch="master", dest_branch=TEMP_SG_BRANCH)
        self.source_git.cherry_pick_base(
            from_branch=TEMP_SG_BRANCH, to_branch=dest_branch
        )

        # configure packit
        self.add_packit_config()
        self.copy_spec()
        self.source_git.stage(add="SPECS")
        self.source_git.commit(message="Add spec-file for the distribution")

        self.copy_all_sources()
        self.source_git.stage(add="SPECS")
        self.source_git.commit(message="Add sources defined in the spec file")

        # mark the last upstream commit
        self.source_git.create_tag(tag=START_TAG, branch=dest_branch)

        # get all the patch-commits
        self.rebase_patches(from_branch=TEMP_SG_BRANCH, to_branch=dest_branch)

    def add_packit_config(self):
        """
        Add packit config to the source-git repo.
        """
        logger.info("Placing .packit.yaml to the source-git repo and committing it.")
        config = {
            # e.g. qemu-kvm ships "some" spec file in their tarball
            # packit doesn't need to look for the spec when we know where it is
            "specfile_path": self.relative_specfile_path,
            "upstream_ref": START_TAG,
            "jobs": [
                {
                    "job": "copr_build",
                    "trigger": "pull_request",
                    "metadata": {"targets": TARGETS},
                },
                {
                    "job": "tests",
                    "trigger": "pull_request",
                    "metadata": {"targets": TARGETS},
                },
            ],
        }
        self.source_git_path.joinpath(".packit.yaml").write_text(dump(config))
        self.source_git.stage(add=".packit.yaml")
        self.source_git.commit(message=".packit.yaml")

    def copy_all_sources(self):
        """Copy 'SOURCES/*' from a dist-git repo to a source-git repo."""
        dg_path = self.dist_git_path / "SOURCES"
        sg_path = self.source_git_path / "SPECS"
        logger.info(f"Copy all sources from {dg_path} to {sg_path}.")
        include_files = get_hook(self.package_name, INCLUDE_SOURCES)
        _copy_files(
            origin=dg_path,
            dest=sg_path,
            glob="*",
            ignore_tarballs=True,
            ignore_patches=True,
            include_files=include_files,
        )

    def copy_spec(self):
        """
        Copy spec file from the dist-git repo to the source-git repo.
        """
        dg_spec = self.dist_git_path / self.relative_specfile_path
        sg_spec = self.source_git_path / self.relative_specfile_path
        # at this point SPECS/ does not exist, so we need to create it
        sg_spec.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Copy spec file from {dg_spec} to {sg_spec}.")
        shutil.copy2(
            dg_spec,
            sg_spec,
        )

    def rebase_patches(self, from_branch, to_branch):
        """Rebase FROM_BRANCH to TO_BRANCH

        With this commits corresponding to patches can be transferred to
        the TO_BRANCH.

        FROM_BRANCH is cleaned up (deleted).
        """
        logger.info(f"Rebase patches from {from_branch} onto {to_branch}.")
        self.source_git.checkout(from_branch)

        # FIXME: don't call out to source_git.repo directly
        if self.source_git.repo.head.commit.message == "Changes after running %prep\n":
            logger.info("Moving the commit with %prep artifacts behind patches.")
            self.source_git.checkout(to_branch)
            self.source_git.repo.git.cherry_pick(from_branch)
            self.source_git.create_tag(tag=START_TAG, branch=to_branch)
            self.source_git.checkout(from_branch)
            self.source_git.repo.git.reset("--hard", "HEAD^")

        self.source_git.repo.git.rebase("--root", "--onto", to_branch)
        self.source_git.checkout(to_branch)
        self.source_git.repo.git.merge("--ff-only", "-q", from_branch)
        self.source_git.repo.git.branch("-d", from_branch)
