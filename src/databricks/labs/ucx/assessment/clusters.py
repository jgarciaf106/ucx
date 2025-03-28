import base64
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from databricks.labs.lsql.backends import SqlBackend
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, BadRequest
from databricks.sdk.service.compute import (
    ClusterDetails,
    ClusterSource,
    DataSecurityMode,
    DbfsStorageInfo,
    InitScriptInfo,
    LocalFileInfo,
    Policy,
    WorkspaceStorageInfo,
)

from databricks.labs.ucx.assessment.crawlers import (
    AZURE_SP_CONF_FAILURE_MSG,
    INCOMPATIBLE_SPARK_CONFIG_KEYS,
    INIT_SCRIPT_DBFS_PATH,
    INIT_SCRIPT_LOCAL_PATH,
    azure_sp_conf_present_check,
    spark_version_compatibility,
    is_mlr,
)
from databricks.labs.ucx.assessment.init_scripts import CheckInitScriptMixin
from databricks.labs.ucx.framework.crawlers import CrawlerBase
from databricks.labs.ucx.framework.owners import Ownership
from databricks.labs.ucx.framework.utils import escape_sql_identifier

logger = logging.getLogger(__name__)


@dataclass
class ClusterInfo:
    cluster_id: str
    success: int
    failures: str
    spark_version: str | None = None
    policy_id: str | None = None
    cluster_name: str | None = None
    creator: str | None = None
    """User-name of the creator of the cluster, if known."""

    __id_attributes__: ClassVar[tuple[str, ...]] = ("cluster_id",)

    @classmethod
    def from_cluster_details(cls, details: ClusterDetails):
        return ClusterInfo(
            cluster_id=details.cluster_id if details.cluster_id else "",
            cluster_name=details.cluster_name,
            policy_id=details.policy_id,
            spark_version=details.spark_version,
            creator=details.creator_user_name or None,
            success=1,
            failures="[]",
        )


class CheckClusterMixin(CheckInitScriptMixin):
    _ws: WorkspaceClient

    def _safe_get_cluster_policy(self, policy_id: str) -> Policy | None:
        try:
            return self._ws.cluster_policies.get(policy_id)
        except NotFound:
            logger.warning(f"The cluster policy was deleted: {policy_id}")
            return None

    def _check_cluster_policy(self, policy_id: str, source: str) -> list[str]:
        failures: list[str] = []
        policy = self._safe_get_cluster_policy(policy_id)
        if policy:
            if policy.definition:
                if azure_sp_conf_present_check(json.loads(policy.definition)):
                    failures.append(f"{AZURE_SP_CONF_FAILURE_MSG} {source}.")
            if policy.policy_family_definition_overrides:
                if azure_sp_conf_present_check(json.loads(policy.policy_family_definition_overrides)):
                    failures.append(f"{AZURE_SP_CONF_FAILURE_MSG} {source}.")
        return failures

    def _get_init_script_data(self, init_script_info: InitScriptInfo) -> str | None:
        try:
            match init_script_info:
                case InitScriptInfo(dbfs=DbfsStorageInfo(destination)):
                    split = destination.split(":")
                    if len(split) != INIT_SCRIPT_DBFS_PATH:
                        return None
                    data = self._ws.dbfs.read(split[1]).data
                    if data is not None:
                        return base64.b64decode(data).decode("utf-8")
                case InitScriptInfo(workspace=WorkspaceStorageInfo(workspace_file_destination)):
                    data = self._ws.workspace.export(workspace_file_destination).content
                    if data is not None:
                        return base64.b64decode(data).decode("utf-8")
                case InitScriptInfo(file=LocalFileInfo(destination)):
                    split = destination.split(":/")
                    if len(split) != INIT_SCRIPT_LOCAL_PATH:
                        return None
                    with open(split[1], "r", encoding="utf-8") as file:
                        data = file.read()
                    return data

            return None
        except (NotFound, BadRequest, FileNotFoundError, UnicodeDecodeError) as e:
            logger.warning(f"Error reading {init_script_info}: {e}")
            return None

    def _check_cluster_init_script(self, init_scripts: list[InitScriptInfo], source: str) -> list[str]:
        failures: list[str] = []
        for init_script_info in init_scripts:
            init_script_data = self._get_init_script_data(init_script_info)
            failures.extend(self.check_init_script(init_script_data, source))
        return failures

    def _check_spark_conf(self, conf: dict[str, str], source: str) -> list[str]:
        failures: list[str] = []
        for key, error in INCOMPATIBLE_SPARK_CONFIG_KEYS.items():
            if key in conf:
                failures.append(f"{error}: {key} in {source}.")
        for value in conf.values():
            if "dbfs:/mnt" in value or "/dbfs/mnt" in value:
                failures.append(f"using DBFS mount in configuration: {value}")
        # Checking if Azure cluster config is present in spark config
        if azure_sp_conf_present_check(conf):
            failures.append(f"{AZURE_SP_CONF_FAILURE_MSG} {source}.")
        return failures

    def _check_cluster_failures(self, cluster: ClusterDetails, source: str) -> list[str]:
        failures: list[str] = []

        unsupported_cluster_types = [
            DataSecurityMode.LEGACY_PASSTHROUGH,
            DataSecurityMode.LEGACY_SINGLE_USER,
            DataSecurityMode.LEGACY_TABLE_ACL,
        ]
        support_status = spark_version_compatibility(cluster.spark_version)
        if support_status != "supported":
            failures.append(f"not supported DBR: {cluster.spark_version}")
        if cluster.spark_conf is not None:
            failures.extend(self._check_spark_conf(cluster.spark_conf, source))
        # Checking if Azure cluster config is present in cluster policies
        if cluster.policy_id is not None:
            failures.extend(self._check_cluster_policy(cluster.policy_id, source))
        if cluster.init_scripts is not None:
            failures.extend(self._check_cluster_init_script(cluster.init_scripts, source))
        if cluster.data_security_mode == DataSecurityMode.NONE:
            failures.append("No isolation shared clusters not supported in UC")
        if cluster.data_security_mode in unsupported_cluster_types:
            failures.append(f"cluster type not supported : {cluster.data_security_mode.value}")
        if cluster.data_security_mode == DataSecurityMode.NONE and is_mlr(cluster.spark_version):
            failures.append("Shared Machine Learning Runtime clusters are not supported in UC")

        return failures


class ClustersCrawler(CrawlerBase[ClusterInfo], CheckClusterMixin):
    def __init__(self, ws: WorkspaceClient, sql_backend: SqlBackend, schema: str):
        super().__init__(sql_backend, "hive_metastore", schema, "clusters", ClusterInfo)
        self._ws = ws

    def _crawl(self) -> Iterable[ClusterInfo]:
        all_clusters = list(self._ws.clusters.list())
        return list(self._assess_clusters(all_clusters))

    def _assess_clusters(self, all_clusters: Iterable[ClusterDetails]):
        for cluster in all_clusters:
            if cluster.cluster_source == ClusterSource.JOB:
                continue
            creator = cluster.creator_user_name or None
            if not creator:
                logger.warning(
                    f"Cluster {cluster.cluster_id} have Unknown creator, it means that the original creator "
                    f"has been deleted and should be re-created"
                )
            cluster_info = ClusterInfo.from_cluster_details(cluster)
            failures = self._check_cluster_failures(cluster, "cluster")
            if len(failures) > 0:
                cluster_info.success = 0
                cluster_info.failures = json.dumps(failures)
            yield cluster_info

    def _try_fetch(self) -> Iterable[ClusterInfo]:
        for row in self._fetch(f"SELECT * FROM {escape_sql_identifier(self.full_name)}"):
            yield ClusterInfo(*row)


class ClusterOwnership(Ownership[ClusterInfo]):
    """Determine ownership of clusters in the inventory.

    This is the cluster creator (if known).
    """

    def _maybe_direct_owner(self, record: ClusterInfo) -> str | None:
        return record.creator


@dataclass
class PolicyInfo:
    policy_id: str
    policy_name: str
    success: int
    failures: str
    spark_version: str | None = None
    policy_description: str | None = None
    creator: str | None = None
    """User-name of the creator of the cluster policy, if known."""

    __id_attributes__: ClassVar[tuple[str, ...]] = ("policy_id",)


class PoliciesCrawler(CrawlerBase[PolicyInfo], CheckClusterMixin):
    def __init__(self, ws: WorkspaceClient, sql_backend: SqlBackend, schema):
        super().__init__(sql_backend, "hive_metastore", schema, "policies", PolicyInfo)
        self._ws = ws

    def _crawl(self) -> Iterable[PolicyInfo]:
        all_policies = list(self._ws.cluster_policies.list())
        return list(self._assess_policies(all_policies))

    def _assess_policies(self, all_policies: Iterable[Policy]) -> Iterable[PolicyInfo]:
        for policy in all_policies:
            failures: list[str] = []
            if policy.policy_id is None:
                continue
            failures.extend(self._check_cluster_policy(policy.policy_id, "policy"))
            spark_version = None
            if policy.definition is not None:
                try:
                    spark_version = json.dumps(json.loads(policy.definition)["spark_version"])
                except KeyError:
                    pass
            policy_name = policy.name or "UNDEFINED"
            creator_name = policy.creator_user_name or None

            policy_info = PolicyInfo(
                policy_id=policy.policy_id,
                policy_description=policy.description,
                policy_name=policy_name,
                spark_version=spark_version,
                success=1,
                failures="[]",
                creator=creator_name,
            )
            if len(failures) > 0:
                policy_info.success = 0
                policy_info.failures = json.dumps(failures)
            yield policy_info

    def _try_fetch(self) -> Iterable[PolicyInfo]:
        for row in self._fetch(f"SELECT * FROM {escape_sql_identifier(self.full_name)}"):
            yield PolicyInfo(*row)


class ClusterPolicyOwnership(Ownership[PolicyInfo]):
    """Determine ownership of cluster policies in the inventory.

    This is the creator of the cluster policy (if known).
    """

    def _maybe_direct_owner(self, record: PolicyInfo) -> str | None:
        return record.creator
