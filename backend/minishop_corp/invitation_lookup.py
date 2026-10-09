"""Q-01 protects code guesses; an exact persisted preview confirms immediately.

Expected errors are values: the endpoint must commit the session before returning
them, otherwise failure counters would disappear with its rollback. Successful
confirmation callers retain the user/contract locks while preparing the join.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts_types import ContractError
from .integration.access_types import AccessError
from .integration.code_attempts import CORPORATE_ATTEMPT_POLICY, AttemptPolicy, CodeAttempts
from .integration.contracts import ContractHost
from .invitations_types import InvitationOffer, OfferReference, TariffOffer, code_digest
from .storage.invitations import lookup_invitation
from .storage.schema import Contract, InvitationPreview


@dataclass(frozen=True)
class LookupResult:
    status: int
    offer: InvitationOffer | None = None
    tariff: TariffOffer | None = None
    error: str | None = None
    retry_after: int | None = None


class InvitationLookup:
    def __init__(
        self, host: ContractHost, policy: AttemptPolicy = CORPORATE_ATTEMPT_POLICY
    ) -> None:
        self.host = host
        self.attempts = CodeAttempts(policy)

    async def inspect(
        self,
        session: AsyncSession,
        user_id: int,
        code: SecretStr,
        *,
        expected: OfferReference | None = None,
        now: datetime | None = None,
    ) -> LookupResult:
        instant = now or datetime.now(UTC)
        await self.host.lock_accounts(session, [user_id])
        preview = await session.get(InvitationPreview, user_id, populate_existing=True)
        verified = False
        if expected is not None and preview is not None and preview.expires_at > instant:
            try:
                verified = (
                    preview.code_digest == code_digest(code)
                    and preview.invitation_id == expected.invitation_id
                    and preview.contract_id == expected.contract_id
                    and preview.contract_version == expected.contract_version
                )
            except ContractError:
                pass  # Malformed input must still pass through the guessing budget.
        admission = await (
            self.attempts.confirmation(session, user_id, now=instant)
            if verified
            else self.attempts.admit(session, user_id, now=instant)
        )
        if not admission.allowed:
            return LookupResult(
                429, error="minishop_corp_code_throttled", retry_after=admission.retry_after
            )
        try:
            offer = await lookup_invitation(session, code, now=instant)
            if expected is not None and (
                expected.invitation_id != offer.invitation_id
                or expected.contract_id != offer.contract.id
                or expected.contract_version != offer.contract.version
            ):
                return LookupResult(
                    409, error="minishop_corp_offer_changed", retry_after=admission.retry_after
                )
            contract = await session.get(Contract, offer.contract.id)
            if contract is None:
                return LookupResult(
                    503, error="minishop_corp_contract_missing", retry_after=admission.retry_after
                )
            tariff = self.host.describe_tariff(contract.tariff_key)
            if expected is None:
                if preview is None:
                    preview = InvitationPreview(user_id=user_id)
                    session.add(preview)
                preview.code_digest = code_digest(code)
                preview.invitation_id = offer.invitation_id
                preview.contract_id = offer.contract.id
                preview.contract_version = offer.contract.version
                preview.expires_at = instant + timedelta(minutes=15)
                await session.flush()
            return LookupResult(200, offer=offer, tariff=tariff, retry_after=admission.retry_after)
        except ContractError as exc:
            if exc.code != "minishop_corp_invitation_unavailable":
                raise
            if verified:
                # Revocation/expiry/exhaustion after a valid preview is not a bad guess.
                return LookupResult(400, error=exc.code)
            penalty = await self.attempts.record_failure(session, user_id, now=instant)
            return LookupResult(400, error=exc.code, retry_after=penalty.retry_after)
        except AccessError as exc:
            # Invalid tariff configuration is a store error, never a guessed-code failure.
            return LookupResult(503, error=exc.code.value, retry_after=admission.retry_after)
