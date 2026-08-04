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
from .provider_ledger import (
    CANARY_LEDGER_SCHEMA_VERSION,
    CanaryProviderLedger,
    CanaryProviderLedgerStore,
    ProviderLedgerError,
    ProviderOperationRecord,
)

# PR8 makes the official Wan 2.7 compatibility executor the public live path.
LiveTaskExecutor = Wan27LiveTaskExecutor

__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "CANARY_LEDGER_SCHEMA_VERSION",
    "LIVE_PROVIDER_SCHEMA_VERSION",
    "RENDER_STATE_SCHEMA_VERSION",
    "ApprovalError",
    "ApprovedKeyframeManifest",
    "CanaryProviderLedger",
    "CanaryProviderLedgerStore",
    "DashScopeProviderConfig",
    "LegacyLiveTaskExecutor",
    "LiveProviderConfig",
    "LiveTaskExecutor",
    "MockTaskExecutor",
    "ProviderCallLimitError",
    "ProviderConfigurationError",
    "ProviderLedgerError",
    "ProviderOperationRecord",
    "RenderExecutionError",
    "RenderGraphRunner",
    "RenderRunState",
    "RenderStateConflictError",
    "RenderTaskFailedError",
    "RenderValidationError",
    "RenderValidationReport",
    "ShotExecutionState",
    "TaskContext",
    "TaskExecutionState",
    "Wan27LiveTaskExecutor",
    "create_approval_manifest",
    "load_and_verify_approval",
    "select_canary_shot",
]
