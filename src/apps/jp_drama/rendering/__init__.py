"""Resumable mock and live rendering for Japanese short dramas."""

from .approval import (
    APPROVAL_SCHEMA_VERSION,
    ApprovalError,
    ApprovedKeyframeManifest,
    create_approval_manifest,
    load_and_verify_approval,
)
from .canary import select_canary_shot
from .canary_tasks import ProviderCallLimitError, Wan27LiveTaskExecutor
from .engine import (
    RenderExecutionError,
    RenderGraphRunner,
    RenderStateConflictError,
    RenderTaskFailedError,
    RenderValidationError,
)
from .execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    DelegatedTaskPlan,
    ExecutionPlan,
    ExecutionTaskPlan,
    ProviderExecutionPlanner,
    ProviderPlanningError,
    ProviderProfile,
)
from .live_tasks import LiveTaskExecutor as LegacyLiveTaskExecutor
from .mock_tasks import MockTaskExecutor, TaskContext
from .models import (
    RENDER_STATE_SCHEMA_VERSION,
    RenderRunState,
    RenderValidationReport,
    ShotExecutionState,
    TaskExecutionState,
)
from .provider_config import (
    LIVE_PROVIDER_SCHEMA_VERSION,
    DashScopeProviderConfig,
    LiveProviderConfig,
    ProviderConfigurationError,
)
from .provider_core import (
    PROVIDER_CORE_SCHEMA_VERSION,
    CostEstimate,
    DialogueLine,
    Money,
    PreparedProviderRequest,
    ProviderAdapter,
    ProviderArtifact,
    ProviderArtifactSet,
    ProviderCapabilities,
    ProviderCapabilitiesRequired,
    ProviderCoreError,
    ProviderDescriptor,
    ProviderPollResult,
    ProviderSubmission,
    ReferenceAsset,
    ShotGenerationSpec,
    ValidationIssue,
    ValidationReport,
)
from .provider_ledger import (
    CANARY_LEDGER_SCHEMA_VERSION,
    CanaryProviderLedger,
    CanaryProviderLedgerStore,
    ProviderLedgerError,
    ProviderOperationRecord,
)
from .provider_registry import (
    MockProviderAdapter,
    ProviderRegistry,
    ProviderRegistryError,
    SeedancePlatformAdapter,
    Wan27ImagePlanningAdapter,
    Wan27PlanningAdapter,
    build_default_provider_registry,
)
from .wan_master_tasks import WanMasterReferenceLiveTaskExecutor

# PR8 makes the official Wan 2.7 compatibility executor the public legacy live path.
# Production first-frame generation should use WanMasterReferenceLiveTaskExecutor.
LiveTaskExecutor = Wan27LiveTaskExecutor

__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "CANARY_LEDGER_SCHEMA_VERSION",
    "EXECUTION_PLAN_SCHEMA_VERSION",
    "LIVE_PROVIDER_SCHEMA_VERSION",
    "PROVIDER_CORE_SCHEMA_VERSION",
    "RENDER_STATE_SCHEMA_VERSION",
    "ApprovalError",
    "ApprovedKeyframeManifest",
    "CanaryProviderLedger",
    "CanaryProviderLedgerStore",
    "CostEstimate",
    "DashScopeProviderConfig",
    "DelegatedTaskPlan",
    "DialogueLine",
    "ExecutionPlan",
    "ExecutionTaskPlan",
    "LegacyLiveTaskExecutor",
    "LiveProviderConfig",
    "LiveTaskExecutor",
    "MockProviderAdapter",
    "MockTaskExecutor",
    "Money",
    "PreparedProviderRequest",
    "ProviderAdapter",
    "ProviderArtifact",
    "ProviderArtifactSet",
    "ProviderCallLimitError",
    "ProviderCapabilities",
    "ProviderCapabilitiesRequired",
    "ProviderConfigurationError",
    "ProviderCoreError",
    "ProviderDescriptor",
    "ProviderExecutionPlanner",
    "ProviderLedgerError",
    "ProviderOperationRecord",
    "ProviderPlanningError",
    "ProviderPollResult",
    "ProviderProfile",
    "ProviderRegistry",
    "ProviderRegistryError",
    "ProviderSubmission",
    "ReferenceAsset",
    "RenderExecutionError",
    "RenderGraphRunner",
    "RenderRunState",
    "RenderStateConflictError",
    "RenderTaskFailedError",
    "RenderValidationError",
    "RenderValidationReport",
    "SeedancePlatformAdapter",
    "ShotExecutionState",
    "ShotGenerationSpec",
    "TaskContext",
    "TaskExecutionState",
    "ValidationIssue",
    "ValidationReport",
    "Wan27ImagePlanningAdapter",
    "Wan27LiveTaskExecutor",
    "Wan27PlanningAdapter",
    "WanMasterReferenceLiveTaskExecutor",
    "build_default_provider_registry",
    "create_approval_manifest",
    "load_and_verify_approval",
    "select_canary_shot",
]
