import abc
import logging
import sys
from collections.abc import Callable, Iterable
from datetime import timedelta
from functools import cached_property
from pathlib import Path

from databricks.labs.blueprint.installation import Installation
from databricks.labs.blueprint.installer import InstallState
from databricks.labs.blueprint.tui import Prompts
from databricks.labs.blueprint.wheels import ProductInfo, WheelsV2
from databricks.labs.lsql.backends import SqlBackend

from databricks.labs.ucx.assessment.dashboards import DashboardOwnership
from databricks.labs.ucx.assessment.jobs import JobsCrawler
from databricks.labs.ucx.assessment.pipelines import PipelinesCrawler
from databricks.labs.ucx.hive_metastore.pipelines_migrate import PipelinesMigrator
from databricks.labs.ucx.recon.data_comparator import StandardDataComparator
from databricks.labs.ucx.recon.data_profiler import StandardDataProfiler
from databricks.labs.ucx.recon.metadata_retriever import DatabricksTableMetadataRetriever
from databricks.labs.ucx.recon.migration_recon import MigrationRecon
from databricks.labs.ucx.recon.schema_comparator import StandardSchemaComparator
from databricks.labs.ucx.source_code.directfs_access import DirectFsAccessCrawler, DirectFsAccessOwnership
from databricks.labs.ucx.source_code.python_libraries import PythonLibraryResolver
from databricks.labs.ucx.source_code.used_table import UsedTablesCrawler
from databricks.sdk import AccountClient, WorkspaceClient, core
from databricks.sdk.service import sql

from databricks.labs.ucx.account.workspaces import WorkspaceInfo
from databricks.labs.ucx.assessment.azure import AzureServicePrincipalCrawler
from databricks.labs.ucx.assessment.dashboards import LakeviewDashboardCrawler, RedashDashboardCrawler
from databricks.labs.ucx.assessment.export import AssessmentExporter
from databricks.labs.ucx.aws.credentials import CredentialManager
from databricks.labs.ucx.config import WorkspaceConfig
from databricks.labs.ucx.framework.owners import AdministratorLocator, WorkspacePathOwnership, LegacyQueryOwnership
from databricks.labs.ucx.hive_metastore import ExternalLocations, MountsCrawler, TablesCrawler
from databricks.labs.ucx.hive_metastore.catalog_schema import CatalogSchema
from databricks.labs.ucx.hive_metastore.grants import (
    ACLMigrator,
    AwsACL,
    AzureACL,
    ComputeLocations,
    Grant,
    GrantsCrawler,
    GrantOwnership,
    MigrateGrants,
    PrincipalACL,
)
from databricks.labs.ucx.hive_metastore.mapping import TableMapping
from databricks.labs.ucx.hive_metastore.table_migration_status import TableMigrationIndex
from databricks.labs.ucx.hive_metastore.ownership import (
    TableMigrationOwnership,
    TableOwnership,
    TableOwnershipGrantLoader,
    DefaultSecurableOwnership,
)
from databricks.labs.ucx.hive_metastore.table_migrate import (
    TableMigrationStatusRefresher,
    TablesMigrator,
)
from databricks.labs.ucx.hive_metastore.table_move import TableMove
from databricks.labs.ucx.hive_metastore.udfs import UdfsCrawler, UdfOwnership
from databricks.labs.ucx.hive_metastore.verification import VerifyHasCatalog, VerifyHasMetastore
from databricks.labs.ucx.installer.workflows import DeployedWorkflows
from databricks.labs.ucx.progress.install import VerifyProgressTracking
from databricks.labs.ucx.source_code.graph import DependencyResolver
from databricks.labs.ucx.source_code.linters.jobs import WorkflowLinter
from databricks.labs.ucx.source_code.known import KnownList
from databricks.labs.ucx.source_code.folders import FolderLoader
from databricks.labs.ucx.source_code.files import FileLoader, ImportFileResolver
from databricks.labs.ucx.source_code.notebooks.loaders import (
    NotebookResolver,
    NotebookLoader,
)
from databricks.labs.ucx.source_code.path_lookup import PathLookup
from databricks.labs.ucx.source_code.linters.queries import QueryLinter
from databricks.labs.ucx.source_code.linters.redash import Redash
from databricks.labs.ucx.workspace_access import generic, redash
from databricks.labs.ucx.workspace_access.groups import GroupManager
from databricks.labs.ucx.workspace_access.manager import PermissionManager
from databricks.labs.ucx.workspace_access.scim import ScimSupport
from databricks.labs.ucx.workspace_access.secrets import SecretScopesSupport
from databricks.labs.ucx.workspace_access.tacl import TableAclSupport

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

# "Service Factories" would always have a lot of public methods.
# This is because they are responsible for creating objects that are
# used throughout the application. That being said, we'll do best
# effort of splitting the instances between Global, Runtime,
# Workspace CLI, and Account CLI contexts.
# pylint: disable=too-many-public-methods

logger = logging.getLogger(__name__)


class GlobalContext(abc.ABC):
    def __init__(self, named_parameters: dict[str, str] | None = None):
        if not named_parameters:
            named_parameters = {}
        self._named_parameters = named_parameters

    def replace(self, **kwargs) -> Self:
        """Replace cached properties for unit testing purposes."""
        for key, value in kwargs.items():
            self.__dict__[key] = value
        return self

    @cached_property
    def workspace_client(self) -> WorkspaceClient:
        raise ValueError("Workspace client not set")

    @cached_property
    def sql_backend(self) -> SqlBackend:
        raise ValueError("SQL backend not set")

    @cached_property
    def account_client(self) -> AccountClient:
        raise ValueError("Account client not set")

    @cached_property
    def named_parameters(self) -> dict[str, str]:
        return self._named_parameters

    @cached_property
    def product_info(self) -> ProductInfo:
        return ProductInfo.from_class(WorkspaceConfig)

    @cached_property
    def installation(self) -> Installation:
        return Installation.current(self.workspace_client, self.product_info.product_name())

    @cached_property
    def config(self) -> WorkspaceConfig:
        return self.installation.load(WorkspaceConfig)

    @cached_property
    def connect_config(self) -> core.Config:
        return self.workspace_client.config

    @cached_property
    def is_azure(self) -> bool:
        return self.connect_config.is_azure

    @cached_property
    def is_aws(self) -> bool:
        return self.connect_config.is_aws

    @cached_property
    def is_gcp(self) -> bool:
        return not self.is_aws and not self.is_azure

    @cached_property
    def inventory_database(self) -> str:
        return self.config.inventory_database

    @cached_property
    def workspace_listing(self) -> generic.WorkspaceListing:
        return generic.WorkspaceListing(
            self.workspace_client,
            self.sql_backend,
            self.inventory_database,
            self.config.num_threads,
            self.config.workspace_start_path,
        )

    @cached_property
    def generic_permissions_support(self) -> generic.GenericPermissionsSupport:
        models_listing = generic.models_listing(self.workspace_client, self.config.num_threads)
        acl_listing = [
            generic.Listing(self.workspace_client.clusters.list, "cluster_id", "clusters"),
            generic.Listing(self.workspace_client.cluster_policies.list, "policy_id", "cluster-policies"),
            generic.Listing(self.workspace_client.instance_pools.list, "instance_pool_id", "instance-pools"),
            generic.Listing(self.workspace_client.warehouses.list, "id", "sql/warehouses"),
            generic.Listing(self.workspace_client.jobs.list, "job_id", "jobs"),
            generic.Listing(self.workspace_client.pipelines.list_pipelines, "pipeline_id", "pipelines"),
            generic.Listing(self.workspace_client.serving_endpoints.list, "id", "serving-endpoints"),
            generic.Listing(generic.experiments_listing(self.workspace_client), "experiment_id", "experiments"),
            generic.Listing(models_listing, "id", "registered-models"),
            generic.Listing(generic.models_root_page, "object_id", "registered-models"),
            generic.Listing(generic.tokens_and_passwords, "object_id", "authorization"),
            generic.Listing(generic.feature_store_listing(self.workspace_client), "object_id", "feature-tables"),
            generic.Listing(generic.feature_tables_root_page, "object_id", "feature-tables"),
            self.workspace_listing,
        ]
        return generic.GenericPermissionsSupport(
            self.workspace_client,
            acl_listing,
            include_object_permissions=self.config.include_object_permissions,
        )

    @cached_property
    def redash_permissions_support(self) -> redash.RedashPermissionsSupport:
        acl_listing = [
            redash.Listing(self.workspace_client.alerts.list, sql.ObjectTypePlural.ALERTS),
            redash.Listing(self.workspace_client.dashboards.list, sql.ObjectTypePlural.DASHBOARDS),
            redash.Listing(self.workspace_client.queries.list, sql.ObjectTypePlural.QUERIES),
        ]
        return redash.RedashPermissionsSupport(
            self.workspace_client,
            acl_listing,
            include_object_permissions=self.config.include_object_permissions,
        )

    @cached_property
    def scim_entitlements_support(self) -> ScimSupport:
        return ScimSupport(self.workspace_client, include_object_permissions=self.config.include_object_permissions)

    @cached_property
    def secret_scope_acl_support(self) -> SecretScopesSupport:
        return SecretScopesSupport(
            self.workspace_client, include_object_permissions=self.config.include_object_permissions
        )

    @cached_property
    def legacy_table_acl_support(self) -> TableAclSupport:
        return TableAclSupport(
            self.grants_crawler,
            self.sql_backend,
            include_object_permissions=self.config.include_object_permissions,
        )

    @cached_property
    def permission_manager(self) -> PermissionManager:
        return PermissionManager(
            self.sql_backend,
            self.inventory_database,
            [
                self.generic_permissions_support,
                self.redash_permissions_support,
                self.secret_scope_acl_support,
                self.scim_entitlements_support,
                self.legacy_table_acl_support,
            ],
        )

    @cached_property
    def group_manager(self) -> GroupManager:
        return GroupManager(
            self.sql_backend,
            self.workspace_client,
            self.inventory_database,
            self.config.include_group_names,
            self.config.renamed_group_prefix,
            workspace_group_regex=self.config.workspace_group_regex,
            workspace_group_replace=self.config.workspace_group_replace,
            account_group_regex=self.config.account_group_regex,
            external_id_match=self.config.group_match_by_external_id,
        )

    @cached_property
    def grants_crawler(self) -> GrantsCrawler:
        return GrantsCrawler(self.tables_crawler, self.udfs_crawler, self.config.include_databases)

    @cached_property
    def grant_ownership(self) -> GrantOwnership:
        return GrantOwnership(self.administrator_locator)

    @cached_property
    def udfs_crawler(self) -> UdfsCrawler:
        return UdfsCrawler(self.sql_backend, self.inventory_database, self.config.include_databases)

    @cached_property
    def udf_ownership(self) -> UdfOwnership:
        return UdfOwnership(self.administrator_locator)

    @cached_property
    def dashboard_ownership(self) -> DashboardOwnership:
        return DashboardOwnership(self.administrator_locator, self.workspace_client, self.workspace_path_ownership)

    @cached_property
    def tables_crawler(self) -> TablesCrawler:
        return TablesCrawler(self.sql_backend, self.inventory_database, self.config.include_databases)

    @cached_property
    def jobs_crawler(self) -> JobsCrawler:
        return JobsCrawler(
            self.workspace_client,
            self.sql_backend,
            self.inventory_database,
            include_job_ids=self.config.include_job_ids,
            exclude_job_ids=[int(job_id) for job_id in self.install_state.jobs.values()],
        )

    @cached_property
    def table_ownership(self) -> TableOwnership:
        return TableOwnership(
            self.administrator_locator,
            self.grants_crawler,
            self.used_tables_crawler_for_paths,
            self.used_tables_crawler_for_queries,
            self.legacy_query_ownership,
            self.workspace_path_ownership,
        )

    @cached_property
    def redash_crawler(self) -> RedashDashboardCrawler:
        return RedashDashboardCrawler(
            self.workspace_client,
            self.sql_backend,
            self.inventory_database,
            include_dashboard_ids=self.config.include_dashboard_ids,
            include_query_ids=self.config.include_query_ids,
            debug_listing_upper_limit=self.config.debug_listing_upper_limit,
        )

    @cached_property
    def lakeview_crawler(self) -> LakeviewDashboardCrawler:
        return LakeviewDashboardCrawler(
            self.workspace_client,
            self.sql_backend,
            self.inventory_database,
            include_dashboard_ids=self.config.include_dashboard_ids,
            exclude_dashboard_ids=list(self.install_state.dashboards.values()),
            include_query_ids=self.config.include_query_ids,
        )

    @cached_property
    def default_securable_ownership(self) -> DefaultSecurableOwnership:
        # validate that the default_owner_group is set and is a valid group (the current user is a member)

        return DefaultSecurableOwnership(
            self.administrator_locator,
            self.tables_crawler,
            self.group_manager,
            self.config.default_owner_group,
            lambda: self.workspace_client.current_user.me().user_name,
        )

    @cached_property
    def workspace_path_ownership(self) -> WorkspacePathOwnership:
        return WorkspacePathOwnership(self.administrator_locator, self.workspace_client)

    @cached_property
    def legacy_query_ownership(self) -> LegacyQueryOwnership:
        return LegacyQueryOwnership(self.administrator_locator, self.workspace_client)

    @cached_property
    def directfs_access_ownership(self) -> DirectFsAccessOwnership:
        return DirectFsAccessOwnership(
            self.administrator_locator,
            self.workspace_path_ownership,
            self.legacy_query_ownership,
            self.workspace_client,
        )

    @cached_property
    def tables_migrator(self) -> TablesMigrator:
        return TablesMigrator(
            self.tables_crawler,
            self.workspace_client,
            self.sql_backend,
            self.table_mapping,
            self.migration_status_refresher,
            self.migrate_grants,
            self.external_locations,
        )

    @cached_property
    def assessment_exporter(self):
        return AssessmentExporter(self.sql_backend, self.config)

    @cached_property
    def acl_migrator(self):
        return ACLMigrator(
            self.tables_crawler,
            self.workspace_info,
            self.migration_status_refresher,
            self.migrate_grants,
            self.sql_backend,
            self.config.inventory_database,
        )

    @cached_property
    def table_ownership_grant_loader(self) -> TableOwnershipGrantLoader:
        return TableOwnershipGrantLoader(self.tables_crawler, self.default_securable_ownership)

    @cached_property
    def pipelines_migrator(self) -> PipelinesMigrator:
        include_pipeline_ids = (
            self.named_parameters.get('include_pipeline_ids', '').split(',')
            if 'include_pipeline_ids' in self.named_parameters
            else None
        )
        exclude_pipeline_ids = (
            self.named_parameters.get('exclude_pipeline_ids', '').split(',')
            if 'exclude_pipeline_ids' in self.named_parameters
            else None
        )
        return PipelinesMigrator(
            self.workspace_client,
            self.pipelines_crawler,
            self.jobs_crawler,
            self.config.ucx_catalog,
            include_pipeline_ids=include_pipeline_ids,
            exclude_pipeline_ids=exclude_pipeline_ids,
        )

    @cached_property
    def migrate_grants(self) -> MigrateGrants:
        # owner grants have to come first
        grant_loaders: list[Callable[[], Iterable[Grant]]] = [
            self.default_securable_ownership.load,
            self.grants_crawler.snapshot,
            self.principal_acl.get_interactive_cluster_grants,
        ]
        return MigrateGrants(
            self.sql_backend,
            self.group_manager,
            grant_loaders,
            skip_tacl_migration=self.config.skip_tacl_migration,
        )

    @cached_property
    def table_move(self) -> TableMove:
        return TableMove(self.workspace_client, self.sql_backend)

    @cached_property
    def mounts_crawler(self) -> MountsCrawler:
        return MountsCrawler(
            self.sql_backend,
            self.workspace_client,
            self.inventory_database,
        )

    @cached_property
    def pipelines_crawler(self) -> PipelinesCrawler:
        return PipelinesCrawler(self.workspace_client, self.sql_backend, self.inventory_database)

    @cached_property
    def azure_service_principal_crawler(self) -> AzureServicePrincipalCrawler:
        return AzureServicePrincipalCrawler(self.workspace_client, self.sql_backend, self.inventory_database)

    @cached_property
    def external_locations(self) -> ExternalLocations:
        return ExternalLocations(
            self.workspace_client,
            self.sql_backend,
            self.inventory_database,
            self.tables_crawler,
            self.mounts_crawler,
            enable_hms_federation=self.config.enable_hms_federation,
        )

    @cached_property
    def azure_acl(self) -> AzureACL:
        return AzureACL(
            self.workspace_client,
            self.sql_backend,
            self.azure_service_principal_crawler,
            self.installation,
        )

    @cached_property
    def aws_acl(self) -> AwsACL:
        return AwsACL(
            self.workspace_client,
            self.sql_backend,
            self.installation,
        )

    @cached_property
    def principal_locations_retriever(self) -> Callable[[], list[ComputeLocations]]:
        def inner():
            if self.is_azure:
                return self.azure_acl.get_eligible_locations_principals()
            if self.is_aws:
                return self.aws_acl.get_eligible_locations_principals()
            raise NotImplementedError("Not implemented for GCP.")

        return inner

    @cached_property
    def principal_acl(self) -> PrincipalACL:
        return PrincipalACL(
            self.workspace_client,
            self.sql_backend,
            self.installation,
            self.tables_crawler,
            self.external_locations,
            self.principal_locations_retriever,
        )

    @cached_property
    def migration_status_refresher(self) -> TableMigrationStatusRefresher:
        return TableMigrationStatusRefresher(
            self.workspace_client,
            self.sql_backend,
            self.inventory_database,
            self.tables_crawler,
        )

    @cached_property
    def table_migration_ownership(self) -> TableMigrationOwnership:
        return TableMigrationOwnership(self.tables_crawler, self.table_ownership)

    @cached_property
    def iam_credential_manager(self) -> CredentialManager:
        return CredentialManager(self.workspace_client)

    @cached_property
    def table_mapping(self) -> TableMapping:
        return TableMapping(self.installation, self.workspace_client, self.sql_backend)

    @cached_property
    def catalog_schema(self) -> CatalogSchema:
        return CatalogSchema(self.workspace_client, self.table_mapping, self.migrate_grants, self.config.ucx_catalog)

    @cached_property
    def verify_timeout(self) -> timedelta:
        return timedelta(minutes=2)

    @cached_property
    def wheels(self) -> WheelsV2:
        return WheelsV2(self.installation, self.product_info)

    @cached_property
    def install_state(self) -> InstallState:
        return InstallState.from_installation(self.installation)

    @cached_property
    def deployed_workflows(self) -> DeployedWorkflows:
        return DeployedWorkflows(self.workspace_client, self.install_state)

    @cached_property
    def workspace_info(self) -> WorkspaceInfo:
        return WorkspaceInfo(self.installation, self.workspace_client)

    @cached_property
    def verify_has_metastore(self) -> VerifyHasMetastore:
        return VerifyHasMetastore(self.workspace_client)

    @cached_property
    def verify_has_ucx_catalog(self) -> VerifyHasCatalog:
        return VerifyHasCatalog(self.workspace_client, self.config.ucx_catalog)

    @cached_property
    def verify_progress_tracking(self) -> VerifyProgressTracking:
        return VerifyProgressTracking(self.verify_has_metastore, self.verify_has_ucx_catalog, self.deployed_workflows)

    @cached_property
    def pip_resolver(self) -> PythonLibraryResolver:
        return PythonLibraryResolver(allow_list=self.allow_list)

    @cached_property
    def notebook_loader(self) -> NotebookLoader:
        return NotebookLoader()

    @cached_property
    def notebook_resolver(self) -> NotebookResolver:
        return NotebookResolver(self.notebook_loader)

    @cached_property
    def site_packages_path(self) -> Path:
        lookup = self.path_lookup
        return next(path for path in lookup.library_roots if "site-packages" in path.as_posix())

    @cached_property
    def path_lookup(self) -> PathLookup:
        # TODO find a solution to enable a different cwd per job/task (maybe it's not necessary or possible?)
        return PathLookup.from_sys_path(Path.cwd())

    @cached_property
    def file_loader(self) -> FileLoader:
        return FileLoader()

    @cached_property
    def folder_loader(self) -> FolderLoader:
        return FolderLoader(self.notebook_loader, self.file_loader)

    @cached_property
    def allow_list(self) -> KnownList:
        return KnownList()

    @cached_property
    def file_resolver(self) -> ImportFileResolver:
        return ImportFileResolver(self.file_loader, allow_list=self.allow_list)

    @cached_property
    def dependency_resolver(self) -> DependencyResolver:
        return DependencyResolver(
            self.pip_resolver, self.notebook_resolver, self.file_resolver, self.file_resolver, self.path_lookup
        )

    @cached_property
    def workflow_linter(self) -> WorkflowLinter:
        return WorkflowLinter(
            self.workspace_client,
            self.jobs_crawler,
            self.dependency_resolver,
            self.path_lookup,
            TableMigrationIndex([]),  # TODO: bring back self.tables_migrator.index()
            self.directfs_access_crawler_for_paths,
            self.used_tables_crawler_for_paths,
        )

    @cached_property
    def query_linter(self) -> QueryLinter:
        return QueryLinter(
            self.sql_backend,
            self.inventory_database,
            TableMigrationIndex([]),
            self.directfs_access_crawler_for_queries,
            self.used_tables_crawler_for_queries,
            [self.redash_crawler, self.lakeview_crawler],
            self.config.debug_listing_upper_limit,
        )

    @cached_property
    def directfs_access_crawler_for_paths(self) -> DirectFsAccessCrawler:
        return DirectFsAccessCrawler.for_paths(self.sql_backend, self.inventory_database)

    @cached_property
    def directfs_access_crawler_for_queries(self) -> DirectFsAccessCrawler:
        return DirectFsAccessCrawler.for_queries(self.sql_backend, self.inventory_database)

    @cached_property
    def used_tables_crawler_for_paths(self):
        return UsedTablesCrawler.for_paths(self.sql_backend, self.inventory_database)

    @cached_property
    def used_tables_crawler_for_queries(self):
        return UsedTablesCrawler.for_queries(self.sql_backend, self.inventory_database)

    @cached_property
    def redash(self) -> Redash:
        return Redash(
            self.migration_status_refresher.index(),
            self.workspace_client,
            self.installation,
            self.redash_crawler,
        )

    @cached_property
    def metadata_retriever(self) -> DatabricksTableMetadataRetriever:
        return DatabricksTableMetadataRetriever(self.sql_backend)

    @cached_property
    def schema_comparator(self) -> StandardSchemaComparator:
        return StandardSchemaComparator(self.metadata_retriever)

    @cached_property
    def data_profiler(self) -> StandardDataProfiler:
        return StandardDataProfiler(self.sql_backend, self.metadata_retriever)

    @cached_property
    def data_comparator(self) -> StandardDataComparator:
        return StandardDataComparator(self.sql_backend, self.data_profiler)

    @cached_property
    def migration_recon(self) -> MigrationRecon:
        return MigrationRecon(
            self.sql_backend,
            self.inventory_database,
            self.migration_status_refresher,
            self.table_mapping,
            self.schema_comparator,
            self.data_comparator,
            self.config.recon_tolerance_percent,
        )

    @cached_property
    def administrator_locator(self) -> AdministratorLocator:
        return AdministratorLocator(self.workspace_client)


class CliContext(GlobalContext, abc.ABC):
    @cached_property
    def prompts(self) -> Prompts:
        return Prompts()
