import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from logging import getLogger
from typing import Any, Optional, Sequence

from django.db.models import Min

from celery.exceptions import SoftTimeLimitExceeded
from eth_typing import ChecksumAddress
from requests import Timeout
from safe_eth.eth import EthereumClient
from web3.exceptions import Web3RPCError

from ..services import IndexingException, IndexService, IndexServiceProvider

logger = getLogger(__name__)


class FindRelevantElementsException(IndexingException):
    pass


class EthereumIndexer(ABC):
    """
    This service allows indexing of Ethereum blockchain.
    `database_field` should be defined with the field used to store the current block number for a monitored address
    `find_relevant_elements` elements should be defined with the query to get the relevant txs/events/etc.
    `process_elements` defines what happens with elements found
    So the flow would be `start()` -> `process_addresses` -> `find_revelant_elements` -> `process_elements` ->
    `process_element`
    """

    def __init__(
        self,
        ethereum_client: EthereumClient,
        confirmations: int = 0,
        block_process_limit: int = 2000,
        block_process_limit_max: int = 0,
        blocks_to_reindex_again: int = 0,
        updated_blocks_behind: int = 20,
        query_chunk_size: Optional[int] = 1_000,
        block_auto_process_limit: bool = True,
        **kwargs,
    ):
        """
        :param ethereum_client:
        :param confirmations: Don't index last `confirmations` blocks to prevent from reorgs
        :param block_process_limit: Number of blocks to scan at a time for relevant data. `0` == `No limit`
        :param block_process_limit_max: Maximum number of blocks to scan at a time for relevant data. `0` == `No limit`
        :param blocks_to_reindex_again: Number of blocks to reindex every time the indexer runs, in case something
            was missed.
        :param updated_blocks_behind: Number of blocks scanned for an address that can be behind and
            still be considered as almost updated. For example, if `updated_blocks_behind` is 100,
            `current block number` is 200, and last scan for an address was stopped on block 150, address
            is almost updated (200 - 100 < 150). Almost updated addresses are prioritized
        :param query_chunk_size: Number of addresses to query for relevant data in the same request. By testing,
            it seems that `5000` can be a good value (for `eth_getLogs`). If `0`, process all together
        :param block_auto_process_limit: Auto increase or decrease the `block_process_limit`
            based on congestion algorithm
        """
        self.ethereum_client = ethereum_client
        self.index_service: IndexService = IndexServiceProvider()
        self.index_service.ethereum_client = (
            self.ethereum_client
        )  # Use tracing ethereum client
        self.confirmations = confirmations
        self.initial_block_process_limit = block_process_limit
        self.block_process_limit = block_process_limit
        self.block_process_limit_max = block_process_limit_max
        self.blocks_to_reindex_again = blocks_to_reindex_again
        self.updated_blocks_behind = updated_blocks_behind
        self.query_chunk_size = query_chunk_size
        self.block_auto_process_limit = block_auto_process_limit

    @property
    @abstractmethod
    def database_field(self):
        """
        :return: Database field for `database_queryset` to store scan status
        """

    @property
    @abstractmethod
    def database_queryset(self):
        """
        :return: Queryset of objects being scanned
        """

    @abstractmethod
    def find_relevant_elements(
        self,
        addresses: set[ChecksumAddress],
        from_block_number: int,
        to_block_number: int,
        current_block_number: Optional[int] = None,
    ) -> Sequence[Any]:
        """
        Find blockchain relevant elements for the `addresses`

        :param addresses:
        :param from_block_number
        :param to_block_number
        :param current_block_number:
        :return: Set of relevant elements
        """

    def process_element(self, element: Any) -> list[Any]:
        """
        Process provided `element` to retrieve relevant data (internal txs, events...)

        :param element:
        :return:
        """
        raise NotImplementedError

    def process_elements(self, elements: Sequence[Any]) -> Sequence[Any]:
        processed_objects = []
        for i, element in enumerate(elements):
            logger.info(
                "%s: Processing element %d/%d",
                i + 1,
                self.__class__.__name__,
                len(list(elements)),
            )
            processed_objects.append(self.process_element(element))
        # processed_objects = [self.process_element(element) for element in elements]
        return [item for sublist in processed_objects for item in sublist]

    def get_block_numbers_for_search(
        self,
        addresses: set[ChecksumAddress],
        current_block_number: Optional[int] = None,
    ) -> Optional[tuple[int, int]]:
        """
        :param addresses:
        :param current_block_number: To prevent fetching it again
        :return: Minimum common `from_block_number` and `to_block_number` for search of relevant `tx hashes`
        """
        current_block_number = (
            current_block_number or self.ethereum_client.current_block_number
        )

        from_block_number = self.get_from_block_number(addresses)
        if from_block_number is None:  # Empty queryset
            return None

        if (current_block_number - from_block_number) < self.confirmations:
            return  # We don't want problems with reorgs

        to_block_number = self.get_to_block_number(
            from_block_number, current_block_number
        )

        # Reindex again when it's almost synced to prevent reorg/missing elements issues
        if (
            from_block_number + self.block_process_limit
            > current_block_number - self.confirmations
        ):
            # Check if there's room on `block_process_limit` to reindex some blocks
            blocks_to_reindex = min(
                self.block_process_limit - (to_block_number - from_block_number + 1),
                self.blocks_to_reindex_again,
            )
            from_block_number = max(from_block_number - blocks_to_reindex, 0)

        return from_block_number, to_block_number

    def get_to_block_number(
        self, from_block_number: int, current_block_number: int
    ) -> int:
        """
        :param from_block_number:
        :param current_block_number:

        :return: Top block number to process
        """
        return min(
            from_block_number + self.block_process_limit - 1,
            current_block_number - self.confirmations,
        )

    def get_from_block_number(
        self, addresses: Optional[set[ChecksumAddress]] = None
    ) -> Optional[int]:
        """
        :param addresses:
        :return: Minimum block number for all the `addresses` provided. If not provided, return
            minimum block number for every `address` on the table.
        """
        logger.debug(
            "%s: Getting minimum-block-number for %s addresses",
            self.__class__.__name__,
            len(addresses) if addresses else "all the",
        )
        queryset = (
            self.database_queryset.filter(address__in=addresses)
            if addresses
            else self.database_queryset
        )
        minimum_block_number = queryset.aggregate(
            **{self.database_field: Min(self.database_field)}
        )[self.database_field]
        logger.debug(
            "%s: Got minimum-block-number=%s",
            self.__class__.__name__,
            minimum_block_number,
        )
        return minimum_block_number

    def get_almost_updated_addresses(
        self, current_block_number: int
    ) -> set[ChecksumAddress]:
        """

        :param current_block_number:
        :return: Addresses almost updated (< `updated_blocks_behind` blocks) to be processed
        """

        logger.debug(
            "%s: Retrieving almost updated monitored addresses", self.__class__.__name__
        )

        from_block_number = max(
            self.get_from_block_number() or 0,
            current_block_number - self.updated_blocks_behind,
        )
        to_block_number = current_block_number - self.confirmations
        almost_updated_addresses = set(
            self.database_queryset.filter(
                **{
                    self.database_field + "__lt": to_block_number,
                    self.database_field + "__gte": from_block_number,
                }
            ).values_list("address", flat=True)
        )

        logger.debug(
            "%s: Retrieved almost updated monitored addresses", self.__class__.__name__
        )
        return almost_updated_addresses

    def get_not_updated_addresses(
        self, current_block_number: int
    ) -> set[ChecksumAddress]:
        """
        :param current_block_number:
        :return: Addresses not updated (> `updated_blocks_behind` blocks) to be processed
        """
        logger.debug(
            "%s: Retrieving not updated monitored addresses",
            self.__class__.__name__,
        )

        not_updated_addresses = set(
            self.database_queryset.filter(
                **{
                    self.database_field
                    + "__lte": current_block_number
                    - self.confirmations
                }
            ).values_list("address", flat=True)
        )

        logger.debug(
            "%s: Retrieved not updated monitored addresses",
            self.__class__.__name__,
        )
        return not_updated_addresses

    def update_monitored_addresses(
        self, addresses: set[str], from_block_number: int, to_block_number: int
    ) -> bool:
        """
        :param addresses: Addresses to have the block number updated
        :param from_block_number: Make sure that no reorg has happened checking that block number was not rollbacked
        :param to_block_number: Block number to be updated
        :return: Number of addresses updated
        """

        logger.debug(
            "%s: Updating monitored addresses",
            self.__class__.__name__,
        )

        # Keep indexing going on the next block
        new_to_block_number = to_block_number + 1

        updated_addresses = self.database_queryset.filter(
            **{
                "address__in": addresses,
                self.database_field
                + "__gte": from_block_number,  # Protect in case of reorg
                self.database_field
                + "__lt": new_to_block_number,  # Don't update to a lower block number
            }
        ).update(**{self.database_field: new_to_block_number})

        all_updated = updated_addresses == len(addresses)
        if not all_updated:
            logger.warning(
                "%s: Possible reorg - Cannot update all indexed addresses... Updated %d/%d addresses "
                "from-block-number=%d to-block-number=%d",
                self.__class__.__name__,
                updated_addresses,
                len(addresses),
                from_block_number,
                new_to_block_number,
            )

        logger.debug(
            "%s: Updated monitored addresses",
            self.__class__.__name__,
        )

        return updated_addresses

    @contextmanager
    def auto_adjust_block_limit(self, from_block_number: int, to_block_number: int):
        """
        Optimize number of elements processed every time (block process limit)
        based on how fast the block interval is retrieved
        """

        # Check that we are processing the `block_process_limit`, if not, measures are not valid
        if not (
            self.block_auto_process_limit
            and (1 + to_block_number - from_block_number) == self.block_process_limit
        ):
            # Auto adjustment disabled
            yield
        else:
            start = int(time.time())
            yield
            delta = int(time.time()) - start
            if delta > 30:
                self.block_process_limit = max(self.block_process_limit // 2, 1)
                logger.info(
                    "%s: block_process_limit halved to %d",
                    self.__class__.__name__,
                    self.block_process_limit,
                )
            elif delta > 10:
                new_block_process_limit = max(self.block_process_limit - 20, 1)
                self.block_process_limit = new_block_process_limit
                logger.info(
                    "%s: block_process_limit decreased to %d",
                    self.__class__.__name__,
                    self.block_process_limit,
                )
            elif delta < 2:
                self.block_process_limit *= 2
                logger.info(
                    "%s: block_process_limit duplicated to %d",
                    self.__class__.__name__,
                    self.block_process_limit,
                )
            elif delta < 5:
                self.block_process_limit += 20
                logger.info(
                    "%s: block_process_limit increased to %d",
                    self.__class__.__name__,
                    self.block_process_limit,
                )

            if (
                self.block_process_limit_max
                and self.block_process_limit > self.block_process_limit_max
            ):
                logger.info(
                    "%s: block_process_limit %d is bigger than block_process_limit_max %d, reducing",
                    self.__class__.__name__,
                    self.block_process_limit,
                    self.block_process_limit_max,
                )
                self.block_process_limit = self.block_process_limit_max

    def process_addresses(
        self,
        addresses: set[ChecksumAddress],
        current_block_number: Optional[int] = None,
    ) -> tuple[Sequence[Any], Optional[int], int, bool]:
        """
        Find and process relevant data for `addresses`, then store and return it

        :param addresses: Addresses to process
        :param current_block_number: To prevent fetching it again
        :return: Tuple with a sequence of `processed data`, `first_block_number` processed,`last_block_number` processed
            and `True` if no more blocks to scan, `False` otherwise
        """
        assert addresses, "Addresses cannot be empty!"

        current_block_number = (
            current_block_number or self.ethereum_client.current_block_number
        )
        parameters = self.get_block_numbers_for_search(addresses, current_block_number)
        if parameters is None:
            return [], None, current_block_number, True
        from_block_number, to_block_number = parameters

        updated = to_block_number == (current_block_number - self.confirmations)

        try:
            elements = self.find_relevant_elements(
                addresses,
                from_block_number,
                to_block_number,
                current_block_number=current_block_number,
            )
            processed_elements = self.process_elements(elements)
        except (
            FindRelevantElementsException,
            SoftTimeLimitExceeded,
            Timeout,
            ValueError,
            Web3RPCError,
        ) as e:
            self.block_process_limit = 1  # Set back to the very minimum
            logger.info(
                "%s: block_process_limit set back to %d",
                self.__class__.__name__,
                self.block_process_limit,
            )
            raise e

        if not self.update_monitored_addresses(
            addresses, from_block_number, to_block_number
        ):
            raise ValueError(
                "Possible reorg, indexed addresses were updated while indexer was running"
            )

        return processed_elements, from_block_number, to_block_number, updated

    def start(self) -> tuple[int, int]:
        """
        Find and process relevant data for existing database addresses

        :return: (number of elements processed, number of blocks processed)
        """
        current_block_number = self.ethereum_client.current_block_number
        logger.debug(
            "%s: Current RPC block number=%d",
            self.__class__.__name__,
            current_block_number,
        )
        total_number_processed_elements = 0
        start_block: Optional[int] = None
        last_block: Optional[int] = None
        to_block_number: Optional[int] = None

        # First process addresses that are almost updated (usually close to the `current_block_number`)
        if almost_updated_addresses := self.get_almost_updated_addresses(
            current_block_number
        ):
            logger.info(
                "%s: Processing almost updated addresses",
                self.__class__.__name__,
            )
            updated = False
            while not updated:
                (
                    processed_elements,
                    from_block_number,
                    to_block_number,
                    updated,
                ) = self.process_addresses(
                    almost_updated_addresses,
                    current_block_number=current_block_number,
                )
                number_processed_elements = len(processed_elements)
                logger.debug(
                    "%s: Processed %d elements for almost updated addresses. From-block-number=%s to-block-number=%d",
                    self.__class__.__name__,
                    number_processed_elements,
                    from_block_number,  # Can be None
                    to_block_number,
                )
                total_number_processed_elements += number_processed_elements
                if start_block is None:
                    start_block = from_block_number
            last_block = to_block_number
        else:
            logger.debug(
                "%s: No almost updated addresses to process", self.__class__.__name__
            )

        # Then process addresses that are not updated (usually far from the `current_block_number`)
        if not_updated_addresses := self.get_not_updated_addresses(
            current_block_number
        ):
            logger.info(
                "%s: Processing not updated addresses",
                self.__class__.__name__,
            )

            updated = False
            while not updated:
                (
                    processed_elements,
                    from_block_number,
                    to_block_number,
                    updated,
                ) = self.process_addresses(
                    not_updated_addresses,
                    current_block_number=current_block_number,
                )
                if start_block is None or from_block_number < start_block:
                    start_block = from_block_number

                number_processed_elements = len(processed_elements)
                logger.debug(
                    "%s: Processed %d elements for not updated addresses. From-block-number=%s to-block-number=%d",
                    self.__class__.__name__,
                    number_processed_elements,
                    from_block_number,  # Can be None
                    to_block_number,
                )
                total_number_processed_elements += number_processed_elements
                from_block_number += 1
            if last_block is None or to_block_number > last_block:
                last_block = to_block_number
        else:
            logger.debug(
                "%s: No not updated addresses to process", self.__class__.__name__
            )
        if start_block is not None and last_block is not None:
            number_of_blocks_processed = last_block - start_block + 1
        else:
            number_of_blocks_processed = 0

        return total_number_processed_elements, number_of_blocks_processed
