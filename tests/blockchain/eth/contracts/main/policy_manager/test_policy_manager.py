"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""


import os

import pytest
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak, to_canonical_address
from web3.contract import Contract

from nucypher.blockchain.eth.interfaces import BlockchainInterface

SPONSOR_FIELD = 0
OWNER_FIELD = 1
RATE_FIELD = 2
START_TIMESTAMP_FIELD = 3
END_TIMESTAMP_FIELD = 4
DISABLED_FIELD = 5

REWARD_FIELD = 0
REWARD_RATE_FIELD = 1
LAST_MINED_PERIOD_FIELD = 2
MIN_REWARD_RATE_FIELD = 3


secret = (123456).to_bytes(32, byteorder='big')
secret2 = (654321).to_bytes(32, byteorder='big')


POLICY_ID_LENGTH = 16


@pytest.mark.slow
def test_create_revoke(testerchain, escrow, policy_manager):
    creator, policy_sponsor, bad_node, node1, node2, node3, policy_owner, *everyone_else = testerchain.client.accounts

    rate = 20
    one_period = 60 * 60
    number_of_periods = 10
    value = rate * number_of_periods

    policy_sponsor_balance = testerchain.client.get_balance(policy_sponsor)
    policy_owner_balance = testerchain.client.get_balance(policy_owner)
    policy_created_log = policy_manager.events.PolicyCreated.createFilter(fromBlock='latest')
    arrangement_revoked_log = policy_manager.events.ArrangementRevoked.createFilter(fromBlock='latest')
    policy_revoked_log = policy_manager.events.PolicyRevoked.createFilter(fromBlock='latest')
    arrangement_refund_log = policy_manager.events.RefundForArrangement.createFilter(fromBlock='latest')
    policy_refund_log = policy_manager.events.RefundForPolicy.createFilter(fromBlock='latest')

    # Check registered nodes
    assert 0 < policy_manager.functions.nodes(node1).call()[LAST_MINED_PERIOD_FIELD]
    assert 0 < policy_manager.functions.nodes(node2).call()[LAST_MINED_PERIOD_FIELD]
    assert 0 < policy_manager.functions.nodes(node3).call()[LAST_MINED_PERIOD_FIELD]
    assert 0 == policy_manager.functions.nodes(bad_node).call()[LAST_MINED_PERIOD_FIELD]
    current_timestamp = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    end_timestamp = current_timestamp + (number_of_periods - 1) * one_period
    policy_id = os.urandom(POLICY_ID_LENGTH)

    # Try to create policy for bad (unregistered) node
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id, policy_sponsor, end_timestamp, [bad_node])\
            .transact({'from': policy_sponsor, 'value': value})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id, policy_sponsor, end_timestamp, [node1, bad_node])\
            .transact({'from': policy_sponsor, 'value': value})
        testerchain.wait_for_receipt(tx)

    # Try to create policy with no ETH
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id, policy_sponsor, end_timestamp, [node1])\
            .transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Can't create policy using timestamp from the past
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id, policy_sponsor, current_timestamp -1, [node1])\
            .transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Create policy
    tx = policy_manager.functions.createPolicy(policy_id, policy_sponsor, end_timestamp, [node1])\
        .transact({'from': policy_sponsor, 'value': value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    current_timestamp = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    # Check balances and policy info
    assert value == testerchain.client.get_balance(policy_manager.address)
    assert policy_sponsor_balance - 200 == testerchain.client.get_balance(policy_sponsor)
    policy = policy_manager.functions.policies(policy_id).call()
    assert policy_sponsor == policy[SPONSOR_FIELD]
    assert BlockchainInterface.NULL_ADDRESS == policy[OWNER_FIELD]
    assert rate == policy[RATE_FIELD]
    assert current_timestamp == policy[START_TIMESTAMP_FIELD]
    assert end_timestamp == policy[END_TIMESTAMP_FIELD]
    assert not policy[DISABLED_FIELD]
    assert 1 == policy_manager.functions.getArrangementsLength(policy_id).call()
    assert node1 == policy_manager.functions.getArrangementInfo(policy_id, 0).call()[0]
    assert policy_sponsor == policy_manager.functions.getPolicyOwner(policy_id).call()

    events = policy_created_log.get_all_entries()
    assert 1 == len(events)
    event_args = events[0]['args']
    assert policy_id == event_args['policyId']
    assert policy_sponsor == event_args['sponsor']
    assert policy_sponsor == event_args['owner']
    assert rate == event_args['rewardRate']
    assert current_timestamp == event_args['startTimestamp']
    assert end_timestamp == event_args['endTimestamp']
    assert 1 == event_args['numberOfNodes']

    # Can't create policy with the same id
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id, policy_sponsor, end_timestamp, [node1])\
            .transact({'from': policy_sponsor, 'value': value})
        testerchain.wait_for_receipt(tx)

    # Only policy owner can revoke policy
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokePolicy(policy_id).transact({'from': creator})
        testerchain.wait_for_receipt(tx)
    tx = policy_manager.functions.revokePolicy(policy_id).transact({'from': policy_sponsor, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert policy_manager.functions.policies(policy_id).call()[DISABLED_FIELD]

    events = policy_revoked_log.get_all_entries()
    assert 1 == len(events)
    event_args = events[0]['args']
    assert policy_id == event_args['policyId']
    assert policy_sponsor == event_args['sender']
    assert value == event_args['value']
    events = arrangement_revoked_log.get_all_entries()
    assert 1 == len(events)

    event_args = events[0]['args']
    assert policy_id == event_args['policyId']
    assert policy_sponsor == event_args['sender']
    assert node1 == event_args['node']
    assert value == event_args['value']

    # Can't revoke again because policy and all arrangements are disabled
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokePolicy(policy_id).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id, node1).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Create new policy
    period = escrow.functions.getCurrentPeriod().call()
    tx = escrow.functions.setDefaultRewardDelta(node1, period, number_of_periods + 1).transact()
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setDefaultRewardDelta(node2, period, number_of_periods + 1).transact()
    testerchain.wait_for_receipt(tx)
    end_timestamp = current_timestamp + (number_of_periods - 1) * one_period
    policy_id_2 = os.urandom(POLICY_ID_LENGTH)
    tx = policy_manager.functions.createPolicy(policy_id_2, policy_owner, end_timestamp, [node1, node2, node3])\
        .transact({'from': policy_sponsor, 'value': 6 * value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    current_timestamp = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    assert 6 * value == testerchain.client.get_balance(policy_manager.address)
    assert policy_sponsor_balance - 6 * value == testerchain.client.get_balance(policy_sponsor)
    policy = policy_manager.functions.policies(policy_id_2).call()
    assert policy_sponsor == policy[SPONSOR_FIELD]
    assert policy_owner == policy[OWNER_FIELD]
    assert 2 * rate == policy[RATE_FIELD]
    assert current_timestamp == policy[START_TIMESTAMP_FIELD]
    assert end_timestamp == policy[END_TIMESTAMP_FIELD]
    assert not policy[DISABLED_FIELD]
    assert policy_owner == policy_manager.functions.getPolicyOwner(policy_id_2).call()

    events = policy_created_log.get_all_entries()
    assert 2 == len(events)
    event_args = events[1]['args']
    assert policy_id_2 == event_args['policyId']
    assert policy_sponsor == event_args['sponsor']
    assert policy_owner == event_args['owner']
    assert 2 * rate == event_args['rewardRate']
    assert current_timestamp == event_args['startTimestamp']
    assert end_timestamp == event_args['endTimestamp']
    assert 3 == event_args['numberOfNodes']

    # Can't revoke nonexistent arrangement
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, testerchain.client.accounts[6])\
            .transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)
    # Can't revoke null arrangement (also it's nonexistent)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, BlockchainInterface.NULL_ADDRESS).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Policy sponsor can't revoke policy, only owner can
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokePolicy(policy_id_2).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, node1).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Revoke only one arrangement
    tx = policy_manager.functions.revokeArrangement(policy_id_2, node1).transact({'from': policy_owner, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert 4 * value == testerchain.client.get_balance(policy_manager.address)
    assert policy_sponsor_balance - 4 * value == testerchain.client.get_balance(policy_sponsor)
    assert not policy_manager.functions.policies(policy_id_2).call()[DISABLED_FIELD]
    assert policy_owner_balance == testerchain.client.get_balance(policy_owner)

    events = arrangement_revoked_log.get_all_entries()
    assert 2 == len(events)
    event_args = events[1]['args']
    assert policy_id_2 == event_args['policyId']
    assert policy_owner == event_args['sender']
    assert node1 == event_args['node']
    assert 2 * value == event_args['value']

    # Can't revoke again because arrangement is disabled
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, node1).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)
    # Can't revoke null arrangement (it's nonexistent)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, BlockchainInterface.NULL_ADDRESS).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Revoke policy with remaining arrangements
    tx = policy_manager.functions.revokePolicy(policy_id_2).transact({'from': policy_owner, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert 0 == testerchain.client.get_balance(policy_manager.address)
    assert policy_sponsor_balance == testerchain.client.get_balance(policy_sponsor)
    assert policy_manager.functions.policies(policy_id_2).call()[DISABLED_FIELD]

    events = arrangement_revoked_log.get_all_entries()
    assert 4 == len(events)
    event_args = events[2]['args']
    assert policy_id_2 == event_args['policyId']
    assert policy_owner == event_args['sender']
    assert node2 == event_args['node']
    assert 2 * value == event_args['value']

    event_args = events[3]['args']
    assert policy_id_2 == event_args['policyId']
    assert policy_owner == event_args['sender']
    assert node3 == event_args['node']
    assert 2 * value == event_args['value']
    events = policy_revoked_log.get_all_entries()
    assert 2 == len(events)
    event_args = events[1]['args']
    assert policy_id_2 == event_args['policyId']
    assert policy_owner == event_args['sender']
    assert 4 * value == event_args['value']

    # Can't revoke policy again because policy and all arrangements are disabled
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokePolicy(policy_id_2).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, node1).transact({'from': policy_sponsor})
        testerchain.wait_for_receipt(tx)

    # Can't create policy with wrong ETH value - when reward is not calculated by formula:
    # numberOfNodes * rewardRate * numberOfPeriods
    policy_id_3 = os.urandom(POLICY_ID_LENGTH)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id_3, policy_sponsor, end_timestamp, [node1])\
            .transact({'from': policy_sponsor, 'value': 11})
        testerchain.wait_for_receipt(tx)

    # Set minimum reward rate for nodes
    tx = policy_manager.functions.setMinRewardRate(10).transact({'from': node1})
    testerchain.wait_for_receipt(tx)
    tx = policy_manager.functions.setMinRewardRate(20).transact({'from': node2})
    testerchain.wait_for_receipt(tx)
    assert 10 == policy_manager.functions.nodes(node1).call()[MIN_REWARD_RATE_FIELD]
    assert 20 == policy_manager.functions.nodes(node2).call()[MIN_REWARD_RATE_FIELD]

    # Try to create policy with low rate
    current_timestamp = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    end_timestamp = current_timestamp + 10
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id_3, policy_sponsor, end_timestamp, [node1])\
            .transact({'from': policy_sponsor, 'value': 5})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.createPolicy(policy_id_3, policy_sponsor, end_timestamp, [node1, node2])\
            .transact({'from': policy_sponsor, 'value': 30})
        testerchain.wait_for_receipt(tx)

    # Create new policy
    end_timestamp = current_timestamp + (number_of_periods - 1) * one_period
    tx = policy_manager.functions.createPolicy(
        policy_id_3, BlockchainInterface.NULL_ADDRESS, end_timestamp, [node1, node2]) \
        .transact({'from': policy_sponsor, 'value': 2 * value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    current_timestamp = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    assert 2 * value == testerchain.client.get_balance(policy_manager.address)
    assert policy_sponsor_balance - 2 * value == testerchain.client.get_balance(policy_sponsor)
    policy = policy_manager.functions.policies(policy_id_3).call()
    assert policy_sponsor == policy[SPONSOR_FIELD]
    assert BlockchainInterface.NULL_ADDRESS == policy[OWNER_FIELD]
    assert rate == policy[RATE_FIELD]
    assert current_timestamp == policy[START_TIMESTAMP_FIELD]
    assert end_timestamp == policy[END_TIMESTAMP_FIELD]
    assert not policy[DISABLED_FIELD]
    assert policy_sponsor == policy_manager.functions.getPolicyOwner(policy_id_3).call()

    events = policy_created_log.get_all_entries()
    assert 3 == len(events)
    event_args = events[2]['args']
    assert policy_id_3 == event_args['policyId']
    assert policy_sponsor == event_args['sponsor']
    assert policy_sponsor == event_args['owner']
    assert rate == event_args['rewardRate']
    assert current_timestamp == event_args['startTimestamp']
    assert end_timestamp == event_args['endTimestamp']
    assert 2 == event_args['numberOfNodes']

    # Revocation using signature

    data = policy_id_3 + to_canonical_address(node1)
    wrong_signature = testerchain.client.sign_message(account=creator, message=data)
    # Only owner's signature can be used
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revoke(policy_id_3, node1, wrong_signature)\
            .transact({'from': policy_sponsor, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)
    signature = testerchain.client.sign_message(account=policy_sponsor, message=data)
    tx = policy_manager.functions.revoke(policy_id_3, node1, signature)\
        .transact({'from': policy_sponsor, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert value == testerchain.client.get_balance(policy_manager.address)
    assert policy_sponsor_balance - value == testerchain.client.get_balance(policy_sponsor)
    assert not policy_manager.functions.policies(policy_id_3).call()[DISABLED_FIELD]
    assert BlockchainInterface.NULL_ADDRESS == policy_manager.functions.getArrangementInfo(policy_id_3, 0).call()[0]
    assert node2 == policy_manager.functions.getArrangementInfo(policy_id_3, 1).call()[0]

    data = policy_id_3 + to_canonical_address(BlockchainInterface.NULL_ADDRESS)
    signature = testerchain.client.sign_message(account=policy_sponsor, message=data)
    tx = policy_manager.functions.revoke(policy_id_3, BlockchainInterface.NULL_ADDRESS, signature)\
        .transact({'from': creator, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert policy_manager.functions.policies(policy_id_3).call()[DISABLED_FIELD]

    # Create new policy
    end_timestamp = current_timestamp + (number_of_periods - 1) * one_period
    policy_id_4 = os.urandom(POLICY_ID_LENGTH)
    tx = policy_manager.functions.createPolicy(policy_id_4, policy_owner, end_timestamp, [node1, node2, node3]) \
        .transact({'from': policy_sponsor, 'value': 3 * value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)

    data = policy_id_4 + to_canonical_address(BlockchainInterface.NULL_ADDRESS)
    wrong_signature = testerchain.client.sign_message(account=policy_sponsor, message=data)
    # Only owner's signature can be used
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revoke(policy_id_4, BlockchainInterface.NULL_ADDRESS, wrong_signature)\
            .transact({'from': policy_owner, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)
    signature = testerchain.client.sign_message(account=policy_owner, message=data)
    tx = policy_manager.functions.revoke(policy_id_4, BlockchainInterface.NULL_ADDRESS, signature)\
        .transact({'from': creator, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert policy_manager.functions.policies(policy_id_4).call()[DISABLED_FIELD]

    events = policy_revoked_log.get_all_entries()
    assert 4 == len(events)
    events = arrangement_revoked_log.get_all_entries()
    assert 9 == len(events)

    events = arrangement_refund_log.get_all_entries()
    assert 0 == len(events)
    events = policy_refund_log.get_all_entries()
    assert 0 == len(events)


@pytest.mark.slow
def test_upgrading(testerchain, deploy_contract):
    creator = testerchain.client.accounts[0]

    secret_hash = keccak(secret)
    secret2_hash = keccak(secret2)

    # Only escrow contract is allowed in PolicyManager constructor
    with pytest.raises((TransactionFailed, ValueError)):
        deploy_contract('PolicyManager', creator)

    # Deploy contracts
    escrow1, _ = deploy_contract('StakingEscrowForPolicyMock', 1)
    escrow2, _ = deploy_contract('StakingEscrowForPolicyMock', 1)
    address1 = escrow1.address
    address2 = escrow2.address
    contract_library_v1, _ = deploy_contract('PolicyManager', address1)
    dispatcher, _ = deploy_contract('Dispatcher', contract_library_v1.address, secret_hash)

    # Deploy second version of the contract
    contract_library_v2, _ = deploy_contract('PolicyManagerV2Mock', address2)
    contract = testerchain.client.get_contract(
        abi=contract_library_v2.abi,
        address=dispatcher.address,
        ContractFactoryClass=Contract)

    # Can't call `finishUpgrade` and `verifyState` methods outside upgrade lifecycle
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract_library_v1.functions.finishUpgrade(contract.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract_library_v1.functions.verifyState(contract.address).transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    # Upgrade to the second version
    assert address1 == contract.functions.escrow().call()
    tx = dispatcher.functions.upgrade(contract_library_v2.address, secret, secret2_hash).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    # Check constructor and storage values
    assert contract_library_v2.address == dispatcher.functions.target().call()
    assert address2 == contract.functions.escrow().call()
    # Check new ABI
    tx = contract.functions.setValueToCheck(3).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    assert 3 == contract.functions.valueToCheck().call()

    # Can't upgrade to the previous version or to the bad version
    contract_library_bad, _ = deploy_contract('PolicyManagerBad', address2)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = dispatcher.functions.upgrade(contract_library_v1.address, secret2, secret_hash)\
            .transact({'from': creator})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = dispatcher.functions.upgrade(contract_library_bad.address, secret2, secret_hash)\
            .transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    # But can rollback
    tx = dispatcher.functions.rollback(secret2, secret_hash).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    assert contract_library_v1.address == dispatcher.functions.target().call()
    assert address1 == contract.functions.escrow().call()
    # After rollback new ABI is unavailable
    with pytest.raises((TransactionFailed, ValueError)):
        tx = contract.functions.setValueToCheck(2).transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    # Try to upgrade to the bad version
    with pytest.raises((TransactionFailed, ValueError)):
        tx = dispatcher.functions.upgrade(contract_library_bad.address, secret, secret2_hash)\
            .transact({'from': creator})
        testerchain.wait_for_receipt(tx)

    events = dispatcher.events.StateVerified.createFilter(fromBlock=0).get_all_entries()
    assert 4 == len(events)
    event_args = events[0]['args']
    assert contract_library_v1.address == event_args['testTarget']
    assert creator == event_args['sender']
    event_args = events[1]['args']
    assert contract_library_v2.address == event_args['testTarget']
    assert creator == event_args['sender']
    assert event_args == events[2]['args']
    event_args = events[3]['args']
    assert contract_library_v2.address == event_args['testTarget']
    assert creator == event_args['sender']

    events = dispatcher.events.UpgradeFinished.createFilter(fromBlock=0).get_all_entries()
    assert 3 == len(events)
    event_args = events[0]['args']
    assert contract_library_v1.address == event_args['target']
    assert creator == event_args['sender']
    event_args = events[1]['args']
    assert contract_library_v2.address == event_args['target']
    assert creator == event_args['sender']
    event_args = events[2]['args']
    assert contract_library_v1.address == event_args['target']
    assert creator == event_args['sender']
