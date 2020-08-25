import random

from eth_utils import to_tuple
import pytest
from trinity.nodes.beacon.full import ChainDBBlockSink

from eth2.beacon.chains.exceptions import SlashableBlockError
from eth2.beacon.chains.testnet.medalla import BeaconChain
from eth2.beacon.committee_helpers import get_beacon_proposer_index
from eth2.beacon.constants import ZERO_HASH32
from eth2.beacon.fork_choice.lmd_ghost2 import LMDGHOSTForkChoice
from eth2.beacon.state_machines.forks.medalla.configs import MEDALLA_CONFIG
from eth2.beacon.tools.builder.proposer import create_block, generate_randao_reveal
from eth2.beacon.tools.builder.validator import create_mock_signed_attestations_at_slot
from eth2.beacon.types.blocks import BeaconBlock
from eth2.beacon.types.eth1_data import Eth1Data
from eth2.beacon.typing import Epoch, Slot
from eth2.clock import Tick


@pytest.fixture(scope="module")
def config():
    return MEDALLA_CONFIG


@pytest.fixture(scope="module")
def validator_count():
    """
    NOTE: overriding here for fork choice test where
    it is easier to test weights with more validators
    """
    return 2048


def _build_chain_of_blocks_with_states(
    chain,
    state,
    parent_block,
    slots,
    config,
    keymap,
    attestation_participation=1.0,
    eth1_block_hash=ZERO_HASH32,
):
    blocks = ()
    states = ()
    for slot in range(parent_block.slot + 1, parent_block.slot + 1 + slots):
        sm = chain.get_state_machine(state.slot)
        pre_state, _ = sm.apply_state_transition(state, future_slot=slot)
        proposer_index = get_beacon_proposer_index(pre_state, config)
        public_key = state.validators[proposer_index].pubkey
        private_key = keymap[public_key]
        randao_reveal = generate_randao_reveal(private_key, slot, pre_state, config)

        attestations = create_mock_signed_attestations_at_slot(
            state,
            config,
            sm,
            slot - 1,
            parent_block.hash_tree_root,
            keymap,
            voted_attesters_ratio=attestation_participation,
        )
        block = create_block(
            slot,
            parent_block.hash_tree_root,
            randao_reveal,
            Eth1Data.create(block_hash=eth1_block_hash),
            attestations,
            state,
            sm,
            private_key,
        )

        parent_block = block.message
        state, block = sm.apply_state_transition(state, block)

        blocks += (block,)
        states += (state,)
    return blocks, states


@to_tuple
def _mk_attestations_from(blocks, states, chain, config, keymap):
    for block, state in zip(blocks, states):
        sm = chain.get_state_machine(block.slot)
        yield from create_mock_signed_attestations_at_slot(
            state, config, sm, block.slot, block.message.hash_tree_root, keymap
        )


@pytest.mark.slow
def test_chain_can_track_canonical_head_without_attestations(
    base_db, genesis_state, genesis_block, config, keymap
):
    chain = BeaconChain.from_genesis(base_db, genesis_state)

    genesis_head = chain.get_canonical_head()
    assert genesis_head == genesis_block.message

    some_epochs = 4
    some_slots = some_epochs * config.SLOTS_PER_EPOCH
    blocks, _ = _build_chain_of_blocks_with_states(
        chain,
        genesis_state,
        genesis_block.message,
        some_slots,
        config,
        keymap,
        attestation_participation=0,
    )
    for block in blocks:
        chain.on_block(block)

    head = chain.get_canonical_head()
    assert head == blocks[-1].message


@pytest.mark.slow
def test_chain_can_track_canonical_head_across_restarts(
    base_db, genesis_state, genesis_block, config, keymap
):
    """
    This test is essentially an integration test for the ability to
    restore the fork choice context from any state saved in the database.

    1. Build a chain of many epochs
    2. Advance beyond the genesis condition
       (can get to first change in finalized head plus a few slots)
    3. Tear down the ``chain`` instance.
    4. Make a new one from the database state
    5. Ensure we can recover last state.
    6. Import rest of chain and ensure normal operation.
    """
    chain = BeaconChain.from_genesis(base_db, genesis_state)

    genesis_head = chain.get_canonical_head()
    assert genesis_head == genesis_block.message

    some_epochs = 8
    some_slots = some_epochs * config.SLOTS_PER_EPOCH
    blocks, _ = _build_chain_of_blocks_with_states(
        chain, genesis_state, genesis_block.message, some_slots, config, keymap
    )
    segment_slot = 4 * config.SLOTS_PER_EPOCH + 3
    first_segment = ()
    second_segment = ()
    for block in blocks:
        if block.slot <= segment_slot:
            first_segment += (block,)
        else:
            second_segment += (block,)

    for block in first_segment:
        chain.on_block(block)

    finalized_head = chain.db.get_finalized_head(BeaconBlock)
    justified_head = chain.db.get_justified_head(BeaconBlock)
    canonical_head = chain.get_canonical_head()
    assert finalized_head != genesis_head
    assert canonical_head == first_segment[-1].message

    chain_db = chain.db
    del chain

    block_sink = ChainDBBlockSink(chain_db)
    fork_choice = LMDGHOSTForkChoice.from_db(chain_db, config, block_sink)
    chain = BeaconChain(chain_db, fork_choice)
    restored_canonical_head = chain.get_canonical_head()
    assert canonical_head == restored_canonical_head
    restored_justified_head = chain.db.get_justified_head(BeaconBlock)
    assert justified_head == restored_justified_head
    restored_finalized_head = chain.db.get_finalized_head(BeaconBlock)
    assert finalized_head == restored_finalized_head

    for block in second_segment:
        chain.on_block(block)

    finalized_head = chain.db.get_finalized_head(BeaconBlock)
    justified_head = chain.db.get_justified_head(BeaconBlock)
    canonical_head = chain.get_canonical_head()
    assert finalized_head != restored_finalized_head
    assert justified_head != restored_justified_head
    assert canonical_head != restored_canonical_head
    assert canonical_head == second_segment[-1].message


@pytest.mark.slow
def test_chain_can_track_canonical_head(
    base_db, genesis_state, genesis_block, config, keymap
):
    chain = BeaconChain.from_genesis(base_db, genesis_state)

    genesis_head = chain.get_canonical_head()
    assert genesis_head == genesis_block.message

    some_epochs = 5
    some_slots = some_epochs * config.SLOTS_PER_EPOCH
    blocks, states = _build_chain_of_blocks_with_states(
        chain, genesis_state, genesis_block.message, some_slots, config, keymap
    )
    for block in blocks:
        chain.on_block(block)

    head = chain.get_canonical_head()
    assert head == blocks[-1].message

    some_attack_slot = random.randint(2, head.slot)
    blocks, _ = _build_chain_of_blocks_with_states(
        chain,
        states[some_attack_slot - 1],
        blocks[some_attack_slot - 1].message,
        5,
        config,
        keymap,
        attestation_participation=0,
    )
    for block in blocks:
        with pytest.raises(SlashableBlockError):
            chain.on_block(block)
    existing_head = head
    head = chain.get_canonical_head()
    assert head == existing_head


@pytest.mark.slow
def test_chain_can_reorg_with_attestations(
    base_db, genesis_state, genesis_block, config, keymap
):
    chain = BeaconChain.from_genesis(base_db, genesis_state)

    genesis_head = chain.get_canonical_head()
    assert genesis_head == genesis_block.message

    some_epochs = 5
    some_slots = some_epochs * config.SLOTS_PER_EPOCH
    blocks, states = _build_chain_of_blocks_with_states(
        chain,
        genesis_state,
        genesis_block.message,
        some_slots,
        config,
        keymap,
        attestation_participation=0.01,
    )
    for block in blocks:
        chain.on_block(block)

    head = chain.get_canonical_head()
    assert head == blocks[-1].message

    # NOTE: ideally we can randomly pick a reorg slot and successfully execute a reorg...
    # however, it becomes tricky to do reliably at low validator count as the numbers
    # may be too low to easily get enough stake one way or the other...
    # The following numbers are somewhat handcrafted and it would greatly improve this test
    # if it were made more resilient to these parameters. However, the big thing blocking
    # that is performance work so that the test runs in a short time at high validator count.
    some_reorg_slot = 12
    # NOTE: this block hash is selected so that
    # we do not re-org in the chain import due to the tie breaker...
    some_block_hash = b"\x12" * 32

    blocks, states = _build_chain_of_blocks_with_states(
        chain,
        states[some_reorg_slot - 1],
        blocks[some_reorg_slot - 1].message,
        25,
        config,
        keymap,
        attestation_participation=0,
        eth1_block_hash=some_block_hash,
    )
    for block in blocks:
        with pytest.raises(SlashableBlockError):
            chain.on_block(block)
    existing_head = head
    head = chain.get_canonical_head()
    assert head == existing_head

    attestations = _mk_attestations_from(blocks, states, chain, config, keymap)
    for attestation in attestations:
        chain.on_attestation(attestation)
    # NOTE: we have not updated the fork choice yet...
    # This is essentially to prevent what would otherwise be a DoS
    # vector according to this test...
    head = chain.get_canonical_head()
    assert head == existing_head

    # NOTE: simulate a tick to run the fork choice
    # in this case, we do not care which tick it is,
    # as long as it is the first tick in the slot
    chain.on_tick(Tick(0, Slot(0), Epoch(0), 0))

    head = chain.get_canonical_head()
    assert head != existing_head
    assert head == blocks[-1].message
