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
from eth_utils import to_canonical_address, to_wei
from umbral.keys import UmbralPrivateKey
from umbral.signing import Signer
from web3.contract import Contract

from nucypher.blockchain.economics import BaseEconomics
from nucypher.blockchain.eth.interfaces import BlockchainInterface
from nucypher.crypto.api import sha256_digest
from nucypher.crypto.signing import SignatureStamp

DISABLE_RE_STAKE_FIELD = 3
WIND_DOWN_FIELD = 10

DISABLED_FIELD = 5

SECRET_LENGTH = 32
escrow_secret = os.urandom(SECRET_LENGTH)
policy_manager_secret = os.urandom(SECRET_LENGTH)
router_secret = os.urandom(SECRET_LENGTH)
adjudicator_secret = os.urandom(SECRET_LENGTH)


@pytest.fixture()
def token_economics():
    economics = BaseEconomics(
        initial_supply=10 ** 9,
        total_supply=2 * 10 ** 9,
        staking_coefficient=8 * 10 ** 7,
        locked_periods_coefficient=4,
        maximum_rewarded_periods=4,
        hours_per_period=1,
        minimum_locked_periods=6,
        minimum_allowed_locked=100,
        maximum_allowed_locked=2000,
        minimum_worker_periods=2,
        base_penalty=300,
        percentage_penalty_coefficient=2)
    return economics


@pytest.fixture()
def token(token_economics, deploy_contract):
    # Create an ERC20 token
    contract, _ = deploy_contract('NuCypherToken', _totalSupply=token_economics.erc20_total_supply)
    return contract


@pytest.fixture()
def escrow(testerchain, token, token_economics, deploy_contract):
    # Creator deploys the escrow
    contract, _ = deploy_contract(
        'StakingEscrow', token.address, *token_economics.staking_deployment_parameters, True
    )

    secret_hash = testerchain.w3.keccak(escrow_secret)
    dispatcher, _ = deploy_contract('Dispatcher', contract.address, secret_hash)

    # Wrap dispatcher contract
    contract = testerchain.client.get_contract(
        abi=contract.abi,
        address=dispatcher.address,
        ContractFactoryClass=Contract)
    return contract, dispatcher


@pytest.fixture()
def policy_manager(testerchain, escrow, deploy_contract):
    escrow, _ = escrow
    creator = testerchain.client.accounts[0]

    secret_hash = testerchain.w3.keccak(policy_manager_secret)

    # Creator deploys the policy manager
    contract, _ = deploy_contract('PolicyManager', escrow.address)
    dispatcher, _ = deploy_contract('Dispatcher', contract.address, secret_hash)

    # Wrap dispatcher contract
    contract = testerchain.client.get_contract(
        abi=contract.abi,
        address=dispatcher.address,
        ContractFactoryClass=Contract)

    tx = escrow.functions.setPolicyManager(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    return contract, dispatcher


@pytest.fixture()
def adjudicator(testerchain, escrow, token_economics, deploy_contract):
    escrow, _ = escrow
    creator = testerchain.client.accounts[0]

    secret_hash = testerchain.w3.keccak(adjudicator_secret)

    # Creator deploys the contract
    contract, _ = deploy_contract(
        'Adjudicator',
        escrow.address,
        *token_economics.slashing_deployment_parameters)

    dispatcher, _ = deploy_contract('Dispatcher', contract.address, secret_hash)

    # Wrap dispatcher contract
    contract = testerchain.client.get_contract(
        abi=contract.abi,
        address=dispatcher.address,
        ContractFactoryClass=Contract)

    tx = escrow.functions.setAdjudicator(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    return contract, dispatcher


def mock_ursula(testerchain, account, mocker):
    ursula_privkey = UmbralPrivateKey.gen_key()
    ursula_stamp = SignatureStamp(verifying_key=ursula_privkey.pubkey,
                                  signer=Signer(ursula_privkey))

    signed_stamp = testerchain.client.sign_message(account=account,
                                                   message=bytes(ursula_stamp))

    ursula = mocker.Mock(stamp=ursula_stamp, decentralized_identity_evidence=signed_stamp)
    return ursula


# TODO organize support functions
def generate_args_for_slashing(mock_ursula_reencrypts, ursula):
    evidence = mock_ursula_reencrypts(ursula, corrupt_cfrag=True)
    args = list(evidence.evaluation_arguments())
    data_hash = sha256_digest(evidence.task.capsule, evidence.task.cfrag)
    return data_hash, args


@pytest.fixture()
def staking_interface(testerchain, token, escrow, policy_manager, deploy_contract):
    escrow, _ = escrow
    policy_manager, _ = policy_manager
    secret_hash = testerchain.w3.keccak(router_secret)
    # Creator deploys the staking interface
    staking_interface, _ = deploy_contract(
        'StakingInterface', token.address, escrow.address, policy_manager.address)
    router, _ = deploy_contract(
        'StakingInterfaceRouter', staking_interface.address, secret_hash)
    return staking_interface, router


@pytest.fixture()
def worklock(testerchain, token, escrow, token_economics, deploy_contract):
    escrow, _ = escrow
    creator = testerchain.w3.eth.accounts[0]

    # Creator deploys the worklock using test values
    now = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    start_bid_date = ((now + 3600) // 3600 + 1) * 3600  # beginning of the next hour plus 1 hour
    end_bid_date = start_bid_date + 3600
    boosting_refund = 100
    staking_periods = token_economics.minimum_locked_periods
    contract, _ = deploy_contract(
        contract_name='WorkLock',
        _token=token.address,
        _escrow=escrow.address,
        _startBidDate=start_bid_date,
        _endBidDate=end_bid_date,
        _boostingRefund=boosting_refund,
        _stakingPeriods=staking_periods
    )

    tx = escrow.functions.setWorkLock(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    return contract


@pytest.fixture()
def multisig(testerchain, escrow, policy_manager, adjudicator, staking_interface, deploy_contract):
    escrow, escrow_dispatcher = escrow
    policy_manager, policy_manager_dispatcher = policy_manager
    adjudicator, adjudicator_dispatcher = adjudicator
    staking_interface, staking_interface_router = staking_interface
    creator, _staker1, _staker2, _staker3, _staker4, _alice1, _alice2, *contract_owners =\
        testerchain.client.accounts
    contract_owners = sorted(contract_owners)
    contract, _ = deploy_contract('MultiSig', 2, contract_owners)
    tx = escrow.functions.transferOwnership(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = policy_manager.functions.transferOwnership(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = adjudicator.functions.transferOwnership(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = staking_interface_router.functions.transferOwnership(contract.address).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    return contract


def execute_multisig_transaction(testerchain, multisig, accounts, tx):

    def to_32byte_hex(w3, value):
        return w3.toHex(w3.toBytes(value).rjust(32, b'\0'))

    def sign_hash(testerchain, account: str, data_hash: bytes) -> dict:
        provider = testerchain.provider
        address = to_canonical_address(account)
        key = provider.ethereum_tester.backend._key_lookup[address]._raw_key
        signed_data = testerchain.w3.eth.account.signHash(data_hash, key)
        return signed_data

    nonce = multisig.functions.nonce().call()
    tx_hash = multisig.functions.getUnsignedTransactionHash(accounts[0], tx['to'], 0, tx['data'], nonce).call()
    signatures = [sign_hash(testerchain, account, tx_hash) for account in accounts]
    w3 = testerchain.w3
    tx = multisig.functions.execute(
        [signature.v for signature in signatures],
        [to_32byte_hex(w3, signature.r) for signature in signatures],
        [to_32byte_hex(w3, signature.s) for signature in signatures],
        tx['to'],
        0,
        tx['data']
    ).transact({'from': accounts[0]})
    testerchain.wait_for_receipt(tx)


@pytest.mark.slow
def test_all(testerchain,
             token_economics,
             token,
             escrow,
             policy_manager,
             adjudicator,
             worklock,
             staking_interface,
             multisig,
             mock_ursula_reencrypts,
             deploy_contract,
             mocker):

    # Travel to the start of the next period to prevent problems with unexpected overflow first period
    testerchain.time_travel(hours=1)

    escrow, escrow_dispatcher = escrow
    policy_manager, policy_manager_dispatcher = policy_manager
    adjudicator, adjudicator_dispatcher = adjudicator
    staking_interface, staking_interface_router = staking_interface
    creator, staker1, staker2, staker3, staker4, alice1, alice2, *contracts_owners =\
        testerchain.client.accounts
    contracts_owners = sorted(contracts_owners)

    # We'll need this later for slashing these Ursulas
    ursula1_with_stamp = mock_ursula(testerchain, staker1, mocker=mocker)
    ursula2_with_stamp = mock_ursula(testerchain, staker2, mocker=mocker)
    ursula3_with_stamp = mock_ursula(testerchain, staker3, mocker=mocker)

    # Give clients some ether
    tx = testerchain.client.send_transaction(
        {'from': testerchain.client.coinbase, 'to': alice1, 'value': 10 ** 10})
    testerchain.wait_for_receipt(tx)
    tx = testerchain.client.send_transaction(
        {'from': testerchain.client.coinbase, 'to': alice2, 'value': 10 ** 10})
    testerchain.wait_for_receipt(tx)
    tx = testerchain.w3.eth.sendTransaction(
        {'from': testerchain.w3.eth.coinbase, 'to': staker2, 'value': 10 ** 10})
    testerchain.wait_for_receipt(tx)

    # Give staker and Alice some coins
    tx = token.functions.transfer(staker1, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.transfer(alice1, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.transfer(alice2, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    assert 10000 == token.functions.balanceOf(staker1).call()
    assert 10000 == token.functions.balanceOf(alice1).call()
    assert 10000 == token.functions.balanceOf(alice2).call()

    # Staker gives Escrow rights to transfer
    tx = token.functions.approve(escrow.address, 10000).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, 10000).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)

    # Staker can't deposit tokens before Escrow initialization
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.deposit(1, 1).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Initialize escrow
    tx = token.functions.transfer(multisig.address, token_economics.erc20_reward_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(escrow.address, token_economics.erc20_reward_supply)\
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], contracts_owners[1]], tx)
    tx = escrow.functions.initialize(token_economics.erc20_reward_supply)\
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], contracts_owners[1]], tx)

    # Initialize worklock
    worklock_supply = 1980
    tx = token.functions.approve(worklock.address, worklock_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = worklock.functions.tokenDeposit(worklock_supply).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Can't do anything before start date
    deposited_eth_1 = to_wei(18, 'ether')
    deposited_eth_2 = to_wei(1, 'ether')
    with pytest.raises((TransactionFailed, ValueError)):
        tx = worklock.functions.bid().transact({'from': staker2, 'value': deposited_eth_1, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)

    # Wait for the start of the bidding
    testerchain.time_travel(hours=1)

    # Staker does bid
    assert worklock.functions.workInfo(staker2).call()[0] == 0
    assert testerchain.w3.eth.getBalance(worklock.address) == 0
    tx = worklock.functions.bid().transact({'from': staker2, 'value': deposited_eth_1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert worklock.functions.workInfo(staker2).call()[0] == deposited_eth_1
    assert testerchain.w3.eth.getBalance(worklock.address) == deposited_eth_1
    assert worklock.functions.ethToTokens(deposited_eth_1).call() == worklock_supply

    # Can't claim while bidding phase
    with pytest.raises((TransactionFailed, ValueError)):
        tx = worklock.functions.claim().transact({'from': staker2, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)

    # Other stakers do bid
    assert worklock.functions.workInfo(staker1).call()[0] == 0
    tx = worklock.functions.bid().transact({'from': staker1, 'value': deposited_eth_2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert worklock.functions.workInfo(staker1).call()[0] == deposited_eth_2
    assert testerchain.w3.eth.getBalance(worklock.address) == deposited_eth_1 + deposited_eth_2
    assert worklock.functions.ethToTokens(deposited_eth_2).call() == worklock_supply // 19

    assert worklock.functions.workInfo(staker4).call()[0] == 0
    tx = worklock.functions.bid().transact({'from': staker4, 'value': deposited_eth_2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert worklock.functions.workInfo(staker4).call()[0] == deposited_eth_2
    assert testerchain.w3.eth.getBalance(worklock.address) == deposited_eth_1 + 2 * deposited_eth_2
    assert worklock.functions.ethToTokens(deposited_eth_2).call() == worklock_supply // 20

    # Wait for the end of the bidding
    testerchain.time_travel(hours=1)

    # Can't bid after the enf of bidding
    with pytest.raises((TransactionFailed, ValueError)):
        tx = worklock.functions.bid().transact({'from': staker2, 'value': 1, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)

    # One of stakers cancels bid
    tx = worklock.functions.cancelBid().transact({'from': staker1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert worklock.functions.workInfo(staker1).call()[0] == 0
    assert testerchain.w3.eth.getBalance(worklock.address) == deposited_eth_1 + deposited_eth_2
    assert worklock.functions.ethToTokens(deposited_eth_2).call() == worklock_supply // 20
    assert worklock.functions.unclaimedTokens().call() == worklock_supply // 20

    # Staker claims tokens
    assert not worklock.functions.workInfo(staker2).call()[2]
    tx = worklock.functions.claim().transact({'from': staker2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert worklock.functions.workInfo(staker2).call()[2]

    staker2_tokens = worklock_supply * 9 // 10
    assert token.functions.balanceOf(staker2).call() == 0
    assert escrow.functions.getLockedTokens(staker2, 0).call() == 0
    assert escrow.functions.getLockedTokens(staker2, 1).call() == staker2_tokens
    assert escrow.functions.getLockedTokens(staker2, token_economics.minimum_locked_periods).call() == staker2_tokens
    assert escrow.functions.getLockedTokens(staker2, token_economics.minimum_locked_periods + 1).call() == 0
    staker2_remaining_work = staker2_tokens
    assert worklock.functions.ethToWork(deposited_eth_1).call() == staker2_remaining_work
    assert worklock.functions.workToETH(staker2_remaining_work).call() == deposited_eth_1
    assert worklock.functions.getRemainingWork(staker2).call() == staker2_remaining_work
    assert token.functions.balanceOf(worklock.address).call() == worklock_supply - staker2_tokens
    tx = escrow.functions.setWorker(staker2).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    escrow_balance = token_economics.erc20_reward_supply + staker2_tokens
    assert escrow.functions.getAllTokens(staker2).call() == staker2_tokens
    assert escrow.functions.getCompletedWork(staker2).call() == 0
    tx = escrow.functions.setWindDown(True).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.stakerInfo(staker2).call()[WIND_DOWN_FIELD]

    # Burn remaining tokens in WorkLock
    tx = worklock.functions.burnUnclaimed().transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    unclaimed = worklock_supply // 20
    escrow_balance += unclaimed
    assert worklock.functions.unclaimedTokens().call() == 0
    assert token.functions.balanceOf(worklock.address).call() == worklock_supply - staker2_tokens - unclaimed
    assert escrow_balance == token.functions.balanceOf(escrow.address).call()
    assert token_economics.erc20_reward_supply + unclaimed == escrow.functions.getReservedReward().call()

    # Staker prolongs lock duration
    tx = escrow.functions.prolongStake(0, 3).transact({'from': staker2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.getLockedTokens(staker2, 0).call() == 0
    assert escrow.functions.getLockedTokens(staker2, 1).call() == staker2_tokens
    assert escrow.functions.getLockedTokens(staker2, 9).call() == staker2_tokens
    assert escrow.functions.getLockedTokens(staker2, 10).call() == 0
    assert escrow.functions.getCompletedWork(staker2).call() == 0

    # Can't claim more than once
    with pytest.raises((TransactionFailed, ValueError)):
        tx = worklock.functions.claim().transact({'from': staker2, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)
    # Can't refund without work
    with pytest.raises((TransactionFailed, ValueError)):
        tx = worklock.functions.refund().transact({'from': staker2, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)

    # Create the first preallocation escrow
    preallocation_escrow_1, _ = deploy_contract(
        'PreallocationEscrow', staking_interface_router.address, token.address, escrow.address)
    preallocation_escrow_interface_1 = testerchain.client.get_contract(
        abi=staking_interface.abi,
        address=preallocation_escrow_1.address,
        ContractFactoryClass=Contract)

    # Set and lock re-stake parameter in first preallocation escrow
    tx = preallocation_escrow_1.functions.transferOwnership(staker3).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    assert not escrow.functions.stakerInfo(preallocation_escrow_1.address).call()[DISABLE_RE_STAKE_FIELD]
    current_period = escrow.functions.getCurrentPeriod().call()
    tx = preallocation_escrow_interface_1.functions.lockReStake(current_period + 22).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    assert not escrow.functions.stakerInfo(preallocation_escrow_1.address).call()[DISABLE_RE_STAKE_FIELD]
    # Can't unlock re-stake parameter now
    with pytest.raises((TransactionFailed, ValueError)):
        tx = preallocation_escrow_interface_1.functions.setReStake(False).transact({'from': staker3})
        testerchain.wait_for_receipt(tx)

    # Deposit some tokens to the preallocation escrow and lock them
    tx = token.functions.approve(preallocation_escrow_1.address, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = preallocation_escrow_1.functions.initialDeposit(10000, 20 * 60 * 60).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    assert 10000 == token.functions.balanceOf(preallocation_escrow_1.address).call()
    assert staker3 == preallocation_escrow_1.functions.owner().call()
    assert 10000 >= preallocation_escrow_1.functions.getLockedTokens().call()
    assert 9500 <= preallocation_escrow_1.functions.getLockedTokens().call()

    # Deploy one more preallocation escrow
    staker4_tokens = 10000
    preallocation_escrow_2, _ = deploy_contract(
        'PreallocationEscrow', staking_interface_router.address, token.address, escrow.address)
    tx = preallocation_escrow_2.functions.transferOwnership(staker4).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = token.functions.approve(preallocation_escrow_2.address, staker4_tokens).transact({'from': creator})
    testerchain.wait_for_receipt(tx)
    tx = preallocation_escrow_2.functions.initialDeposit(staker4_tokens, 20 * 60 * 60).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    assert token.functions.balanceOf(staker4).call() == 0
    assert token.functions.balanceOf(preallocation_escrow_2.address).call() == staker4_tokens
    assert preallocation_escrow_2.functions.owner().call() == staker4
    assert preallocation_escrow_2.functions.getLockedTokens().call() == staker4_tokens

    # Staker's withdrawal attempt won't succeed because nothing to withdraw
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.withdraw(100).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # And can't lock because nothing to lock
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.lock(500, 2).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Check that nothing is locked
    assert 0 == escrow.functions.getLockedTokens(staker1, 0).call()
    assert 0 == escrow.functions.getLockedTokens(staker2, 0).call()
    assert 0 == escrow.functions.getLockedTokens(staker3, 0).call()
    assert 0 == escrow.functions.getLockedTokens(staker4, 0).call()
    assert 0 == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 0).call()
    assert 0 == escrow.functions.getLockedTokens(preallocation_escrow_2.address, 0).call()
    assert 0 == escrow.functions.getLockedTokens(contracts_owners[0], 0).call()

    # Staker can't deposit and lock too low value
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.deposit(1, 1).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # And can't deposit and lock too high value
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.deposit(2001, 1).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Grant access to transfer tokens
    tx = token.functions.approve(escrow.address, 10000).transact({'from': creator})
    testerchain.wait_for_receipt(tx)

    # Staker transfers some tokens to the escrow and lock them
    tx = escrow.functions.deposit(1000, 10).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setWorker(staker1).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setReStake(False).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.setWindDown(True).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.stakerInfo(staker1).call()[WIND_DOWN_FIELD]
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    escrow_balance += 1000
    assert escrow_balance == token.functions.balanceOf(escrow.address).call()
    assert 9000 == token.functions.balanceOf(staker1).call()
    assert 0 == escrow.functions.getLockedTokens(staker1, 0).call()
    assert 1000 == escrow.functions.getLockedTokens(staker1, 1).call()
    assert 1000 == escrow.functions.getLockedTokens(staker1, 10).call()
    assert 0 == escrow.functions.getLockedTokens(staker1, 11).call()

    # Wait 1 period and deposit from one more staker
    testerchain.time_travel(hours=1)
    tx = preallocation_escrow_interface_1.functions.depositAsStaker(1000, 10).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    tx = preallocation_escrow_interface_1.functions.setWorker(staker3).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    tx = preallocation_escrow_interface_1.functions.setWindDown(True).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.stakerInfo(preallocation_escrow_interface_1.address).call()[WIND_DOWN_FIELD]
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    escrow_balance += 1000
    assert 1000 == escrow.functions.getAllTokens(preallocation_escrow_1.address).call()
    assert 0 == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 0).call()
    assert 1000 == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 1).call()
    assert 1000 == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 10).call()
    assert 0 == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 11).call()
    assert escrow_balance == token.functions.balanceOf(escrow.address).call()
    assert 9000 == token.functions.balanceOf(preallocation_escrow_1.address).call()

    # Only owner can deposit tokens to the staking escrow
    with pytest.raises((TransactionFailed, ValueError)):
        tx = preallocation_escrow_interface_1.functions.depositAsStaker(1000, 5).transact({'from': creator})
        testerchain.wait_for_receipt(tx)
    # Can't deposit more than amount in the preallocation escrow
    with pytest.raises((TransactionFailed, ValueError)):
        tx = preallocation_escrow_interface_1.functions.depositAsStaker(10000, 5).transact({'from': staker3})
        testerchain.wait_for_receipt(tx)

    # Divide stakes
    tx = escrow.functions.divideStake(0, 500, 6).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.divideStake(0, 500, 9).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = preallocation_escrow_interface_1.functions.divideStake(0, 500, 6).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)

    # Confirm activity
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)

    testerchain.time_travel(hours=1)
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)

    # Turn on re-stake for staker1
    assert escrow.functions.stakerInfo(staker1).call()[DISABLE_RE_STAKE_FIELD]
    tx = escrow.functions.setReStake(True).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert not escrow.functions.stakerInfo(staker1).call()[DISABLE_RE_STAKE_FIELD]

    testerchain.time_travel(hours=1)
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)

    # Create policies
    policy_id_1 = os.urandom(16)
    number_of_periods = 5
    one_period = 60 * 60
    rate = 200
    one_node_value = number_of_periods * rate
    value = 2 * one_node_value
    current_timestamp = testerchain.w3.eth.getBlock(block_identifier='latest').timestamp
    end_timestamp = current_timestamp + (number_of_periods - 1) * one_period
    tx = policy_manager.functions.createPolicy(policy_id_1, alice1, end_timestamp, [staker1, staker2]) \
        .transact({'from': alice1, 'value': value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)

    policy_id_2 = os.urandom(16)
    tx = policy_manager.functions.createPolicy(
        policy_id_2, alice2, end_timestamp, [staker2, preallocation_escrow_1.address]) \
        .transact({'from': alice1, 'value': value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)

    policy_id_3 = os.urandom(16)
    tx = policy_manager.functions.createPolicy(
        policy_id_3, BlockchainInterface.NULL_ADDRESS, end_timestamp, [staker1, preallocation_escrow_1.address]) \
        .transact({'from': alice2, 'value': value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)

    policy_id_4 = os.urandom(16)
    tx = policy_manager.functions.createPolicy(
        policy_id_4, alice1, end_timestamp, [staker2, preallocation_escrow_1.address]) \
        .transact({'from': alice2, 'value': value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)

    policy_id_5 = os.urandom(16)
    tx = policy_manager.functions.createPolicy(
        policy_id_5, alice1, end_timestamp, [staker1, staker2]) \
        .transact({'from': alice2, 'value': value, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert 5 * value == testerchain.client.get_balance(policy_manager.address)

    # Only Alice can revoke policy
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokePolicy(policy_id_5).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)
    alice2_balance = testerchain.client.get_balance(alice2)
    tx = policy_manager.functions.revokePolicy(policy_id_5).transact({'from': alice1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    two_nodes_rate = 2 * rate
    assert 4 * value + two_nodes_rate == testerchain.client.get_balance(policy_manager.address)
    assert alice2_balance + (value - two_nodes_rate) == testerchain.client.get_balance(alice2)
    assert policy_manager.functions.policies(policy_id_5).call()[DISABLED_FIELD]

    # Can't revoke again
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokePolicy(policy_id_5).transact({'from': alice2})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_5, staker1).transact({'from': alice2})
        testerchain.wait_for_receipt(tx)

    alice1_balance = testerchain.client.get_balance(alice1)
    tx = policy_manager.functions.revokeArrangement(policy_id_2, staker2).transact({'from': alice2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    remaining_value = 3 * value + two_nodes_rate + one_node_value + rate
    assert remaining_value == testerchain.client.get_balance(policy_manager.address)
    assert alice1_balance + one_node_value - rate == testerchain.client.get_balance(alice1)
    assert not policy_manager.functions.policies(policy_id_2).call()[DISABLED_FIELD]

    # Can't revoke again
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager.functions.revokeArrangement(policy_id_2, staker2)\
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)

    # Wait, confirm activity, mint
    testerchain.time_travel(hours=1)
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)

    # Check work measurement
    completed_work = escrow.functions.getCompletedWork(staker2).call()
    assert 0 < completed_work
    assert 0 == escrow.functions.getCompletedWork(preallocation_escrow_1.address).call()
    assert 0 == escrow.functions.getCompletedWork(staker1).call()

    testerchain.time_travel(hours=1)
    tx = policy_manager.functions.revokeArrangement(policy_id_3, preallocation_escrow_1.address) \
        .transact({'from': alice2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)

    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)

    # Turn off re-stake for staker1
    assert not escrow.functions.stakerInfo(staker1).call()[DISABLE_RE_STAKE_FIELD]
    tx = escrow.functions.setReStake(False).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.stakerInfo(staker1).call()[DISABLE_RE_STAKE_FIELD]

    testerchain.time_travel(hours=1)
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)

    testerchain.time_travel(hours=1)
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)

    # Withdraw reward and refund
    testerchain.time_travel(hours=3)
    staker1_balance = testerchain.client.get_balance(staker1)
    tx = policy_manager.functions.withdraw().transact({'from': staker1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert staker1_balance < testerchain.client.get_balance(staker1)
    staker2_balance = testerchain.client.get_balance(staker2)
    tx = policy_manager.functions.withdraw().transact({'from': staker2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert staker2_balance < testerchain.client.get_balance(staker2)
    staker3_balance = testerchain.client.get_balance(staker3)
    tx = preallocation_escrow_interface_1.functions.withdrawPolicyReward(staker3).transact({'from': staker3, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert staker3_balance < testerchain.client.get_balance(staker3)

    alice1_balance = testerchain.client.get_balance(alice1)
    tx = policy_manager.functions.refund(policy_id_1).transact({'from': alice1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert alice1_balance < testerchain.client.get_balance(alice1)
    alice1_balance = testerchain.client.get_balance(alice1)
    tx = policy_manager.functions.refund(policy_id_2).transact({'from': alice1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert alice1_balance < testerchain.client.get_balance(alice1)
    alice2_balance = testerchain.client.get_balance(alice2)
    tx = policy_manager.functions.refund(policy_id_3).transact({'from': alice2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert alice2_balance == testerchain.client.get_balance(alice2)
    tx = policy_manager.functions.refund(policy_id_4).transact({'from': alice1, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    assert alice2_balance < testerchain.client.get_balance(alice2)

    # Upgrade main contracts
    escrow_secret2 = os.urandom(SECRET_LENGTH)
    policy_manager_secret2 = os.urandom(SECRET_LENGTH)
    escrow_secret2_hash = testerchain.w3.keccak(escrow_secret2)
    policy_manager_secret2_hash = testerchain.w3.keccak(policy_manager_secret2)
    escrow_v1 = escrow.functions.target().call()
    policy_manager_v1 = policy_manager.functions.target().call()
    # Creator deploys the contracts as the second versions
    escrow_v2, _ = deploy_contract(
        'StakingEscrow', token.address, *token_economics.staking_deployment_parameters, False
    )
    policy_manager_v2, _ = deploy_contract('PolicyManager', escrow.address)
    # Staker and Alice can't upgrade contracts, only owner can
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow_dispatcher.functions.upgrade(escrow_v2.address, escrow_secret, escrow_secret2_hash) \
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow_dispatcher.functions.upgrade(escrow_v2.address, escrow_secret, escrow_secret2_hash) \
            .transact({'from': staker1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager_dispatcher.functions \
            .upgrade(policy_manager_v2.address, policy_manager_secret, policy_manager_secret2_hash) \
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager_dispatcher.functions \
            .upgrade(policy_manager_v2.address, policy_manager_secret, policy_manager_secret2_hash) \
            .transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Prepare transactions to upgrade contracts
    tx1 = escrow_dispatcher.functions.upgrade(escrow_v2.address, escrow_secret, escrow_secret2_hash)\
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    tx2 = policy_manager_dispatcher.functions\
        .upgrade(policy_manager_v2.address, policy_manager_secret, policy_manager_secret2_hash)\
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    # Staker and Alice can't sign this transactions
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx1)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx1)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx2)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx2)

    # Execute transactions
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], contracts_owners[1]], tx1)
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], contracts_owners[1]], tx2)
    assert escrow_v2.address == escrow.functions.target().call()
    assert policy_manager_v2.address == policy_manager.functions.target().call()

    # Staker and Alice can't rollback contracts, only owner can
    escrow_secret3 = os.urandom(SECRET_LENGTH)
    policy_manager_secret3 = os.urandom(SECRET_LENGTH)
    escrow_secret3_hash = testerchain.w3.keccak(escrow_secret3)
    policy_manager_secret3_hash = testerchain.w3.keccak(policy_manager_secret3)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow_dispatcher.functions.rollback(escrow_secret2, escrow_secret3_hash).transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow_dispatcher.functions.rollback(escrow_secret2, escrow_secret3_hash).transact({'from': staker1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager_dispatcher.functions.rollback(policy_manager_secret2, policy_manager_secret3_hash) \
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = policy_manager_dispatcher.functions.rollback(policy_manager_secret2, policy_manager_secret3_hash) \
            .transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Prepare transactions to rollback contracts
    tx1 = escrow_dispatcher.functions.rollback(escrow_secret2, escrow_secret3_hash) \
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    tx2 = policy_manager_dispatcher.functions.rollback(policy_manager_secret2, policy_manager_secret3_hash) \
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    # Staker and Alice can't sign this transactions
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx1)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx1)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx2)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx2)

    # Execute transactions
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[1], contracts_owners[2]], tx1)
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[1], contracts_owners[2]], tx2)
    assert escrow_v1 == escrow.functions.target().call()
    assert policy_manager_v1 == policy_manager.functions.target().call()

    # Upgrade the preallocation escrow library
    # Deploy the same contract as the second version
    staking_interface_v2, _ = deploy_contract(
        'StakingInterface', token.address, escrow.address, policy_manager.address)
    router_secret2 = os.urandom(SECRET_LENGTH)
    router_secret2_hash = testerchain.w3.keccak(router_secret2)
    # Staker and Alice can't upgrade library, only owner can
    with pytest.raises((TransactionFailed, ValueError)):
        tx = staking_interface_router.functions \
            .upgrade(staking_interface_v2.address, router_secret, router_secret2_hash) \
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = staking_interface_router.functions \
            .upgrade(staking_interface_v2.address, router_secret, router_secret2_hash) \
            .transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Prepare transactions to upgrade library
    tx = staking_interface_router.functions \
        .upgrade(staking_interface_v2.address, router_secret, router_secret2_hash)\
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    # Staker and Alice can't sign this transactions
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx)

    # Execute transactions
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[1], contracts_owners[2]], tx)
    assert staking_interface_v2.address == staking_interface_router.functions.target().call()

    # Slash stakers
    # Confirm activity for two periods
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)
    tx = escrow.functions.confirmActivity().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.confirmActivity().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    testerchain.time_travel(hours=1)

    # Can't slash directly using the escrow contract
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.slashStaker(staker1, 100, alice1, 10).transact()
        testerchain.wait_for_receipt(tx)

    # Slash part of the free amount of tokens
    current_period = escrow.functions.getCurrentPeriod().call()
    tokens_amount = escrow.functions.getAllTokens(staker1).call()
    previous_lock = escrow.functions.getLockedTokensInPast(staker1, 1).call()
    lock = escrow.functions.getLockedTokens(staker1, 0).call()
    next_lock = escrow.functions.getLockedTokens(staker1, 1).call()
    total_previous_lock = escrow.functions.lockedPerPeriod(current_period - 1).call()
    total_lock = escrow.functions.lockedPerPeriod(current_period).call()
    alice1_balance = token.functions.balanceOf(alice1).call()

    algorithm_sha256, base_penalty, *coefficients = token_economics.slashing_deployment_parameters
    penalty_history_coefficient, percentage_penalty_coefficient, reward_coefficient = coefficients

    data_hash, slashing_args = generate_args_for_slashing(mock_ursula_reencrypts, ursula1_with_stamp)
    assert not adjudicator.functions.evaluatedCFrags(data_hash).call()
    tx = adjudicator.functions.evaluateCFrag(*slashing_args).transact({'from': alice1})
    testerchain.wait_for_receipt(tx)
    assert adjudicator.functions.evaluatedCFrags(data_hash).call()
    assert tokens_amount - base_penalty == escrow.functions.getAllTokens(staker1).call()
    assert previous_lock == escrow.functions.getLockedTokensInPast(staker1, 1).call()
    assert lock == escrow.functions.getLockedTokens(staker1, 0).call()
    assert next_lock == escrow.functions.getLockedTokens(staker1, 1).call()
    assert total_previous_lock == escrow.functions.lockedPerPeriod(current_period - 1).call()
    assert total_lock == escrow.functions.lockedPerPeriod(current_period).call()
    assert 0 == escrow.functions.lockedPerPeriod(current_period + 1).call()
    assert alice1_balance + base_penalty / reward_coefficient == token.functions.balanceOf(alice1).call()

    # Slash part of the one sub stake
    tokens_amount = escrow.functions.getAllTokens(staker2).call()
    unlocked_amount = tokens_amount - escrow.functions.getLockedTokens(staker2, 0).call()
    tx = escrow.functions.withdraw(unlocked_amount).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    previous_lock = escrow.functions.getLockedTokensInPast(staker2, 1).call()
    lock = escrow.functions.getLockedTokens(staker2, 0).call()
    next_lock = escrow.functions.getLockedTokens(staker2, 1).call()
    data_hash, slashing_args = generate_args_for_slashing(mock_ursula_reencrypts, ursula2_with_stamp)
    assert not adjudicator.functions.evaluatedCFrags(data_hash).call()
    tx = adjudicator.functions.evaluateCFrag(*slashing_args).transact({'from': alice1})
    testerchain.wait_for_receipt(tx)
    assert adjudicator.functions.evaluatedCFrags(data_hash).call()
    assert lock - base_penalty == escrow.functions.getAllTokens(staker2).call()
    assert previous_lock == escrow.functions.getLockedTokensInPast(staker2, 1).call()
    assert lock - base_penalty == escrow.functions.getLockedTokens(staker2, 0).call()
    assert next_lock - base_penalty == escrow.functions.getLockedTokens(staker2, 1).call()
    assert total_previous_lock == escrow.functions.lockedPerPeriod(current_period - 1).call()
    assert total_lock - base_penalty == escrow.functions.lockedPerPeriod(current_period).call()
    assert 0 == escrow.functions.lockedPerPeriod(current_period + 1).call()
    assert alice1_balance + base_penalty == token.functions.balanceOf(alice1).call()

    # Slash preallocation escrow
    tokens_amount = escrow.functions.getAllTokens(preallocation_escrow_1.address).call()
    previous_lock = escrow.functions.getLockedTokensInPast(preallocation_escrow_1.address, 1).call()
    lock = escrow.functions.getLockedTokens(preallocation_escrow_1.address, 0).call()
    next_lock = escrow.functions.getLockedTokens(preallocation_escrow_1.address, 1).call()
    total_previous_lock = escrow.functions.lockedPerPeriod(current_period - 1).call()
    total_lock = escrow.functions.lockedPerPeriod(current_period).call()
    alice1_balance = token.functions.balanceOf(alice1).call()

    data_hash, slashing_args = generate_args_for_slashing(mock_ursula_reencrypts, ursula3_with_stamp)
    assert not adjudicator.functions.evaluatedCFrags(data_hash).call()
    tx = adjudicator.functions.evaluateCFrag(*slashing_args).transact({'from': alice1})
    testerchain.wait_for_receipt(tx)
    assert adjudicator.functions.evaluatedCFrags(data_hash).call()
    assert tokens_amount - base_penalty == escrow.functions.getAllTokens(preallocation_escrow_1.address).call()
    assert previous_lock == escrow.functions.getLockedTokensInPast(preallocation_escrow_1.address, 1).call()
    assert lock - base_penalty == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 0).call()
    assert next_lock - base_penalty == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 1).call()
    assert total_previous_lock == escrow.functions.lockedPerPeriod(current_period - 1).call()
    assert total_lock - base_penalty == escrow.functions.lockedPerPeriod(current_period).call()
    assert 0 == escrow.functions.lockedPerPeriod(current_period + 1).call()
    assert alice1_balance + base_penalty / reward_coefficient == token.functions.balanceOf(alice1).call()

    # Upgrade the adjudicator
    # Deploy the same contract as the second version
    adjudicator_v1 = adjudicator.functions.target().call()
    adjudicator_v2, _ = deploy_contract(
        'Adjudicator',
        escrow.address,
        *token_economics.slashing_deployment_parameters)
    adjudicator_secret2 = os.urandom(SECRET_LENGTH)
    adjudicator_secret2_hash = testerchain.w3.keccak(adjudicator_secret2)
    # Staker and Alice can't upgrade library, only owner can
    with pytest.raises((TransactionFailed, ValueError)):
        tx = adjudicator_dispatcher.functions \
            .upgrade(adjudicator_v2.address, adjudicator_secret, adjudicator_secret2_hash) \
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = adjudicator_dispatcher.functions \
            .upgrade(adjudicator_v2.address, adjudicator_secret, adjudicator_secret2_hash) \
            .transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Prepare transactions to upgrade contracts
    tx = adjudicator_dispatcher.functions\
        .upgrade(adjudicator_v2.address, adjudicator_secret, adjudicator_secret2_hash) \
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    # Staker and Alice can't sign this transactions
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx)

    # Execute transactions
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], contracts_owners[1]], tx)
    assert adjudicator_v2.address == adjudicator.functions.target().call()

    # Staker and Alice can't rollback contract, only owner can
    adjudicator_secret3 = os.urandom(SECRET_LENGTH)
    adjudicator_secret3_hash = testerchain.w3.keccak(adjudicator_secret3)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = adjudicator_dispatcher.functions.rollback(adjudicator_secret2, adjudicator_secret3_hash)\
            .transact({'from': alice1})
        testerchain.wait_for_receipt(tx)
    with pytest.raises((TransactionFailed, ValueError)):
        tx = adjudicator_dispatcher.functions.rollback(adjudicator_secret2, adjudicator_secret3_hash)\
            .transact({'from': staker1})
        testerchain.wait_for_receipt(tx)

    # Prepare transactions to rollback contracts
    tx = adjudicator_dispatcher.functions.rollback(adjudicator_secret2, adjudicator_secret3_hash) \
        .buildTransaction({'from': multisig.address, 'gasPrice': 0})
    # Staker and Alice can't sign this transactions
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], staker1], tx)
    with pytest.raises((TransactionFailed, ValueError)):
        execute_multisig_transaction(testerchain, multisig, [contracts_owners[0], alice1], tx)

    # Execute transactions
    execute_multisig_transaction(testerchain, multisig, [contracts_owners[1], contracts_owners[2]], tx)
    assert adjudicator_v1 == adjudicator.functions.target().call()

    # Slash two sub stakes
    tokens_amount = escrow.functions.getAllTokens(staker1).call()
    unlocked_amount = tokens_amount - escrow.functions.getLockedTokens(staker1, 0).call()
    tx = escrow.functions.withdraw(unlocked_amount).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    previous_lock = escrow.functions.getLockedTokensInPast(staker1, 1).call()
    lock = escrow.functions.getLockedTokens(staker1, 0).call()
    next_lock = escrow.functions.getLockedTokens(staker1, 1).call()
    total_lock = escrow.functions.lockedPerPeriod(current_period).call()
    alice2_balance = token.functions.balanceOf(alice2).call()
    data_hash, slashing_args = generate_args_for_slashing(mock_ursula_reencrypts, ursula1_with_stamp)
    assert not adjudicator.functions.evaluatedCFrags(data_hash).call()
    tx = adjudicator.functions.evaluateCFrag(*slashing_args).transact({'from': alice2})
    testerchain.wait_for_receipt(tx)
    assert adjudicator.functions.evaluatedCFrags(data_hash).call()
    data_hash, slashing_args = generate_args_for_slashing(mock_ursula_reencrypts, ursula1_with_stamp)
    assert not adjudicator.functions.evaluatedCFrags(data_hash).call()
    tx = adjudicator.functions.evaluateCFrag(*slashing_args).transact({'from': alice2})
    testerchain.wait_for_receipt(tx)
    assert adjudicator.functions.evaluatedCFrags(data_hash).call()
    penalty = (2 * base_penalty + 3 * penalty_history_coefficient)
    assert lock - penalty == escrow.functions.getAllTokens(staker1).call()
    assert previous_lock == escrow.functions.getLockedTokensInPast(staker1, 1).call()
    assert lock - penalty == escrow.functions.getLockedTokens(staker1, 0).call()
    assert next_lock - (penalty - (lock - next_lock)) == escrow.functions.getLockedTokens(staker1, 1).call()
    assert total_previous_lock == escrow.functions.lockedPerPeriod(current_period - 1).call()
    assert total_lock - penalty == escrow.functions.lockedPerPeriod(current_period).call()
    assert 0 == escrow.functions.lockedPerPeriod(current_period + 1).call()
    assert alice2_balance + penalty / reward_coefficient == token.functions.balanceOf(alice2).call()

    # Can't prolong stake by too low duration
    with pytest.raises((TransactionFailed, ValueError)):
        tx = escrow.functions.prolongStake(0, 1).transact({'from': staker2, 'gas_price': 0})
        testerchain.wait_for_receipt(tx)

    # Unlock and withdraw all tokens
    for index in range(9):
        tx = escrow.functions.confirmActivity().transact({'from': staker1})
        testerchain.wait_for_receipt(tx)
        tx = escrow.functions.confirmActivity().transact({'from': staker2})
        testerchain.wait_for_receipt(tx)
        tx = escrow.functions.confirmActivity().transact({'from': staker3})
        testerchain.wait_for_receipt(tx)
        testerchain.time_travel(hours=1)

    # Can't unlock re-stake parameter yet
    with pytest.raises((TransactionFailed, ValueError)):
        tx = preallocation_escrow_interface_1.functions.setReStake(False).transact({'from': staker3})
        testerchain.wait_for_receipt(tx)

    testerchain.time_travel(hours=1)
    # Now can turn off re-stake
    tx = preallocation_escrow_interface_1.functions.setReStake(False).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    assert escrow.functions.stakerInfo(preallocation_escrow_1.address).call()[DISABLE_RE_STAKE_FIELD]

    tx = escrow.functions.mint().transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tx = escrow.functions.mint().transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tx = preallocation_escrow_interface_1.functions.mint().transact({'from': staker3})
    testerchain.wait_for_receipt(tx)

    assert 0 == escrow.functions.getLockedTokens(staker1, 0).call()
    assert 0 == escrow.functions.getLockedTokens(staker2, 0).call()
    assert 0 == escrow.functions.getLockedTokens(staker3, 0).call()
    assert 0 == escrow.functions.getLockedTokens(staker4, 0).call()
    assert 0 == escrow.functions.getLockedTokens(preallocation_escrow_1.address, 0).call()
    assert 0 == escrow.functions.getLockedTokens(preallocation_escrow_2.address, 0).call()

    staker1_balance = token.functions.balanceOf(staker1).call()
    staker2_balance = token.functions.balanceOf(staker2).call()
    preallocation_escrow_1_balance = token.functions.balanceOf(preallocation_escrow_1.address).call()
    tokens_amount = escrow.functions.getAllTokens(staker1).call()
    tx = escrow.functions.withdraw(tokens_amount).transact({'from': staker1})
    testerchain.wait_for_receipt(tx)
    tokens_amount = escrow.functions.getAllTokens(staker2).call()
    tx = escrow.functions.withdraw(tokens_amount).transact({'from': staker2})
    testerchain.wait_for_receipt(tx)
    tokens_amount = escrow.functions.getAllTokens(preallocation_escrow_1.address).call()
    tx = preallocation_escrow_interface_1.functions.withdrawAsStaker(tokens_amount).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    assert staker1_balance < token.functions.balanceOf(staker1).call()
    assert staker2_balance < token.functions.balanceOf(staker2).call()
    assert preallocation_escrow_1_balance < token.functions.balanceOf(preallocation_escrow_1.address).call()

    # Unlock and withdraw all tokens in PreallocationEscrow
    testerchain.time_travel(hours=1)
    assert 0 == preallocation_escrow_1.functions.getLockedTokens().call()
    assert 0 == preallocation_escrow_2.functions.getLockedTokens().call()
    staker3_balance = token.functions.balanceOf(staker3).call()
    staker4_balance = token.functions.balanceOf(staker4).call()
    tokens_amount = token.functions.balanceOf(preallocation_escrow_1.address).call()
    tx = preallocation_escrow_1.functions.withdrawTokens(tokens_amount).transact({'from': staker3})
    testerchain.wait_for_receipt(tx)
    tokens_amount = token.functions.balanceOf(preallocation_escrow_2.address).call()
    tx = preallocation_escrow_2.functions.withdrawTokens(tokens_amount).transact({'from': staker4})
    testerchain.wait_for_receipt(tx)
    assert staker3_balance < token.functions.balanceOf(staker3).call()
    assert staker4_balance < token.functions.balanceOf(staker4).call()

    # Partial refund for staker
    new_completed_work = escrow.functions.getCompletedWork(staker2).call()
    assert completed_work < new_completed_work
    remaining_work = worklock.functions.getRemainingWork(staker2).call()
    assert 0 < remaining_work
    assert deposited_eth_1 == worklock.functions.workInfo(staker2).call()[0]
    staker2_balance = testerchain.w3.eth.getBalance(staker2)
    tx = worklock.functions.refund().transact({'from': staker2, 'gas_price': 0})
    testerchain.wait_for_receipt(tx)
    refund = worklock.functions.workToETH(new_completed_work).call()
    assert deposited_eth_1 - refund == worklock.functions.workInfo(staker2).call()[0]
    assert refund + staker2_balance == testerchain.w3.eth.getBalance(staker2)
    assert deposited_eth_1 + deposited_eth_2 - refund == testerchain.w3.eth.getBalance(worklock.address)
    assert 0 == escrow.functions.getCompletedWork(staker1).call()
    assert 0 == escrow.functions.getCompletedWork(preallocation_escrow_1.address).call()
