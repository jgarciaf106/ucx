from __future__ import annotations

import collections
import email
import json
import logging
import pkgutil
import re
import sys
from dataclasses import dataclass
from email.message import Message
from functools import cached_property
from pathlib import Path

from databricks.labs.blueprint.entrypoint import get_logger

from databricks.labs.ucx.github import GITHUB_URL
from databricks.labs.ucx.hive_metastore.table_migration_status import TableMigrationIndex
from databricks.labs.ucx.source_code.base import Advice, CurrentSessionState
from databricks.labs.ucx.source_code.graph import (
    Dependency,
    DependencyLoader,
    StubContainer,
)
from databricks.labs.ucx.source_code.path_lookup import PathLookup

logger = logging.getLogger(__name__)
KNOWN_URL = f"{GITHUB_URL}/blob/main/src/databricks/labs/ucx/source_code/known.json"

"""
Known libraries that are not in known.json

1) libraries with Python syntax that astroid cannot parse
tempo (error -> multiple exception types must be parenthesized)
chromadb (error -> 'Module' object has no attribute 'doc_node')

2) code that cannot be installed locally
dbruntime (error -> no pypi package)
horovod (error -> Failed to install temporary CMake. Please update your CMake to 3.13+ or set HOROVOD_CMAKE appropriately)
mediamix (requires a workspace, see https://d1r5llqwmkrl74.cloudfront.net/notebooks/CME/media-mix-modeling/index.html#media-mix-modeling_1.html)
mosaic (error -> print without parenthesis not supported by pip)
sat (requires a workspace, see https://github.com/databricks-industry-solutions/security-analysis-tool/blob/main/docs/setup.md)

3) code that cannot be located
utils
util
chedispy

4) code that's imported only once
"""


@dataclass
class Compatibility:
    known: bool
    problems: list[KnownProblem]


@dataclass(unsafe_hash=True, frozen=True, eq=True, order=True)
class KnownProblem:
    code: str
    message: str

    def as_dict(self):
        return {'code': self.code, 'message': self.message}

    def as_advice(self) -> Advice:
        # TODO: Pass on the complete Advice (https://github.com/databrickslabs/ucx/issues/3625)
        return Advice(self.code, self.message, -1, -1, -1, -1)


UNKNOWN = Compatibility(False, [])
_DEFAULT_ENCODING = sys.getdefaultencoding()


class KnownList:
    def __init__(self):
        self._module_problems = collections.OrderedDict()
        self._library_problems = collections.defaultdict(list)
        known = self._get_known()
        for distribution_name, modules in known.items():
            specific_modules_first = sorted(modules.items(), key=lambda x: x[0], reverse=True)
            for module_ref, raw_problems in specific_modules_first:
                problems = [KnownProblem(**_) for _ in raw_problems]
                self._module_problems[module_ref] = problems
                self._library_problems[distribution_name].extend(problems)
        for name in sys.stdlib_module_names:
            self._module_problems[name] = []

    @staticmethod
    def _get_known() -> dict[str, dict[str, list[dict[str, str]]]]:
        module = __name__
        if __name__ == "__main__":  # code path for UCX developers invoking `make known`
            module = "databricks.labs.ucx.source_code.known"
        # load known.json from package data, because we may want to use zipapp packaging
        data = pkgutil.get_data(module, "known.json")
        if not data:
            raise FileNotFoundError("known.json not found")
        return json.loads(data)

    def module_compatibility(self, name: str) -> Compatibility:
        if not name:
            return UNKNOWN
        for module, problems in self._module_problems.items():
            # Find exact matches OR parent module matches
            # Note sorting when constructing module problems from known.json
            if not (name == module or name.startswith(module + ".")):
                continue
            return Compatibility(True, problems)
        return UNKNOWN

    def distribution_compatibility(self, name: str) -> Compatibility:
        if not name:
            return UNKNOWN
        name = self._cleanup_name(name)
        problems = self._library_problems.get(name, None)
        if problems is None:
            return UNKNOWN
        return Compatibility(True, problems)

    @staticmethod
    def _cleanup_name(name) -> str:
        """parses the name to extract the library name, e.g. "numpy==1.21.0" -> "numpy",
        and "dist/databricks_labs_ucx-0.24.0-py3-none-any.whl" -> "databricks-labs-ucx"

        See https://pip.pypa.io/en/stable/reference/requirement-specifiers/#requirement-specifiers
        """
        _requirement_specifier_re = re.compile(r"([a-zA-Z0-9-]+)(?:[<>=].*)?")
        _wheel_name_re = re.compile(r"^([a-zA-Z0-9_]+)-.*\.whl$", re.MULTILINE)
        for matcher in (_wheel_name_re, _requirement_specifier_re):
            maybe_match = matcher.match(name)
            if not maybe_match:
                continue
            raw = maybe_match.group(1)
            return raw.replace('_', '-').lower()
        return name

    @classmethod
    def rebuild(cls, root: Path, *, dry_run: bool = False) -> None:
        """rebuild the known.json file by analyzing the source code of installed libraries. Invoked by `make known`."""
        path_lookup = PathLookup.from_sys_path(root)
        try:
            known_distributions = cls._get_known()
            logger.info("Scanning for newly installed distributions...")
        except FileNotFoundError:
            logger.info("No known distributions found; scanning all distributions...")
            known_distributions = {}
        updated_distributions = known_distributions.copy()
        for library_root in path_lookup.library_roots:
            for dist_info_folder in library_root.glob("*.dist-info"):
                cls._analyze_dist_info(dist_info_folder, updated_distributions, library_root)
        updated_distributions = dict(sorted(updated_distributions.items()))
        if known_distributions == updated_distributions:
            logger.info("No new distributions found.")
        elif dry_run:
            logger.info("Found new distributions during dry run.")
        else:
            known_json = Path(__file__).parent / "known.json"
            with known_json.open('w') as f:
                json.dump(updated_distributions, f, indent=2)
            logger.info(f"Updated known distributions: {known_json.relative_to(Path.cwd())}")

    @classmethod
    def _analyze_dist_info(cls, dist_info_folder, known_distributions, library_root) -> None:
        dist_info = DistInfo(dist_info_folder)
        if dist_info.name in known_distributions:
            logger.debug(f"Skipping distribution: {dist_info.name}")
            return
        logger.info(f"Processing distribution: {dist_info.name}")
        known_distributions[dist_info.name] = collections.OrderedDict()
        for module_path in dist_info.module_paths:
            if not module_path.is_file():
                continue
            if module_path.name in {'__main__.py', '__version__.py', '__about__.py'}:
                continue
            try:
                cls._analyze_file(known_distributions, library_root, dist_info, module_path)
            except RecursionError:
                logger.warning(f"Recursion error in {module_path}")
                continue

    @classmethod
    def _analyze_file(cls, known_distributions, library_root, dist_info, module_path) -> None:
        # Avoiding circular import when rebuilding KnownList as FileLinter and its dependencies expect KnownList to
        # exist, while KnownList needs FileLinter to analyze the source code. Given that building KnownList falls
        # outside the normal execution path, we can safely import FileLinter here.
        # pylint: disable-next=import-outside-toplevel,cyclic-import
        from databricks.labs.ucx.source_code.files import FileLoader

        # pylint: disable-next=import-outside-toplevel,cyclic-import
        from databricks.labs.ucx.source_code.linters.context import LinterContext

        # pylint: disable-next=import-outside-toplevel,cyclic-import
        from databricks.labs.ucx.source_code.linters.files import FileLinter

        empty_index = TableMigrationIndex([])
        relative_path = module_path.relative_to(library_root)
        module_ref = relative_path.as_posix().replace('/', '.')
        for suffix in ('.py', '.__init__'):
            if module_ref.endswith(suffix):
                module_ref = module_ref[: -len(suffix)]
        logger.info(f"Processing module: {module_ref}")
        session_state = CurrentSessionState()
        ctx = LinterContext(empty_index, session_state)
        dependency = Dependency(FileLoader(), module_path, inherits_context=False)
        linter = FileLinter(dependency, PathLookup.from_sys_path(module_path.parent), ctx)
        known_problems = set()
        for problem in linter.lint():
            known_problems.add(KnownProblem(problem.code, problem.message))
        problems = [_.as_dict() for _ in sorted(known_problems)]
        known_distributions[dist_info.name][module_ref] = problems

    def __repr__(self):
        modules = len(self._module_problems)
        libraries = len(self._library_problems)
        return f"<{self.__class__.__name__}: {modules} modules, {libraries} libraries>"


class DistInfo:
    """represents installed library in dist-info format
    see https://packaging.python.org/en/latest/specifications/binary-distribution-format/
    """

    def __init__(self, path: Path):
        self._path = path

    @cached_property
    def module_paths(self) -> list[Path]:
        files = []
        with Path(self._path, "RECORD").open(encoding=_DEFAULT_ENCODING) as f:
            for line in f.readlines():
                filename = line.split(',')[0]
                if not filename.endswith(".py"):
                    continue
                files.append(self._path.parent / filename)
        return files

    @cached_property
    def _metadata(self) -> Message:
        with Path(self._path, "METADATA").open(encoding=_DEFAULT_ENCODING) as f:
            return email.message_from_file(f)

    @property
    def name(self) -> str:
        name = self._metadata.get('Name', 'unknown')
        return name.lower()

    @property
    def library_names(self) -> list[str]:
        names = []
        for requirement in self._metadata.get_all('Requires-Dist', []):
            library = self._extract_library_name_from_requires_dist(requirement)
            names.append(library)
        return names

    @staticmethod
    def _extract_library_name_from_requires_dist(requirement: str) -> str:
        delimiters = {' ', '@', '<', '>', ';'}
        for i, char in enumerate(requirement):
            if char in delimiters:
                return requirement[:i]
        return requirement

    def __repr__(self):
        return f"<DistInfoPackage {self._path}>"


class KnownLoader(DependencyLoader):
    """Always load as `StubContainer`.

    This loader is used in combination with the KnownList to load known dependencies and their known problems.
    """

    def load_dependency(self, path_lookup: PathLookup, dependency: Dependency) -> StubContainer:
        """Load the dependency."""
        _ = path_lookup
        if not isinstance(dependency, KnownDependency):
            raise RuntimeError("Only KnownDependency is supported")
        # Known library paths do not need to be resolved
        return StubContainer(dependency.path)


class KnownDependency(Dependency):
    """A dependency for known libraries, see :class:KnownList."""

    def __init__(self, module_name: str, problems: list[KnownProblem]):
        # Note that Github does not support navigating JSON files, hence the #<module_name> does nothing.
        # https://docs.github.com/en/repositories/working-with-files/using-files/navigating-code-on-github
        super().__init__(KnownLoader(), Path(f"{KNOWN_URL}#{module_name}"), inherits_context=False)
        self._module_name = module_name
        self.problems = problems


if __name__ == "__main__":
    logger = get_logger(__file__)  # this only works for __main__
    KnownList.rebuild(Path.cwd())
