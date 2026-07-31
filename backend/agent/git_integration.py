# 移植自 Aider (Apache 2.0 License)
# 源文件: aider/repo.py + aider/commands.py 中的 /commit /undo /diff 命令
# https://github.com/Aider-AI/aider/blob/main/aider/repo.py
#
# 改造说明:
# - 去掉 self.io 依赖，改为 logging.getLogger
# - 去掉 LLM 生成 commit message 的逻辑 (简化为传 message 参数，auto_message 仅做基于文件名的简易生成)
# - 去掉 aider 归因逻辑 (GIT_COMMITTER_NAME / GIT_AUTHOR_NAME / Co-authored-by 等)
# - 用 GitPython (git package) 操作
# - 如果 git 不可用，优雅降级 (所有方法返回安全默认值)
"""Aider Git 集成 - Git 变更提交与差异管理 (移植自 Aider)

封装 GitPython，提供 Agent 编辑代码后常用的 Git 操作: 提交变更、撤销提交、
获取 diff、查询跟踪文件、判断 .gitignore 等。

设计要点:
1. ``GitIntegration`` 在初始化时打开 Git 仓库; GitPython 未安装或目录非仓库时
   优雅降级 (``self.repo = None``)，所有方法返回安全默认值 (None/False/[])。
2. ``commit_changes`` 不再调用 LLM 生成 commit message:
   - 显式传入 ``message`` 时直接使用;
   - ``auto_message=True`` 且无 message 时，基于文件名生成简易消息;
   - ``auto_message=False`` 且无 message 时使用默认占位消息。
3. ``undo_last_commit`` 仅撤销由本实例 ``commit_changes`` 产生的提交 (通过记录
   commit hash 实现 aider 的 "aider 提交" 语义)，``force=True`` 可强制撤销任意提交。
4. 不包含任何 aider 归因 (author/committer 改名、Co-authored-by trailer) 逻辑。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import List, Optional, Set, Union

logger = logging.getLogger(__name__)

# ====== GitPython 可选依赖加载 ======
try:
    import git
    from git import Repo
    from git.exc import (
        GitCommandNotFound,
        GitError,
        InvalidGitRepositoryError,
        NoSuchPathError,
        ODBError,
    )

    _GIT_AVAILABLE = True

    # aider repo.py 中收集的所有需要捕获的异常
    ANY_GIT_ERROR = [
        ODBError,
        GitError,
        InvalidGitRepositoryError,
        GitCommandNotFound,
    ]
except ImportError:
    git = None  # type: ignore[assignment]
    _GIT_AVAILABLE = False
    ANY_GIT_ERROR = []

# 额外的通用异常 (与 aider 保持一致，提高健壮性)
ANY_GIT_ERROR += [
    OSError,
    IndexError,
    BufferError,
    TypeError,
    ValueError,
    AttributeError,
    AssertionError,
    TimeoutError,
]
ANY_GIT_ERROR_TUPLE = tuple(ANY_GIT_ERROR)


class GitIntegration:
    """Git 操作封装 (基于 GitPython)。

    用法::

        gi = GitIntegration("/path/to/repo")
        gi.commit_changes(["src/foo.py"], message="fix: 修复 foo")
        diff = gi.get_diff()
        gi.undo_last_commit()

    Args:
        repo_path: 仓库路径 (默认当前工作目录); 会向上搜索父目录寻找 .git。
        encoding: diff 等文本输出的解码编码。
    """

    def __init__(self, repo_path: Optional[str] = None, encoding: str = "utf-8"):
        self.encoding = encoding
        self.repo: Optional["Repo"] = None
        self.root: Optional[str] = None
        # 记录由本实例提交的 commit hash (短)，用于 undo_last_commit 判断 "aider 提交"
        self._committed_hashes: Set[str] = set()
        # 路径规范化缓存
        self._normalized_path: dict = {}

        if not _GIT_AVAILABLE:
            logger.warning(
                "GitPython 未安装，GitIntegration 功能不可用。请 pip install GitPython"
            )
            return

        search_path = repo_path or os.getcwd()
        try:
            # odbt=GitDB 缓解 GitPython #427 (大仓库性能问题)
            try:
                odbt = git.GitDB
            except AttributeError:  # 老版本 GitPython 可能无 GitDB
                odbt = None
            if odbt is not None:
                self.repo = Repo(search_path, search_parent_directories=True, odbt=odbt)
            else:
                self.repo = Repo(search_path, search_parent_directories=True)
            self.root = os.path.abspath(self.repo.working_tree_dir)
        except (InvalidGitRepositoryError, NoSuchPathError) as err:
            logger.warning("路径 %s 不是 Git 仓库: %s", search_path, err)
            self.repo = None
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("打开 Git 仓库失败 (%s): %s", search_path, err)
            self.repo = None

    # ====== 内部工具 ======

    @property
    def available(self) -> bool:
        """Git 仓库是否可用。"""
        return self.repo is not None

    def _ensure_repo(self) -> bool:
        if self.repo is None:
            logger.debug("Git 仓库不可用，操作被跳过")
            return False
        return True

    def normalize_path(self, path: str) -> str:
        """将 path 规范化为相对仓库根的 POSIX 风格相对路径。"""
        if self.root is None:
            return path
        orig_path = path
        cached = self._normalized_path.get(orig_path)
        if cached:
            return cached
        try:
            p = str(
                Path(PurePosixPath((Path(self.root) / path).relative_to(self.root)))
            )
        except ValueError:
            p = path
        self._normalized_path[orig_path] = p
        return p

    def abs_root_path(self, path: str) -> str:
        """将相对路径解析为仓库根下的绝对路径。"""
        if self.root is None:
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(self.root, path))

    def get_rel_repo_dir(self) -> str:
        if self.repo is None:
            return ""
        try:
            return os.path.relpath(self.repo.git_dir, os.getcwd())
        except (ValueError, OSError):
            return self.repo.git_dir

    # ====== Head commit 查询 ======

    def get_head_commit(self):
        """返回当前 HEAD commit 对象，无 commit 或出错时返回 None。"""
        if not self._ensure_repo():
            return None
        try:
            return self.repo.head.commit
        except (ValueError,) + ANY_GIT_ERROR_TUPLE:
            return None

    def get_head_commit_sha(self, short: bool = False) -> Optional[str]:
        """返回 HEAD commit 的 hash; short=True 返回 7 位短 hash。"""
        commit = self.get_head_commit()
        if not commit:
            return None
        if short:
            return commit.hexsha[:7]
        return commit.hexsha

    def get_head_commit_message(self, default: Optional[str] = None) -> Optional[str]:
        commit = self.get_head_commit()
        if not commit:
            return default
        return commit.message

    # ====== Diff ======

    def get_diff(self, since_commit: Optional[str] = None) -> str:
        """获取 diff 文本。

        Args:
            since_commit: 指定时返回 ``git diff <since_commit> HEAD`` (该提交到 HEAD
                之间的已提交变更); 为 None 时返回工作区相对 HEAD 的未提交变更
                (``git diff HEAD``，与 aider ``get_diffs`` 行为一致)。
        """
        if not self._ensure_repo():
            return ""

        try:
            if since_commit:
                # since_commit 到 HEAD 之间的变更
                return self.repo.git.diff(
                    since_commit, "HEAD", stdout_as_string=False
                ).decode(self.encoding, "replace")
            # 工作区 + 暂存区相对 HEAD 的变更 (匹配 aider get_diffs)
            current_branch_has_commits = False
            try:
                active_branch = self.repo.active_branch
                current_branch_has_commits = any(self.repo.iter_commits(active_branch))
            except ANY_GIT_ERROR_TUPLE:
                pass
            except TypeError:
                pass

            if current_branch_has_commits:
                return self.repo.git.diff("HEAD", stdout_as_string=False).decode(
                    self.encoding, "replace"
                )

            # 无 commit 的空仓库: 分别取 staged 与 unstaged
            diffs = ""
            diffs += self.repo.git.diff("--cached", stdout_as_string=False).decode(
                self.encoding, "replace"
            )
            diffs += self.repo.git.diff(stdout_as_string=False).decode(
                self.encoding, "replace"
            )
            return diffs
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("获取 diff 失败: %s", err)
            return ""

    def diff_commits(
        self, from_commit: str, to_commit: str, pretty: bool = False
    ) -> str:
        """获取两个 commit 之间的 diff。"""
        if not self._ensure_repo():
            return ""
        args = []
        args.append("--color" if pretty else "--color=never")
        args += [from_commit, to_commit]
        try:
            return self.repo.git.diff(*args, stdout_as_string=False).decode(
                self.encoding, "replace"
            )
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("获取 commit diff 失败: %s", err)
            return ""

    # ====== 跟踪文件 / 忽略 ======

    def get_tracked_files(self) -> List[str]:
        """获取 Git 跟踪的文件列表 (HEAD 树 + 暂存区)，排除被忽略的文件。"""
        if not self._ensure_repo():
            return []

        try:
            commit = self.repo.head.commit
        except ValueError:
            commit = None
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("无法列出 Git 跟踪文件: %s", err)
            return []

        files: Set[str] = set()
        if commit:
            try:
                iterator = commit.tree.traverse()
                while True:
                    try:
                        blob = next(iterator)
                        if blob.type == "blob":  # blob 即文件
                            files.add(blob.path)
                    except IndexError:
                        # 树遍历过程中可能出现 IndexError，跳过
                        continue
                    except StopIteration:
                        break
            except ANY_GIT_ERROR_TUPLE as err:
                logger.warning("无法遍历 Git 树: %s", err)
                return []
            files = {self.normalize_path(p) for p in files}

        # 加入暂存区文件
        try:
            index = self.repo.index
            staged_files = [path for path, _ in index.entries.keys()]
            files.update(self.normalize_path(p) for p in staged_files)
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("无法读取暂存区文件: %s", err)

        return [f for f in files if not self.is_ignored(f)]

    def is_ignored(self, filepath: str) -> bool:
        """检查文件是否被 .gitignore 忽略 (基于 ``git check-ignore``)。"""
        if not self._ensure_repo():
            return False
        try:
            if self.repo.ignored(filepath):
                return True
        except ANY_GIT_ERROR_TUPLE:
            return False
        return False

    def path_in_repo(self, path: str) -> bool:
        """判断路径是否在 Git 跟踪文件中。"""
        if not self._ensure_repo() or not path:
            return False
        tracked = set(self.get_tracked_files())
        return self.normalize_path(path) in tracked

    # ====== Dirty 检查 ======

    def is_dirty(self, path: Optional[str] = None) -> bool:
        """判断仓库 (或指定文件) 是否有未提交变更。"""
        if not self._ensure_repo():
            return False
        if path and not self.path_in_repo(path):
            return True
        try:
            return self.repo.is_dirty(path=path)
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("is_dirty 检查失败: %s", err)
            return False

    def get_dirty_files(self) -> List[str]:
        """返回所有有未提交变更的文件 (暂存 + 未暂存)。"""
        if not self._ensure_repo():
            return []
        dirty: Set[str] = set()
        try:
            staged = self.repo.git.diff("--name-only", "--cached").splitlines()
            dirty.update(staged)
            unstaged = self.repo.git.diff("--name-only").splitlines()
            dirty.update(unstaged)
        except ANY_GIT_ERROR_TUPLE as err:
            logger.warning("获取 dirty 文件失败: %s", err)
        return list(dirty)

    # ====== Commit ======

    def _generate_auto_message(self, fnames: Optional[List[str]]) -> str:
        """无 LLM 的简易 commit message 生成 (基于文件名)。"""
        if fnames:
            names = [os.path.basename(f) for f in fnames]
            summary = ", ".join(names)
            if len(summary) > 60:
                summary = summary[:57] + "..."
            return f"chore: update {summary}"
        return "chore: update files"

    def commit_changes(
        self,
        fnames: Optional[Union[str, List[str]]] = None,
        message: Optional[str] = None,
        auto_message: bool = True,
    ) -> Optional[str]:
        """提交变更。

        Args:
            fnames: 要提交的文件列表 (或单个文件字符串); 为 None 时提交所有 dirty
                    文件 (``git commit -a``)。
            message: 显式 commit message; 提供时优先使用。
            auto_message: 无 message 时是否自动生成简易消息 (基于文件名)。

        Returns:
            成功时返回短 commit hash，失败/无可提交时返回 None。
        """
        if not self._ensure_repo():
            return None

        # 规范 fnames 为 list[str] 或 None
        if isinstance(fnames, str):
            fnames = [fnames]

        # 无指定文件且仓库不 dirty，则无变更可提交
        if not fnames and not self.repo.is_dirty():
            logger.debug("没有变更可提交")
            return None

        # 确认确实有 diff
        if fnames:
            # 仅检查指定文件是否有 diff
            has_diff = False
            for f in fnames:
                if not self.path_in_repo(f):
                    has_diff = True
                    break
            if not has_diff:
                try:
                    out = self.repo.git.diff(
                        "HEAD", "--", *fnames, stdout_as_string=False
                    )
                    if out:
                        has_diff = True
                except ANY_GIT_ERROR_TUPLE:
                    pass
            if not has_diff:
                logger.debug("指定文件无变更可提交")
                return None
        elif not self.get_diff():
            logger.debug("没有变更可提交")
            return None

        # 确定 commit message
        if message:
            commit_message = message
        elif auto_message:
            commit_message = self._generate_auto_message(fnames)
        else:
            commit_message = "chore: automated commit"

        try:
            cmd = ["-m", commit_message]
            if fnames:
                abs_fnames = [self.abs_root_path(f) for f in fnames]
                for fname in abs_fnames:
                    try:
                        self.repo.git.add(fname)
                    except ANY_GIT_ERROR_TUPLE as err:
                        logger.warning("无法 git add %s: %s", fname, err)
                cmd += ["--"] + abs_fnames
            else:
                cmd += ["-a"]

            self.repo.git.commit(cmd)
            commit_hash = self.get_head_commit_sha(short=True)
            if commit_hash:
                self._committed_hashes.add(commit_hash)
            logger.info("提交成功: %s %s", commit_hash, commit_message)
            return commit_hash
        except ANY_GIT_ERROR_TUPLE as err:
            logger.error("提交失败: %s", err)
            return None

    def auto_commit_if_dirty(
        self,
        fnames: Optional[Union[str, List[str]]] = None,
        message: Optional[str] = None,
    ) -> Optional[str]:
        """当仓库有未提交变更时自动提交。

        Args:
            fnames: 限定检查/提交的文件; 为 None 时检查整个仓库。
            message: commit message。

        Returns:
            发生提交时返回短 commit hash; 无变更未提交时返回 None。
        """
        if not self._ensure_repo():
            return None

        if isinstance(fnames, str):
            fnames = [fnames]

        dirty = False
        if fnames:
            for f in fnames:
                if self.is_dirty(f) or not self.path_in_repo(f):
                    dirty = True
                    break
        else:
            try:
                dirty = self.repo.is_dirty()
            except ANY_GIT_ERROR_TUPLE as err:
                logger.warning("is_dirty 检查失败: %s", err)
                return None

        if not dirty:
            return None

        return self.commit_changes(fnames=fnames, message=message, auto_message=True)

    # ====== Undo ======

    def undo_last_commit(self, force: bool = False) -> bool:
        """撤销最后一次提交 (``git reset --soft HEAD~1``)。

        默认仅撤销由本实例 ``commit_changes`` 产生的提交 (aider 的 "aider 提交" 语义)。
        若最后一次提交不在本实例记录中，将拒绝撤销并返回 False (除非 ``force=True``)。

        Args:
            force: 为 True 时强制撤销最后一次提交，不校验是否由本实例产生。

        Returns:
            撤销成功返回 True，否则 False。
        """
        if not self._ensure_repo():
            return False

        last_commit = self.get_head_commit()
        if not last_commit or not last_commit.parents:
            logger.warning("这是仓库的第一个提交，无法撤销")
            return False

        if len(last_commit.parents) > 1:
            logger.warning(
                "最后一次提交 %s 是合并提交 (多个父提交)，无法安全撤销",
                last_commit.hexsha,
            )
            return False

        last_commit_hash = self.get_head_commit_sha(short=True)

        # 校验是否由本实例提交
        if not force:
            if not last_commit_hash or last_commit_hash not in self._committed_hashes:
                logger.warning(
                    "最后一次提交 %s 不是由本 GitIntegration 产生，拒绝撤销 "
                    "(可使用 force=True 强制撤销)",
                    last_commit_hash,
                )
                return False

        # 安全检查: 最后一次提交涉及的文件是否有未提交变更 (避免覆盖工作区改动)
        if not force:
            prev_commit = last_commit.parents[0]
            try:
                changed_files = [item.a_path for item in last_commit.diff(prev_commit)]
            except ANY_GIT_ERROR_TUPLE as err:
                logger.warning("无法获取最后一次提交的变更文件: %s", err)
                return False

            for fname in changed_files:
                try:
                    if self.repo.is_dirty(path=fname):
                        logger.warning(
                            "文件 %s 有未提交变更，请先 stash 再撤销，操作中止", fname
                        )
                        return False
                except ANY_GIT_ERROR_TUPLE as err:
                    logger.warning("检查 %s dirty 状态失败: %s", fname, err)
                    return False

        # 执行软撤销
        try:
            self.repo.git.reset("--soft", "HEAD~1")
        except ANY_GIT_ERROR_TUPLE as err:
            logger.error("撤销提交失败: %s", err)
            return False

        if last_commit_hash:
            self._committed_hashes.discard(last_commit_hash)

        new_head_hash = self.get_head_commit_sha(short=True)
        new_head_msg = (self.get_head_commit_message("(unknown)") or "").strip()
        new_head_msg = (new_head_msg.splitlines() or [""])[0]
        logger.info(
            "已撤销提交: %s; 当前 HEAD: %s %s",
            last_commit_hash,
            new_head_hash,
            new_head_msg,
        )
        return True
