# Copyright (c) The Libra Core Contributors
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
from threading import Thread
from time import time
import pytest
from _pytest.monkeypatch import MonkeyPatch
from libra.jsonrpc import Client as LibraClient

from offchainapi.business import VASPInfo, BusinessContext, BusinessValidationFailure
from offchainapi.core import Vasp
from offchainapi.crypto import ComplianceKey
from offchainapi.libra_address import LibraAddress
from offchainapi.payment import (
    PaymentAction,
    PaymentActor,
    PaymentObject,
    KYCData,
    StatusObject,
)
from offchainapi.payment_logic import PaymentCommand
from offchainapi.status_logic import Status

from tests.wallet_tests.libra_client_sdk_mocks import AccountMocker
from tests.wallet_tests.resources.seeds.one_user_seeder import OneUser
from wallet import OnchainWallet
from wallet.storage import (
    db_session,
    get_account_transaction_ids,
    get_single_transaction,
    get_reference_id_from_transaction_id,
    add_transaction,
    get_new_reference_id,
)
from wallet.types import TransactionType, TransactionStatus
from wallet.services.account import generate_new_subaddress, get_account_balance
from libra_utils.types.currencies import LibraCurrency
import offchain.offchain_business
from offchain.offchain_business import (
    LRWOffChainBusinessContext,
    get_compliance_key_on_chain,
)
from offchain import LRWSimpleVASPInfo


logger = logging.getLogger(name="test_offchain")


PeerA_addr = LibraAddress.from_hex("c77e1ae3e4a136f070bfcce807747daf")
PeerB_addr = LibraAddress.from_bytes(b"B" * 16)
peer_address = {
    PeerA_addr.as_str(): "http://localhost:8091",
    PeerB_addr.as_str(): "http://localhost:8092",
}

peer_keys = {
    PeerA_addr.as_str(): ComplianceKey.generate(),
    PeerB_addr.as_str(): ComplianceKey.generate(),
}


class SimpleVASPInfo(VASPInfo):
    def __init__(self, my_addr):
        self.my_addr = my_addr

    def get_base_url(self):
        return peer_address[self.my_addr.as_str()]

    def get_peer_base_url(self, other_addr):
        assert other_addr.as_str() in peer_address
        return peer_address[other_addr.as_str()]

    def get_peer_compliance_verification_key(self, other_addr):
        key = ComplianceKey.from_str(peer_keys[other_addr].export_pub())
        assert not key._key.has_private
        return key

    def get_my_compliance_signature_key(self, my_addr):
        return peer_keys[my_addr]


def start_thread_main(vasp, loop):
    # Initialize the VASP services.
    vasp.start_services()

    try:
        # Start the loop
        loop.run_forever()
    finally:
        # Do clean up
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

    print("VASP loop exit...")


def make_VASPa(Peer_addr, port, reliable=True):
    VASPx = Vasp(
        Peer_addr,
        host="localhost",
        port=port,
        business_context=LRWOffChainBusinessContext(Peer_addr, reliable=reliable),
        info_context=SimpleVASPInfo(Peer_addr),
        database={},
    )

    loop = asyncio.new_event_loop()
    VASPx.set_loop(loop)

    # Create and launch a thread with the VASP event loop
    t = Thread(target=start_thread_main, args=(VASPx, loop))
    t.start()
    print(f"Start Node {port}")

    # Block until the event loop in the thread is running.
    VASPx.wait_for_start()

    return (VASPx, loop, t)


def make_VASPb(Peer_addr, port, reliable=True):
    VASPx = Vasp(
        Peer_addr,
        host="localhost",
        port=port,
        business_context=TestBusinessContext(Peer_addr, reliable=reliable),
        info_context=SimpleVASPInfo(Peer_addr),
        database={},
    )

    loop = asyncio.new_event_loop()
    VASPx.set_loop(loop)

    # Create and launch a thread with the VASP event loop
    t = Thread(target=start_thread_main, args=(VASPx, loop))
    t.start()
    print(f"Start Node {port}")

    # Block until the event loop in the thread is running.
    VASPx.wait_for_start()

    return (VASPx, loop, t)


@pytest.fixture
def send_transaction_mock(monkeypatch):
    def send_transaction_travel_rule_mock(
        self,
        amount: int,
        currency: LibraCurrency,
        source_sub_address: str,
        dest_vasp_address: str,
        dest_sub_address: str,
        off_chain_reference_id: str,
        metadata_signature: bytes,
    ) -> (int, int):
        return 0, 0

    monkeypatch.setattr(
        OnchainWallet, "send_transaction_travel_rule", send_transaction_travel_rule_mock
    )


@pytest.fixture
def get_peer_compliance_key_mock(monkeypatch):
    def get_peer_key_mock(self, other_addr: str) -> ComplianceKey:
        libra_address = LibraAddress.from_encoded_str(
            other_addr
        ).get_onchain_address_hex()
        if libra_address == PeerA_addr.get_onchain_address_hex():
            return ComplianceKey.from_str(peer_keys[PeerA_addr.as_str()].export_pub())
        else:
            return ComplianceKey.from_str(peer_keys[PeerB_addr.as_str()].export_pub())

    monkeypatch.setattr(
        LRWSimpleVASPInfo, "get_peer_compliance_verification_key", get_peer_key_mock
    )


@pytest.fixture
def get_compliance_key_mock(monkeypatch):
    def get_key_mock(addr: str) -> ComplianceKey:
        address = LibraAddress.from_hex(addr)
        return ComplianceKey.from_str(
            peer_keys[address.get_onchain_encoded_str()].export_pub()
        )

    monkeypatch.setattr(
        offchain.offchain_business, "get_compliance_key_on_chain", get_key_mock
    )


@pytest.fixture
def get_peer_base_url_mock(monkeypatch):
    def get_peer_url_mock(self, other_addr: LibraAddress) -> str:
        other_vasp_addr = other_addr.get_onchain_address_hex()
        logger.info(f"==========Get peer base url: {other_vasp_addr}")
        if other_vasp_addr == PeerA_addr.get_onchain_address_hex():
            return "http://localhost:8091"
        else:
            return "http://localhost:8092"

    monkeypatch.setattr(LRWSimpleVASPInfo, "get_peer_base_url", get_peer_url_mock)


async def test_offchain(
    caplog,
    monkeypatch: MonkeyPatch,
    get_peer_compliance_key_mock,
    get_compliance_key_mock,
    get_peer_base_url_mock,
    send_transaction_mock,
    messages_num=1,
):
    caplog.set_level(logging.DEBUG)

    VASPa, loopA, tA = make_VASPa(PeerA_addr, port=8091)
    VASPb, loopB, tB = make_VASPb(PeerB_addr, port=8092)

    account_mocker = AccountMocker()
    monkeypatch.setattr(LibraClient, "get_account", account_mocker.get_account)

    user = OneUser.run(
        db_session,
        account_amount=2000 * 1_000_000,
        account_currency=LibraCurrency.Coin1,
    )
    account_id = user.account_id
    amount = 1500 * 1_000_000
    payment_type = TransactionType.OFFCHAIN
    currency = LibraCurrency.Coin1
    source_vasp_address = PeerA_addr.get_onchain_address_hex()
    source_subaddress = generate_new_subaddress(account_id=account_id)
    destination_vasp_address = PeerB_addr.get_onchain_address_hex()
    destination_subaddress = (b"b" * 8).hex()

    sub_a = LibraAddress.from_hex(
        PeerA_addr.get_onchain_address_hex(), source_subaddress
    ).as_str()
    sub_b = LibraAddress.from_hex(
        PeerB_addr.get_onchain_address_hex(), destination_subaddress
    ).as_str()

    logger.info(f"==========Address A full: {sub_a}, onchain: {PeerA_addr.as_str()}")
    logger.info(f"==========Address B full: {sub_b}, onchain: {PeerB_addr.as_str()}")
    logger.info(
        f"==========COMP A: {peer_keys[PeerA_addr.as_str()].export_full()}, PUBLICA: {peer_keys[PeerA_addr.as_str()].export_pub()}"
    )
    logger.info(
        f"==========COMP B: {peer_keys[PeerB_addr.as_str()].export_full()}, PUBLICA: {peer_keys[PeerB_addr.as_str()].export_pub()}"
    )

    tx = add_transaction(
        amount=amount,
        currency=currency,
        payment_type=payment_type,
        status=TransactionStatus.OFF_CHAIN_STARTED,
        source_id=account_id,
        source_address=source_vasp_address,
        source_subaddress=source_subaddress,
        destination_id=None,
        destination_address=destination_vasp_address,
        destination_subaddress=destination_subaddress,
        reference_id=get_new_reference_id(),
    )
    assert tx.id in get_account_transaction_ids(account_id)
    assert tx.source_id == account_id
    assert tx.amount == amount
    assert tx.destination_address == destination_vasp_address
    assert tx.destination_subaddress == destination_subaddress

    sender = PaymentActor(sub_a, StatusObject(Status.needs_kyc_data), [])
    receiver = PaymentActor(sub_b, StatusObject(Status.none), [])

    action = PaymentAction(amount, currency, "charge", int(time()))
    reference_id = get_reference_id_from_transaction_id(tx.id)
    payment = PaymentObject(
        sender=sender,
        receiver=receiver,
        reference_id=reference_id,
        original_payment_reference_id=None,
        description=None,
        action=action,
    )
    cmd = PaymentCommand(payment)
    result = VASPa.new_command(PeerB_addr.get_onchain(), cmd).result()
    assert result

    num_tries, delay = 20, 1.0
    while num_tries > 1:
        payment = VASPa.get_payment_by_ref(reference_id)
        if (
            payment.sender.status.as_status() != Status.ready_for_settlement
            and payment.receiver.status.as_status() != Status.ready_for_settlement
        ):
            await asyncio.sleep(delay)
            num_tries -= 1
        else:
            num_tries = 0

    assert (
        VASPa.get_payment_by_ref(reference_id).sender.status.as_status()
        == Status.ready_for_settlement
    )
    assert (
        VASPb.get_payment_by_ref(reference_id).receiver.status.as_status()
        == Status.ready_for_settlement
    )

    tx = get_single_transaction(tx.id)
    assert tx.status == TransactionStatus.COMPLETED

    # Close the loops
    VASPa.close()
    VASPb.close()


class TestBusinessContext(BusinessContext):
    __test__ = False

    def __init__(self, my_addr, reliable=True):
        self.my_addr = my_addr

        # Option to make the contect unreliable to
        # help test error handling.
        self.reliable = reliable
        self.reliable_count = 0

    def cause_error(self):
        self.reliable_count += 1
        fail = self.reliable_count % 5 == 0
        if fail:
            e = BusinessValidationFailure(
                "Artifical error caused for " "testing error handling"
            )
            raise e

    def open_channel_to(self, other_vasp_info):
        logger.info("~~~~~~~~~~~~~~~~open_channel_to~~~~~~~~~~~")
        return True

    # ----- Actors -----

    def is_sender(self, payment, ctx=None):
        myself = self.my_addr.as_str()
        return myself == payment.sender.get_onchain_address_encoded_str()

    def is_recipient(self, payment, ctx=None):
        return not self.is_sender(payment)

    async def check_account_existence(self, payment, ctx=None):
        logger.info("~~~~~~~~~~~~~~~~check_account_existence~~~~~~~~~~~")
        return True

    # ----- VASP Signature -----

    def validate_recipient_signature(self, payment, ctx=None):
        logger.info("~~~~~~~~~~~~~~~~validate_recipient_signature~~~~~~~~~~~")

        if "recipient_signature" in payment.data:
            try:
                logger.info("----------valid recipient signature=======")
                libra_address_bytes = LibraAddress.from_encoded_str(
                    payment.sender.address
                ).onchain_address_bytes
                sig = payment.data["recipient_signature"]
                recipient_addr = payment.receiver.get_onchain_address_encoded_str()
                peer_keys[recipient_addr].verify_dual_attestation_data(
                    payment.reference_id,
                    libra_address_bytes,
                    payment.action.amount,
                    bytes.fromhex(sig),
                )

                if not self.reliable:
                    self.cause_error()

                logger.info("----------valid recipient signature end=======")
                return
            except Exception as e:
                raise BusinessValidationFailure(
                    f"Could not validate recipient signature: {e}"
                )

        sig = payment.data.get("recipient_signature", "Not present")
        raise BusinessValidationFailure(f"Invalid signature: {sig}")

    async def get_recipient_signature(self, payment, ctx=None):
        logger.info(
            f"~~~~~~~~~~~~~~~~get_recipient_signature~~~~~~~~~~~{payment.sender.address}"
        )
        myself = self.my_addr.as_str()
        key = peer_keys.get(myself)
        libra_addres = LibraAddress.from_encoded_str(
            payment.sender.address
        ).onchain_address_bytes
        signed = key.sign_dual_attestation_data(
            payment.reference_id, libra_addres, payment.action.amount
        )
        logger.info(f"this SIGNATURE {bytes.hex(signed)}")
        return bytes.hex(signed)

    # ----- KYC/Compliance checks -----

    async def next_kyc_to_provide(self, payment, ctx=None):
        logger.info("~~~~~~~~~~~~~~~~next_kyc_to_provide~~~~~~~~~~~")
        role = ["receiver", "sender"][self.is_sender(payment)]
        other_role = ["sender", "receiver"][self.is_sender(payment)]
        own_actor = payment.data[role]
        other_actor = payment.data[other_role]
        kyc_data = set()

        if "kyc_data" not in own_actor:
            kyc_data.add(Status.needs_kyc_data)

        if (
            "additional_kyc_data" not in own_actor
            and other_actor.status.as_status() == Status.soft_match
        ):
            kyc_data.add(Status.soft_match)

        if role == "receiver":
            if "recipient_signature" not in payment:
                kyc_data.add(Status.needs_recipient_signature)
        logger.info("~~~~~~~~~~~~~~~~next_kyc_level_to_provide reached end~~~~~~~~~~~")
        return kyc_data

    async def next_kyc_level_to_request(self, payment, ctx=None):
        logger.info("~~~~~~~~~~~~~~~~next_kyc_level_to_request~~~~~~~~~~~")
        other_role = ["sender", "receiver"][self.is_sender(payment)]
        other_actor = payment.data[other_role]

        if "kyc_data" not in other_actor:
            return Status.needs_kyc_data

        if "additional_kyc_data" not in other_actor:
            return Status.soft_match

        if other_role == "receiver" and "recipient_signature" not in payment:
            return Status.needs_recipient_signature
        logger.info("~~~~~~~~~~~~~~~~next_kyc_level_to_request reached end~~~~~~~~~~~")
        return Status.none

    async def get_extended_kyc(self, payment, ctx=None):
        """ Returns the extended KYC information for this payment.
            In the format: (kyc_data, kyc_signature, kyc_certificate), where
            all fields are of type str.
            Can raise:
                   BusinessNotAuthorized.
        """
        logger.info("~~~~~~~~~~~~~~~~get_extended_kyc~~~~~~~~~~~")
        return KYCData(
            {"payload_type": "KYC_DATA", "payload_version": 1, "type": "individual",}
        )

    async def get_additional_kyc(self, payment, ctx=None):
        """ Provides the additional KYC information for this payment.
            The additional information is requested or may be provided in case
            of a `soft_match` state from the other VASP indicating more
            information is required to disambiguate an individual.
            Args:
                payment (PaymentCommand): The concerned payment.
            Raises:
                   BusinessNotAuthorized: If the other VASP is not authorized to
                    receive extended KYC data for this payment.
            Returns:
                KYCData: Returns the extended KYC information for
                this payment.
        """
        logger.info("~~~~~~~~~~~~~~~~get_additional_kyc~~~~~~~~~~~")
        return KYCData(
            {
                "payload_type": "KYC_DATA",
                "payload_version": 1,
                "type": "individual",
                "given_name": "John",
                "surname": "Smith",
                "dob": "1973-07-08",
            }
        )

    # ----- Settlement -----

    async def ready_for_settlement(self, payment, ctx=None):
        logger.info("~~~~~~~~~~~~~~~~ready_for_settlement~~~~~~~~~~~")
        if not self.reliable:
            self.cause_error()

        return (await self.next_kyc_level_to_request(payment)) is Status.none
