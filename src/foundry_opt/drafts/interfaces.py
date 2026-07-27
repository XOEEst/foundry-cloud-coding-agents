from typing import Protocol

from foundry_opt.drafts.models import DraftRecord, DraftRequest


class DraftGateway(Protocol):
    def create_draft(self, request: DraftRequest) -> DraftRecord: ...

    def delete_probe(self, record: DraftRecord) -> None: ...
