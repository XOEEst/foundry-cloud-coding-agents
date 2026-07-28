from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_opt.onboarding import (
    FoundryAgentDiscovery,
    OnboardingRequest,
    PythonAgentCandidate,
)
from foundry_opt.onboarding.production import (
    DraftProbeUnavailable,
    DraftIntegrationComponents,
    SourceBundleDraftProbe,
    build_production_onboarding_dependencies,
    build_source_bundle_draft_probe,
)


class FakeGateway:
    def __init__(self) -> None:
        self.created = []
        self.records = []
        self.deleted = []

    def create_draft(self, request):
        self.created.append(request)
        record = SimpleNamespace(
            agent_name=request.agent_name,
            version_id="draft-onboarding-probe",
        )
        self.records.append(record)
        return record

    def delete_probe(self, record) -> None:
        self.deleted.append(record)


def test_draft_probe_factory_adapts_milestone_three_gateway(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    bundle = SimpleNamespace(
        path=tmp_path / "probe.zip",
        sha256="a" * 64,
        byte_size=1,
    )
    bundle_requests = []

    def build_bundle(request):
        bundle_requests.append(request)
        return bundle

    components = DraftIntegrationComponents(
        gateway=gateway,
        build_bundle=build_bundle,
        bundle_request_type=SimpleNamespace,
        draft_request_type=SimpleNamespace,
        scratch_path_factory=lambda request: tmp_path / "probe.zip",
    )
    probe = build_source_bundle_draft_probe(components)
    request = OnboardingRequest(
        repository_root=tmp_path,
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant",
        client_id="client",
        subscription_id="subscription",
        product_install="foundry-cloud-coding-agent==0.1.0",
    )

    result = probe.probe(
        request,
        FoundryAgentDiscovery("support-agent", ("7",)),
        PythonAgentCandidate(
            "support-agent",
            Path("src/support_agent"),
            Path("src/support_agent/main.py"),
        ),
    )
    probe.delete_probe(result.agent_name, result.version)

    assert isinstance(probe, SourceBundleDraftProbe)
    assert result.version == "draft-onboarding-probe"
    assert gateway.created[0].probe is True
    assert gateway.created[0].base_version == 7
    assert gateway.created[0].entry_point == (
        "python",
        "src/support_agent/main.py",
    )
    assert gateway.created[0].bundle is bundle
    # The probe omits runtime/dependency/CPU/memory/protocol so the draft
    # inherits the published baseline rather than guessing values.
    for field in (
        "runtime",
        "dependency_resolution",
        "cpu",
        "memory",
        "protocol",
        "protocol_version",
    ):
        assert not hasattr(gateway.created[0], field)
    assert bundle_requests[0].include == ("src/support_agent/**",)
    assert gateway.deleted == gateway.records


def test_production_assembly_uses_available_draft_probe_factory() -> None:
    functional_probe = build_source_bundle_draft_probe(
        DraftIntegrationComponents(
            gateway=FakeGateway(),
            build_bundle=lambda request: None,
            bundle_request_type=SimpleNamespace,
            draft_request_type=SimpleNamespace,
        )
    )

    dependencies = build_production_onboarding_dependencies(
        draft_probe_factory=lambda: functional_probe,
    )

    assert dependencies.draft_probe is functional_probe
    assert isinstance(dependencies.draft_probe, SourceBundleDraftProbe)


def test_draft_probe_deletes_created_probe_when_bundle_cleanup_fails(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    output = tmp_path / "probe.zip"
    output.mkdir()
    bundle = SimpleNamespace(
        path=output,
        sha256="a" * 64,
        byte_size=1,
    )
    components = DraftIntegrationComponents(
        gateway=gateway,
        build_bundle=lambda request: bundle,
        bundle_request_type=SimpleNamespace,
        draft_request_type=SimpleNamespace,
        scratch_path_factory=lambda request: output,
    )
    probe = build_source_bundle_draft_probe(components)
    request = OnboardingRequest(
        repository_root=tmp_path,
        environment_name="acceptance",
        target_name="support-agent",
        project_endpoint=(
            "https://example.services.ai.azure.com/api/projects/demo"
        ),
        project_resource_id="/subscriptions/sub/projects/demo",
        tenant_id="tenant",
        client_id="client",
        subscription_id="subscription",
        product_install="foundry-cloud-coding-agent==0.1.0",
    )

    with pytest.raises(DraftProbeUnavailable, match="bundle cleanup failed"):
        probe.probe(
            request,
            FoundryAgentDiscovery("support-agent", ("7",)),
            PythonAgentCandidate(
                "support-agent",
                Path("agent"),
                Path("agent/main.py"),
            ),
        )

    assert gateway.deleted == gateway.records
