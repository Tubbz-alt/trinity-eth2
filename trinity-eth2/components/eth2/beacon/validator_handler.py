from typing import cast

from cancel_token import CancelToken
from eth_typing import BLSSignature
from lahja import EndpointAPI
from trinity._utils.shellart import bold_green
from trinity.components.eth2.beacon.base_validator import (
    BaseValidator,
    GetAggregatableAttestationsFn,
    GetReadyAttestationsFn,
    ImportAttestationFn,
)
from trinity.http.api.events import GetBeaconBlockRequest, GetBeaconBlockResponse
from trinity.protocol.bcc_libp2p.node import Node

from eth2.beacon.chains.base import BaseBeaconChain
from eth2.beacon.state_machines.abc import BaseBeaconStateMachine
from eth2.beacon.tools.builder.proposer import create_block_proposal
from eth2.beacon.types.blocks import BaseBeaconBlock
from eth2.beacon.typing import Slot


class ValidatorHandler(BaseValidator):
    def __init__(
        self,
        chain: BaseBeaconChain,
        p2p_node: Node,
        event_bus: EndpointAPI,
        get_ready_attestations_fn: GetReadyAttestationsFn,
        get_aggregatable_attestations_fn: GetAggregatableAttestationsFn,
        import_attestation_fn: ImportAttestationFn,
        token: CancelToken = None,
    ) -> None:
        super().__init__(
            chain,
            p2p_node,
            event_bus,
            get_ready_attestations_fn,
            get_aggregatable_attestations_fn,
            import_attestation_fn,
            token,
        )

    async def _run(self) -> None:
        self.logger.info(bold_green("ValidatorHandler is running"))
        self.run_daemon_task(self.handle_get_block_requests())

        await self.cancellation()

    async def generate_unsigned_block(
        self, slot: Slot, randao_reveal: BLSSignature
    ) -> BaseBeaconBlock:
        """
        Generate an unsigned block of the given ``slot`` with ``randao_reaveal``.
        """
        head_block = self.chain.get_canonical_head()
        state_machine = self.chain.get_state_machine()
        state = self.chain.get_head_state()

        eth1_vote = await self._get_eth1_vote(slot, state, state_machine)
        # deposits = await self._get_deposit_data(state, state_machine, eth1_vote)

        # TODO(hwwhww): Check if need to aggregate and if they are overlapping.

        aggregated_attestations = self.get_ready_attestations(slot, True)
        unaggregated_attestations = self.get_ready_attestations(slot, False)
        ready_attestations = aggregated_attestations + unaggregated_attestations

        return create_block_proposal(
            slot,
            head_block.message.hash_tree_root,
            randao_reveal,
            eth1_vote,
            ready_attestations,
            state,
            cast(BaseBeaconStateMachine, state_machine),
        )

    #
    # Handle API Request
    #
    async def handle_get_block_requests(self) -> None:
        async for req in self.wait_iter(self.event_bus.stream(GetBeaconBlockRequest)):
            block = await self.generate_unsigned_block(req.slot, req.randao_reveal)
            await self.event_bus.broadcast(
                GetBeaconBlockResponse(block), req.broadcast_config()
            )
