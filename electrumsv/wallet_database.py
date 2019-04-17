"""

# Why DateCreated, DateUpdated and DateDeleted?

This was added with the intent that it can be used to serve as a watermark. As most, if not all of
this data is stored in encrypted lumps, we will need to index it and provide cached overviews of it
that can be quickly loaded to provide a responsive user-interface (presumably).

It should be possible using these dates to quickly ascertain whether the indexed/cached preview
is out of date.

"""

from abc import ABC, abstractmethod
from collections import namedtuple
import enum
from io import BytesIO
import random
import sqlite3
import threading
import time
from typing import Optional, Dict, Set, Iterable, List, Tuple

import bitcoinx

from .logs import logs
from .transaction import Transaction


__all__ = [
    "MissingRowError", "DataPackingError", "TransactionStore", "TransactionInputStore",
    "TransactionOutputStore",
]


class DataPackingError(Exception):
    pass


class MissingRowError(Exception):
    pass


class InvalidDataError(Exception):
    pass


TXDATA_VERSION = 1
TXPROOF_VERSION = 1


total_time = 0.0

def tprofiler(func):
    def do_profile(func, args, kw_args):
        global total_time
        n = func.__name__
        logger = logs.get_logger("profiler")
        t0 = time.time()
        o = func(*args, **kw_args)
        t = time.time() - t0
        total_time += t
        logger.debug("%s call=%.4f total=%0.4f", n, t, total_time)
        return o
    return lambda *args, **kw_args: do_profile(func, args, kw_args)

def byte_repr(value):
    if value is None:
        return str(value)
    return f"ByteData({len(value)})"


# TODO: Deletion should be via a flag. Occasional purges might do row deletion of flagged rows.
# NOTE: We could hash the db and store the hash in the wallet storage to detect changes.

class BaseWalletStore:
    _table_name = None

    def __init__(self, table_name: str, wallet_path: str, aeskey: bytes) -> None:
        self._state = threading.local()
        self._aes_key = aeskey[:16]
        self._aes_iv = aeskey[16:]

        self._db_path = wallet_path +".sqlite"

        self._set_table_name(table_name)

        db = self._get_db()
        self._db_create(db)
        self._db_migrate(db)
        db.commit()

        self._fetch_write_timestamp()

    def _set_table_name(self, table_name: str) -> None:
        self._table_name = table_name

    def _db_create(self, db: sqlite3.Connection) -> None:
        pass

    def _db_migrate(self, db: sqlite3.Connection) -> None:
        pass

    def _get_db(self):
        if not hasattr(self._state, "db"):
            self._state.db = sqlite3.connect(self._db_path)
        return self._state.db

    def _get_column_types(self, db, table_name):
        column_types = {}
        for row in db.execute(f"PRAGMA table_info({table_name});"):
            _discard, column_name, column_type, _discard, _discard, _discard = row
            column_types[column_name] = column_type
        return column_types

    def close(self):
        # TODO: This only closes the database instance held on the current thread. In theory
        # only the async code behind the daemon should be touching this, not the GUI thread
        # via the wallet.
        self._state.db.close()
        self._state = None

    def get_write_timestamp(self):
        "Get the cached write timestamp (when anything was last updated or deleted)."
        return self._write_timestamp

    def _get_current_timestamp(self):
        "Get the current timestamp in a form suitable for database column storage."
        return int(time.time())

    def _fetch_write_timestamp(self):
        "Calculate the timestamp of the last write to this table, based on database metadata."
        self._write_timestamp = 0

        if self._table_name is None:
            return

        db = self._get_db()
        cursor = db.execute(f"SELECT DateUpdated FROM {self._table_name} "+
            "ORDER BY DateUpdated DESC LIMIT 1")
        row = cursor.fetchone()
        if row is not None:
            self._write_timestamp = max(row[0], self._write_timestamp)

        cursor = db.execute(f"SELECT DateDeleted FROM {self._table_name} "+
            "ORDER BY DateDeleted DESC LIMIT 1")
        row = cursor.fetchone()
        if row is not None and row[0] is not None:
            self._write_timestamp = max(row[0], self._write_timestamp)

    def _encrypt(self, value: bytes) -> bytes:
        return bitcoinx.aes.aes_encrypt_with_iv(self._aes_key, self._aes_iv, value)

    def _decrypt(self, value: bytes) -> bytes:
        return bitcoinx.aes.aes_decrypt_with_iv(self._aes_key, self._aes_iv, value)

    def _encrypt_hex(self, value: str) -> bytes:
        return self._encrypt(bytes.fromhex(value))

    def _decrypt_hex(self, value: bytes) -> str:
        return self._decrypt(value).hex()


class GenericKeyValueStore(BaseWalletStore):
    def __init__(self, table_name: str, wallet_path: str, aeskey: bytes) -> None:
        self._logger = logs.get_logger(f"{table_name}-store")

        super().__init__(table_name, wallet_path, aeskey)

    def _set_table_name(self, table_name: str) -> None:
        super()._set_table_name(table_name)

        self._CREATE_TABLE_SQL = ("CREATE TABLE IF NOT EXISTS "+ table_name +" ("+
                "Key BLOB,"+
                "ByteData BLOB,"+
                "DateCreated INTEGER,"+
                "DateUpdated INTEGER,"+
                "DateDeleted INTEGER DEFAULT NULL"+
            ")")
        self._CREATE_SQL = ("INSERT INTO "+ table_name +" "+
            "(Key, ByteData, DateCreated, DateUpdated) VALUES (?, ?, ?, ?)")
        self._READ_SQL = ("SELECT ByteData FROM "+ table_name +" "+
            "WHERE DateDeleted IS NULL AND Key=?")
        self._READ_ALL_SQL = ("SELECT Key, ByteData FROM "+ table_name +" "+
            "WHERE DateDeleted IS NULL")
        self._READ_ROW_SQL = ("SELECT ByteData, DateCreated, DateUpdated, DateDeleted "+
            "FROM "+ table_name +" "+
            "WHERE Key=?")
        self._UPDATE_SQL = ("UPDATE "+ table_name +" SET ByteData=?, DateUpdated=? "+
            "WHERE DateDeleted IS NULL AND Key=?")
        self._DELETE_SQL = ("UPDATE "+ table_name +" SET DateDeleted=? "+
            "WHERE DateDeleted IS NULL AND Key=?")
        self._DELETE_VALUE_SQL = ("UPDATE "+ table_name +" SET DateDeleted=? "+
            "WHERE DateDeleted IS NULL AND Key=? AND ByteData=?")

    def _db_create(self, db: sqlite3.Connection) -> None:
        db.execute(self._CREATE_TABLE_SQL)

    def _fetch_write_timestamp(self):
        "Calculate the timestamp of the last write to this table, based on database metadata."
        self._write_timestamp = 0

        db = self._get_db()
        cursor = db.execute(f"SELECT DateUpdated FROM {self._table_name} "+
            "ORDER BY DateUpdated DESC LIMIT 1")
        row = cursor.fetchone()
        if row is not None:
            self._write_timestamp = max(row[0], self._write_timestamp)

        cursor = db.execute(f"SELECT DateDeleted FROM {self._table_name} "+
            "ORDER BY DateDeleted DESC LIMIT 1")
        row = cursor.fetchone()
        if row is not None and row[0] is not None:
            self._write_timestamp = max(row[0], self._write_timestamp)

    @tprofiler
    def add(self, key: str, value: bytes) -> None:
        assert type(value) is bytes
        ekey = self._encrypt_hex(key)
        evalue = self._encrypt(value)
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp
        db = self._get_db()
        db.execute(self._CREATE_SQL, [ekey, evalue, timestamp, timestamp])
        db.commit()
        self._logger.debug("added '%s'", key)

    @tprofiler
    def get_value(self, key: str) -> Optional[bytes]:
        ekey = self._encrypt_hex(key)
        db = self._get_db()
        cursor = db.execute(self._READ_SQL, [ekey])
        row = cursor.fetchone()
        if row is not None:
            return self._decrypt(row[0])
        return None

    @tprofiler
    def get_all(self) -> Optional[bytes]:
        db = self._get_db()
        cursor = db.execute(self._READ_ALL_SQL)
        return [ (self._decrypt_hex(row[0]), self._decrypt(row[1])) for row in cursor.fetchall() ]

    @tprofiler
    def get_values(self, key: str) -> List[bytes]:
        ekey = self._encrypt_hex(key)
        db = self._get_db()
        cursor = db.execute(self._READ_SQL, [ekey])
        return [ self._decrypt(row[0]) for row in cursor.fetchall() ]

    @tprofiler
    def get_row(self, key: str) -> Optional[bytes]:
        ekey = self._encrypt_hex(key)
        db = self._get_db()
        cursor = db.execute(self._READ_ROW_SQL, [ekey])
        row = cursor.fetchone()
        if row is not None:
            return (self._decrypt(row[0]), row[1], row[2], row[3])
        return None

    @tprofiler
    def update(self, key: str, value: bytes) -> None:
        assert type(value) is bytes
        ekey = self._encrypt_hex(key)
        evalue = self._encrypt(value)
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp
        db = self._get_db()
        db.execute(self._UPDATE_SQL, [evalue, timestamp, ekey])
        db.commit()
        self._logger.debug("updated '%s'", key)

    @tprofiler
    def delete(self, key: str) -> None:
        ekey = self._encrypt_hex(key)
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp
        db = self._get_db()
        db.execute(self._DELETE_SQL, [timestamp, ekey])
        db.commit()
        self._logger.debug("deleted '%s'", key)

    @tprofiler
    def delete_value(self, key: str, value: bytes) -> None:
        ekey = self._encrypt_hex(key)
        evalue = self._encrypt(value)
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp
        db = self._get_db()
        db.execute(self._DELETE_VALUE_SQL, [timestamp, ekey, evalue])
        db.commit()
        self._logger.debug("deleted value for '%s'", key)


class AbstractTransactionXput(ABC):
    @abstractmethod
    def add_entry(self, tx_id: str, txinout: Tuple) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_entries(self, tx_id: str) -> List[Tuple]:
        raise NotImplementedError

    @abstractmethod
    def delete_entry(self, tx_id: str, txinout: Tuple) -> None:
        raise NotImplementedError


class TxInput(namedtuple("TxInputTuple", "address_string prevout_tx_hash prevout_n amount")):
    pass


class TransactionInputStore(GenericKeyValueStore, AbstractTransactionXput):
    def __init__(self, wallet_path: str, aeskey: bytes) -> None:
        super().__init__("TransactionInputs", wallet_path, aeskey)

    @staticmethod
    def _pack_value(txin: TxInput) -> bytes:
        raw = bitcoinx.pack_varint(1)
        raw += bitcoinx.pack_varbytes(txin.address_string.encode())
        raw += bitcoinx.pack_varbytes(txin.prevout_tx_hash.encode())
        raw += bitcoinx.pack_varint(txin.prevout_n)
        raw += bitcoinx.pack_varint(txin.amount)
        return raw

    @staticmethod
    def _unpack_value(raw: bytes) -> TxInput:
        io = BytesIO(raw)
        pack_version = bitcoinx.read_varint(io.read)
        if pack_version == 1:
            address_string = bitcoinx.read_varbytes(io.read).decode()
            prevout_tx_hash = bitcoinx.read_varbytes(io.read).decode()
            prevout_n = bitcoinx.read_varint(io.read)
            amount = bitcoinx.read_varint(io.read)
            return TxInput(address_string, prevout_tx_hash, prevout_n, amount)
        raise DataPackingError(f"Unhandled packing format {pack_version}")

    def add_entry(self, tx_id: str, txin: TxInput) -> None:
        value = self._pack_value(txin)
        super().add(tx_id, value)

    def get_entries(self, tx_id: str) -> List[TxInput]:
        values = super().get_values(tx_id)
        for i, value in enumerate(values):
            values[i] = self._unpack_value(value)
        return values

    def get_all_entries(self) -> Dict[str, List[TxInput]]:
        d = {}
        for key, value in super().get_all():
            l = d.setdefault(key, [])
            l.append(self._unpack_value(value))
        return d

    def delete_entry(self, tx_id: str, txin: TxInput) -> None:
        value = self._pack_value(txin)
        super().delete_value(tx_id, value)


class TxOutput(namedtuple("TxOutputTuple", "address_string out_tx_n amount is_coinbase")):
    pass


class TransactionOutputStore(GenericKeyValueStore):
    def __init__(self, wallet_path: str, aeskey: bytes) -> None:
        super().__init__("TransactionOutputs", wallet_path, aeskey)

    @staticmethod
    def _pack_value(txout: TxOutput) -> bytes:
        raw = bitcoinx.pack_varint(1)
        raw += bitcoinx.pack_varbytes(txout.address_string.encode())
        raw += bitcoinx.pack_varint(txout.out_tx_n)
        raw += bitcoinx.pack_varint(txout.amount)
        raw += bitcoinx.pack_varint(int(txout.is_coinbase))
        return raw

    @staticmethod
    def _unpack_value(raw: bytes) -> TxOutput:
        io = BytesIO(raw)
        pack_version = bitcoinx.read_varint(io.read)
        if pack_version == 1:
            address_string = bitcoinx.read_varbytes(io.read).decode()
            out_tx_n = bitcoinx.read_varint(io.read)
            amount = bitcoinx.read_varint(io.read)
            is_coinbase = bool(bitcoinx.read_varint(io.read))
            return TxOutput(address_string, out_tx_n, amount, is_coinbase)
        raise DataPackingError(f"Unhandled packing format {pack_version}")

    def add_entry(self, tx_id: str, txout: TxOutput) -> None:
        value = self._pack_value(txout)
        super().add(tx_id, value)

    def get_entries(self, tx_hash: str) -> List[TxOutput]:
        values = super().get_values(tx_hash)
        for i, value in enumerate(values):
            values[i] = self._unpack_value(value)
        return values

    def get_all_entries(self) -> Dict[str, List[TxOutput]]:
        d = {}
        for key, value in super().get_all():
            l = d.setdefault(key, [])
            l.append(self._unpack_value(value))
        return d

    def delete_entry(self, tx_id: str, txout: TxOutput) -> None:
        value = self._pack_value(txout)
        super().delete_value(tx_id, value)


class TxData(namedtuple("TxDataTuple", "height timestamp position fee",
        defaults=(None, None, None, None))):
    def __repr__(self):
        return (f"TxData(height={self.height},timestamp={self.timestamp},"+
            f"position={self.position},fee={self.fee}")


class TxProof(namedtuple("TxProofTuple", "position branch")):
    pass


class TxFlags(enum.IntEnum):
    Unset = 0

    # TxData() packed into Transactions.MetaData:
    HasFee = 1 << 4
    HasHeight = 1 << 5
    HasPosition = 1 << 6
    HasTimestamp = 1 << 7

    # TODO: Evaluate whether maintaining these is more effort than it's worth.
    # Reflects Transactions.ByteData contains a value:
    HasByteData = 1 << 12
    # Reflects Transactions.ProofData contains a value:
    HasProofData = 1 << 13

    # A transaction received over the p2p network which is unconfirmed and in the mempool.
    StateSettled = 1 << 20
    # A transaction received over the p2p network which is confirmed and known to be in a block.
    StateCleared = 1 << 21
    # A transaction received from another party which is unknown to the p2p network.
    StateReceived = 1 << 22
    # A transaction you have not sent or given to anyone else, but are with-holding and are
    # considering the inputs it uses frozen. """
    StateSigned = 1 << 23
    # A transaction you have given to someone else, and are considering the inputs it uses frozen.
    StateDispatched = 1 << 24

    METADATA_FIELD_MASK = (HasFee | HasHeight | HasPosition | HasTimestamp)
    STATE_MASK = (StateCleared | StateDispatched | StateReceived | StateSettled | StateSigned)
    MASK = 0xFFFFFFFF

    def __repr__(self):
        return f"TxFlags({self.name})"

    @staticmethod
    def to_repr(bitmask: int):
        if bitmask is None:
            return repr(bitmask)

        # Handle existing values.
        try:
            return f"TxFlags({TxFlags(bitmask).name})"
        except ValueError:
            pass

        # Handle bit flags.
        mask = TxFlags.StateDispatched
        names = []
        while mask > 0:
            value = bitmask & mask
            if value == mask:
                try:
                    names.append(TxFlags(value).name)
                except ValueError:
                    pass
            mask >>= 1

        return f"TxFlags({'|'.join(names)})"


class TransactionStore(BaseWalletStore):
    """
    We store transactions for two cases currently:
    - Received transactions (IsPending=0) which have come in over the P2P network. These are
      solely those related to the user's inputs, outputs and pruned transactions.
    - Pending transactions (IsPending=1), which have been constructed and designated in play,
      but not broadcast to the P2P network by the user, nor broadcast to the P2P network by anyone
      they might have been given to. The persisted existence of these are considered to freeze the
      coins they have in their inputs.

    These transactions must be the user's own transactions relating to their own inputs and
    outputs.
    """

    def __init__(self, wallet_path: str, aeskey: bytes) -> None:
        self._logger = logs.get_logger("tx-store")

        super().__init__("Transactions", wallet_path, aeskey)

    def _db_create(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS Transactions ("+
                "Key BLOB, "+
                "Flags INTEGER,"
                "MetaData BLOB,"+
                "ByteData BLOB,"+
                "ProofData BLOB,"+
                "DateCreated INTEGER,"+
                "DateUpdated INTEGER,"+
                "DateDeleted INTEGER DEFAULT NULL,"+
                "UNIQUE(Key,DateDeleted))")

    def _db_migrate(self, db):
        pass

    @staticmethod
    def _pack_data(data: TxData, flags: int) -> bytes:
        flags &= ~TxFlags.METADATA_FIELD_MASK
        if data.height is not None:
            flags |= TxFlags.HasHeight
        if data.fee is not None:
            flags |= TxFlags.HasFee
        if data.position is not None:
            flags |= TxFlags.HasPosition
        if data.timestamp is not None:
            flags |= TxFlags.HasTimestamp

        raw = bitcoinx.pack_varint(1)
        # Why put random dummy values in? Why not?
        raw += bitcoinx.pack_varint(data.height if flags & TxFlags.HasHeight
                                    else random.randint(1000, 100000))
        raw += bitcoinx.pack_varint(data.fee if flags & TxFlags.HasFee
                                    else random.randint(100, 2000))
        raw += bitcoinx.pack_varint(data.position if flags & TxFlags.HasPosition
                                    else random.randint(2, 2000))
        raw += bitcoinx.pack_varint(data.timestamp if flags & TxFlags.HasTimestamp
                                    else random.randint(1554000000, 1556000000))
        return raw, flags

    @staticmethod
    def _unpack_data(raw: bytes, flags: int) -> TxData:
        io = BytesIO(raw)
        pack_version = bitcoinx.read_varint(io.read)
        if pack_version == 1:
            kwargs = {}
            for kw, mask in (
                    ('height', TxFlags.HasHeight),
                    ('fee', TxFlags.HasFee),
                    ('position', TxFlags.HasPosition),
                    ('timestamp', TxFlags.HasTimestamp)):
                value = bitcoinx.read_varint(io.read)
                kwargs[kw] = value if (flags & mask) == mask else None
            return TxData(**kwargs)
        raise DataPackingError(f"Unhandled packing format {pack_version}")

    @staticmethod
    def _pack_proof(proof: TxProof) -> bytes:
        raw = bitcoinx.pack_varint(1)
        raw += bitcoinx.pack_varint(proof.position)
        raw += bitcoinx.pack_varint(len(proof.branch))
        for hash in proof.branch:
            raw += bitcoinx.pack_varbytes(hash)
        return raw

    @staticmethod
    def _unpack_proof(raw: bytes) -> TxProof:
        io = BytesIO(raw)
        pack_version = bitcoinx.read_varint(io.read)
        if pack_version == 1:
            position = bitcoinx.read_varint(io.read)
            branch_count = bitcoinx.read_varint(io.read)
            merkle_branch = [ bitcoinx.read_varbytes(io.read) for i in range(branch_count) ]
            return TxProof(position, merkle_branch)
        raise DataPackingError(f"Unhandled packing format {pack_version}")

    @staticmethod
    def _flag_clause(flags: Optional[int], mask: Optional[int]) -> Tuple[str, Tuple]:
        if flags is None:
            if mask is None:
                return "", []
            return "(flags & ?) != 0", [mask]

        if mask is None:
            return "(flags & ?) != 0", [flags]

        return "(flags & ?) == ?", [mask, flags]

    @tprofiler
    def has(self, tx_id: str) -> bool:
        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        cursor = db.execute("SELECT EXISTS(SELECT 1 FROM Transactions "+
            "WHERE Key=? AND DateDeleted IS NULL)", [etx_id])
        row = cursor.fetchone()
        return row[0] == 1

    @tprofiler
    def get_flags(self, tx_id: str) -> Optional[int]:
        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        cursor = db.execute(
            "SELECT Flags FROM Transactions "+
            "WHERE Key=? AND DateDeleted IS NULL", [etx_id])
        row = cursor.fetchone()
        return row[0] if row is not None else None

    @tprofiler
    def get(self, tx_id: str, flags: Optional[int]=None,
            mask: Optional[int]=None) -> Optional[Tuple[TxData, Optional[bytes], int]]:
        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        clause, params = self._flag_clause(flags, mask)
        query = "SELECT MetaData, ByteData, Flags FROM Transactions WHERE Key=?"
        if clause:
            query += " AND "+ clause
        cursor = db.execute(query, [etx_id] + params)
        row = cursor.fetchone()
        if row is not None:
            bytedata = self._decrypt(row[1]) if row[1] is not None else None
            return self._unpack_data(self._decrypt(row[0]), row[2]), bytedata, row[2]
        return None

    @tprofiler
    def get_many(self, flags: Optional[int]=None, mask: Optional[int]=None,
            tx_ids: Optional[Iterable[str]]=None) -> List[Tuple[str, TxData, Optional[bytes], int]]:
        db = self._get_db()
        query = "SELECT Key, MetaData, ByteData, Flags FROM Transactions"
        clause, params = self._flag_clause(flags, mask)
        if clause:
            query += " WHERE "+ clause
        if tx_ids is not None and len(tx_ids):
            etx_ids = [ self._encrypt_hex(tx_id) for tx_id in tx_ids ]
            if clause:
                query += " AND "
            else:
                query += " WHERE "
            query += "Key IN ({0})".format(",".join("?" for k in tx_ids))
            params += tx_ids
        cursor = db.execute(query, params)
        results = []
        for row in cursor.fetchall():
            tx_id = self._decrypt_hex(row[0])
            bytedata = self._decrypt(row[2]) if row[2] is not None else None
            data = self._unpack_data(self._decrypt(row[1]), row[3])
            results.append((tx_id, data, bytedata, row[3]))
        return results

    @tprofiler
    def get_metadata(self, tx_id: str, flags: Optional[int]=None,
            mask: Optional[int]=None) -> Optional[TxData]:
        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        clause, params = self._flag_clause(flags, mask)
        query = "SELECT MetaData, Flags FROM Transactions WHERE Key=?"
        if clause:
            query += " AND "+ clause
        cursor = db.execute(query, [etx_id] + params)
        row = cursor.fetchone()
        if row is not None:
            return self._unpack_data(self._decrypt(row[0]), row[1]), row[1]
        return None, None

    @tprofiler
    def get_metadata_many(self, flags: Optional[int]=None, mask: Optional[int]=None,
            tx_ids: Optional[Iterable[str]]=None) -> List[Tuple[str, TxData]]:
        db = self._get_db()
        query = "SELECT Key, MetaData, Flags FROM Transactions"
        clause, params = self._flag_clause(flags, mask)
        if clause:
            query += " WHERE "+ clause
        if tx_ids is not None and len(tx_ids):
            etx_ids = [ self._encrypt_hex(tx_id) for tx_id in tx_ids ]
            if clause:
                query += " AND "
            else:
                query += " WHERE "
            query += "Key IN ({0})".format(",".join("?" for k in tx_ids))
            params += tx_ids
        cursor = db.execute(query, params)
        results = []
        for row in cursor.fetchall():
            tx_id = self._decrypt_hex(row[0])
            data = self._unpack_data(self._decrypt(row[1]), row[2])
            results.append((tx_id, data, row[2]))
        return results

    @tprofiler
    def get_proof(self, tx_id: str) -> Optional[TxProof]:
        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        cursor = db.execute(
            "SELECT ProofData FROM Transactions "+
            "WHERE DateDeleted is NULL AND Key=?", [etx_id])
        row = cursor.fetchone()
        if row is None:
            raise MissingRowError(tx_id)
        if row[0] is None:
            return None
        raw = self._decrypt(row[0])
        return self._unpack_proof(raw)

    @tprofiler
    def get_ids(self, flags: Optional[int]=None, mask: Optional[int]=None) -> Set[str]:
        db = self._get_db()
        query = "SELECT Key FROM Transactions WHERE DateDeleted IS NULL"
        clause, params = self._flag_clause(flags, mask)
        if clause:
            query += " AND "+ clause
        results = []
        for t in db.execute(query, params):
            results.append(self._decrypt_hex(t[0]))
        return set(results)

    def add(self, tx_id: str, metadata: TxData, bytedata: Optional[bytes]=None,
            flags: Optional[int]=TxFlags.Unset) -> None:
        self.add_many([ (tx_id, metadata, bytedata, flags) ])

    @tprofiler
    def add_many(self, entries: List[Tuple[str, TxData, Optional[bytes], int]]) -> None:
        db = self._get_db()
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp
        for tx_id, metadata, bytedata, flags in entries:
            etx_id = self._encrypt_hex(tx_id)
            metadata_bytes, flags = self._pack_data(metadata, flags)
            emetadata = self._encrypt(metadata_bytes)
            flags &= ~TxFlags.HasByteData
            if bytedata is not None:
                flags |= TxFlags.HasByteData
            ebytedata = None if bytedata is None else self._encrypt(bytedata)
            db.execute("INSERT INTO Transactions "+
                "(Key, MetaData, ByteData, Flags, DateCreated, DateUpdated) "+
                "VALUES (?, ?, ?, ?, ?, ?)",
                [etx_id, emetadata, ebytedata, flags, timestamp, timestamp])
        db.commit()
        self._logger.debug("add %d transactions: %s", len(entries),
            [ (a, b, byte_repr(c), d) for (a, b, c, d) in entries ])

    def update(self, tx_id: str, metadata: TxData, bytedata: Optional[bytes],
            flags: Optional[int]=TxFlags.Unset) -> None:
        self.update_many([ (tx_id, metadata, bytedata, flags) ])

    @tprofiler
    def update_many(self, entries: List[Tuple[str, TxData, bytes, int]]) -> None:
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp

        datas = []
        for tx_id, metadata, bytedata, flags in entries:
            assert bytedata is not None
            etx_id = self._encrypt_hex(tx_id)
            metadata_bytes, flags = self._pack_data(metadata, flags)
            emetadata = self._encrypt(metadata_bytes)
            flags |= TxFlags.HasByteData
            ebytedata = self._encrypt(bytedata)
            datas.append((emetadata, ebytedata, flags, timestamp, etx_id))

        db = self._get_db()
        db.executemany(
            "UPDATE Transactions SET MetaData=?,ByteData=?,Flags=?,DateUpdated=? "+
            "WHERE Key=? AND DateDeleted IS NULL",
            datas)
        db.commit()
        self._logger.debug("update %d transactions: %s", len(entries),
            [ (a, b, byte_repr(c), d) for (a, b, c, d) in entries ])

    def update_metadata(self, tx_id: str, data: TxData, flags: Optional[int]=TxFlags.Unset) -> None:
        # NOTE: This should only be used if it knows the existing flags column value, it should
        # preserve the state, bytedata and proofdata flags if it does not intend to clear them.
        self.update_metadata_many([ (tx_id, data, flags) ])

    @tprofiler
    def update_metadata_many(self, entries: List[Tuple[str, TxData, int]]) -> None:
        # NOTE: This should only be used if it knows the existing flags column value, it should
        # preserve the state, bytedata and proofdata flags if it does not intend to clear them.
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp

        datas = []
        for tx_id, data, flags in entries:
            etx_id = self._encrypt_hex(tx_id)
            metadata_bytes, flags = self._pack_data(data, flags)
            emetadata = self._encrypt(metadata_bytes)
            datas.append((emetadata, flags, timestamp, etx_id))
        db = self._get_db()
        db.executemany(
            "UPDATE Transactions SET MetaData=?,Flags=?,DateUpdated=? "+
            "WHERE Key=? AND DateDeleted IS NULL",
            datas)
        db.commit()
        self._logger.debug("update %d transactions: %s", len(entries),
            [ (a, b, byte_repr(c), d) for (a, b, c, d) in entries ])

    @tprofiler
    def update_flags(self, tx_id: str, flags: int, mask: Optional[int]=TxFlags.Unset) -> None:
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp

        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        db.execute("UPDATE Transactions SET Flags=((Flags&?)|?), DateUpdated=? "+
            "WHERE Key=? AND DateDeleted IS NULL",
            [mask, flags, timestamp, etx_id])
        db.commit()
        self._logger.debug("update_flags '%s'", tx_id)

    @tprofiler
    def update_proof(self, tx_id: str, proof: TxProof) -> None:
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp

        etx_id = self._encrypt_hex(tx_id)
        raw = self._pack_proof(proof)
        eraw = self._encrypt(raw)
        db = self._get_db()
        db.execute(
            "UPDATE Transactions SET ProofData=?, DateUpdated=?, Flags=(Flags|?) "+
            "WHERE Key=? AND DateDeleted IS NULL",
            [eraw, timestamp, TxFlags.HasProofData, etx_id])
        db.commit()
        self._logger.debug("updated %d transaction proof '%s'", 1, tx_id)

    @tprofiler
    def delete(self, tx_id: str) -> None:
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp

        etx_id = self._encrypt_hex(tx_id)
        db = self._get_db()
        db.execute("UPDATE Transactions SET DateDeleted=? WHERE Key=? AND DateDeleted IS NULL",
            [timestamp, etx_id])
        db.commit()
        self._logger.debug("deleted %d transaction '%s'", 1, tx_id)

    @tprofiler
    def delete_many(self, tx_ids: Iterable[str]) -> None:
        # TODO: Integrate this with delete and look at using executemany.
        timestamp = self._get_current_timestamp()
        self._write_timestamp = timestamp

        db = self._get_db()
        for tx_id in tx_ids:
            etx_id = self._encrypt_hex(tx_id)
            db.execute("UPDATE Transactions SET DateDeleted=? WHERE Key=? AND DateDeleted IS NULL",
                [timestamp, etx_id])
        db.commit()
        self._logger.debug("deleted %d transactions", len(tx_ids))


class TxXputCache(AbstractTransactionXput):
    def __init__(self, store, preload=False):
        self._store = store
        self._preload = preload

        if self._preload:
            self._cache = store.get_all_entries()
        else:
            self._cache = {}

    def add_entry(self, tx_id: str, tx_xput: tuple) -> None:
        cached_entries = self._cache.setdefault(tx_id, [])
        cached_entries.append(tx_xput)
        self._store.add_entry(tx_id, tx_xput)

    def get_entries(self, tx_id: str) -> List[tuple]:
        if tx_id not in self._cache:
            if self._preload:
                return []
            self._cache[tx_id] = self._store.get_entries(tx_id)
        return self._cache[tx_id].copy()

    def delete_entry(self, tx_id: str, tx_xput: tuple) -> None:
        cached_entries = self._cache[tx_id]
        cached_entries.remove(tx_xput)
        self._store.delete_entry(tx_id, tx_xput)


class TxCacheEntry:
    def __init__(self, metadata: TxData, flags: int, bytedata: Optional[bytes]=None,
            time_loaded: Optional[float]=None, is_bytedata_cached: bool=True) -> None:
        self._transaction = None
        self.metadata = metadata
        self.bytedata = bytedata
        self._is_bytedata_cached = is_bytedata_cached
        self.flags = flags
        self.time_loaded = time.time() if time_loaded is None else time_loaded

    def is_metadata_cached(self):
        # At this time the metadata blob is always loaded, either by itself, or accompanying
        # the bytedata.
        return self.metadata is not None

    def is_bytedata_cached(self):
        return self._is_bytedata_cached

    @property
    def transaction(self) -> None:
        if self._transaction is None:
            if self.bytedata is None:
                return None
            self._transaction = Transaction(self.bytedata.hex())
        return self._transaction

    def __repr__(self):
        return (f"TxCacheEntry({self.metadata}, {TxFlags.to_repr(self.flags)}, "
            f"{None if self.bytedata is None else f'data({len(self.bytedata)} bytes)'})")


class TxCache:
    def __init__(self, store: TransactionStore) -> None:
        self.logger = logs.get_logger("tx-cache")
        self._cache = {}
        # self._cache_access = {}
        self._store = store

        self.update_proof = self._store.update_proof

    def _validate_transaction_bytes(self, tx_id: str, bytedata: Optional[bytes]) -> bool:
        if bytedata is None:
            return True
        hash_bytes = bitcoinx.double_sha256(bytedata)
        return bitcoinx.hash_to_hex_str(hash_bytes) == tx_id

    def _entry_visible(self, entry_flags: int, flags: Optional[int]=None,
            mask: Optional[int]=None) -> bool:
        """
        Filter an entry based on it's flag bits compared to an optional comparison flag and flag
        mask value.
        - No flag and no mask: keep.
        - No flag and mask: keep if any masked bits are set.
        - Flag and no mask: keep if any masked bits are set.
        - Flag and mask: keep if the masked bits are the flags.
        """
        if flags is None:
            if mask is None:
                return True
            return (entry_flags & mask) != 0
        if mask is None:
            return (entry_flags & flags) != 0
        return (entry_flags & mask) == flags

    @staticmethod
    def _adjust_field_flags(data: TxData, flags: int) -> int:
        flags &= ~TxFlags.METADATA_FIELD_MASK
        flags |= TxFlags.HasFee if data.fee is not None else 0
        flags |= TxFlags.HasHeight if data.height is not None else 0
        flags |= TxFlags.HasPosition if data.position is not None else 0
        flags |= TxFlags.HasTimestamp if data.timestamp is not None else 0
        return flags

    def add_missing_transaction(self, tx_id: str, height: int, fee: Optional[int]=None) -> None:
        # TODO: Consider setting state based on height.
        self.add([ (tx_id, TxData(height=height, fee=fee), None, TxFlags.Unset) ])

    def add_transaction(self, tx: Transaction, flags: Optional[int]=TxFlags.Unset) -> None:
        tx_id = tx.txid()
        tx_hex = str(tx)
        bytedata = bytes.fromhex(tx_hex)
        self.add([ (tx_id, TxData(), bytedata, flags) ])

    def add(self, inserts: List[Tuple[str, TxData, Optional[bytes], int]]) -> None:
        access_time = time.time()
        for tx_id, metadata, bytedata, add_flags in inserts:
            assert tx_id not in self._cache, f"Tx {tx_id} not in cache"
            flags = self._adjust_field_flags(metadata, add_flags)
            if bytedata is not None:
                flags |= TxFlags.HasByteData
            assert ((add_flags & TxFlags.METADATA_FIELD_MASK) == 0 or
                flags == add_flags), f"{TxFlags.to_repr(flags)} != {TxFlags.to_repr(add_flags)}"
            self._cache[tx_id] = TxCacheEntry(metadata, flags, bytedata)
            # self._cache_access[tx_id] = access_time
        self._store.add_many(inserts)

    def update(self, updates: List[Tuple[str, TxData, Optional[bytes], int]]) -> None:
        self._update(updates)

    def _update(self, updates: List[Tuple[str, TxData, Optional[bytes], int]]) -> Iterable[str]:
        # NOTE: This does not set state flags at this time, from update flags.
        # We would need to pass in a per-row mask for that to work, perhaps.

        update_map = { t[0]: t for t in updates }
        desired_update_ids = set(update_map)
        skipped_update_ids = set([])
        actual_updates = {}
        present_update_ids = [ k for k in desired_update_ids if k in self._cache ]
        for tx_id, entry in self.get_entries(tx_ids=present_update_ids):
            _discard, metadata, bytedata, flags = update_map[tx_id]
            # No-one should ever pass in field flags in normal circumstances.
            # In this case we use this to selectively merge the flagged fields in the update
            # to the cache entry data.
            fee = metadata.fee if flags & TxFlags.HasFee else entry.metadata.fee
            height = metadata.height if flags & TxFlags.HasHeight else entry.metadata.height
            position = metadata.position if flags & TxFlags.HasPosition else entry.metadata.position
            timestamp = (metadata.timestamp if flags & TxFlags.HasTimestamp
                else entry.metadata.timestamp)
            new_metadata = TxData(height, timestamp, position, fee)
            new_bytedata = bytedata if flags & TxFlags.HasByteData else entry.bytedata
            is_bytedata_cached = (flags & TxFlags.HasByteData) != 0 or entry.is_bytedata_cached()
            if metadata == new_metadata and (not is_bytedata_cached or bytedata == new_bytedata):
                self.logger.error("_update: skipped %s (unchanged)", tx_id)
                skipped_update_ids.add(tx_id)
            else:
                new_flags = self._adjust_field_flags(new_metadata,
                    entry.flags & ~TxFlags.STATE_MASK)
                new_flags |= entry.flags & TxFlags.STATE_MASK
                if new_bytedata is not None:
                    new_flags |= TxFlags.HasByteData
                self.logger.debug("_update: %s %r %s %r %s", tx_id, metadata,
                    TxFlags.to_repr(flags), new_metadata, TxFlags.to_repr(new_flags))
                actual_updates[tx_id] = TxCacheEntry(new_metadata, new_flags, new_bytedata,
                    entry.time_loaded, is_bytedata_cached)

        if len(actual_updates):
            self._cache.update(actual_updates)
            update_entries = [
                (tx_id, entry.metadata, entry.bytedata, entry.flags)
                for tx_id, entry in actual_updates.items()
            ]
            self._store.update_many(update_entries)

        return (desired_update_ids - set(actual_updates)) - set(skipped_update_ids)

    def update_or_add(self, upadds: List[Tuple[str, TxData, Optional[bytes], int]]) -> None:
        insert_ids = self._update(upadds)
        if len(insert_ids):
            self.add([ t for t in upadds if t[0] in insert_ids ])

    def update_flags(self, tx_id: str, flags: int, mask: Optional[int]=None) -> None:
        if mask is None:
            mask = TxFlags.METADATA_FIELD_MASK
        else:
            mask |= TxFlags.METADATA_FIELD_MASK

        entry = self.get_entry(tx_id)
        entry.flags = (entry.flags & mask) | (flags & ~TxFlags.METADATA_FIELD_MASK)
        self._store.update_flags(tx_id, flags, mask)

    def delete(self, tx_id: str):
        del self._cache[tx_id]
        self._store.delete(tx_id)

    def get_flags(self, tx_id: str) -> Optional[int]:
        entry = self.get_entry(tx_id)
        if entry is not None:
            return entry.flags

    # NOTE: Only used by unit tests at this time.
    def is_cached(self, tx_id: str) -> bool:
        return tx_id in self._cache

    def get_entry(self, tx_id: str, flags: Optional[int]=None,
            mask: Optional[int]=None) -> Optional[TxCacheEntry]:
        if tx_id in self._cache:
            entry = self._cache[tx_id]
            # self._cache_access[tx_id] = time.time()
            return entry if self._entry_visible(entry.flags, flags, mask) else None

        result = self._store.get(tx_id, flags, mask)
        if result is not None:
            metadata, bytedata, flags_get = result
            if bytedata is None or self._validate_transaction_bytes(tx_id, bytedata):
                entry = TxCacheEntry(metadata, flags_get, bytedata)
                self._cache[tx_id] = entry
                # self._cache_access[tx_id] = time.time()
                self.logger.debug("cache_addition: %r", (tx_id, entry, TxFlags.to_repr(flags),
                    mask))
                return entry if self._entry_visible(entry.flags, flags, mask) else None
            raise InvalidDataError(tx_id)

        # TODO: If something is requested that does not exist, it will miss the cache and wait
        # on the store access every time. It should be possible to cache misses and also maintain/
        # update them on other accesses. A complication is the flag/mask filtering, which will
        # not indicate presence of entries for the tx_id.
        return None

    def get_metadata(self, tx_id: str, flags: Optional[int]=None,
            mask: Optional[int]=None) -> Optional[TxData]:
        entry = self.get_entry(tx_id, flags, mask)
        return entry.metadata

    def get_transaction(self, tx_id: str, flags: Optional[int]=None,
            mask: Optional[int]=None) -> Optional[Transaction]:
        entry = self.get_entry(tx_id, flags, mask)
        if entry is not None:
            return entry.transaction

    def get_entries(self, flags: Optional[int]=None, mask: Optional[int]=None,
            tx_ids: Optional[Iterable[str]]=None) -> List[Tuple[str, TxCacheEntry]]:
        specific_tx_ids = None
        if tx_ids is not None:
            specific_tx_ids = [ tx_id for tx_id in tx_ids if tx_id not in self._cache ]

        cache_additions = []
        for tx_id, metadata, bytedata, get_flags in self._store.get_many(flags, mask,
                specific_tx_ids):
            # TODO: Evaluate whether this is necessary.
            if bytedata is not None and not self._validate_transaction_bytes(tx_id, bytedata):
                raise InvalidDataError(tx_id)
            cache_additions.append((tx_id, TxCacheEntry(metadata, get_flags, bytedata)))
        if len(cache_additions):
            self.logger.debug("cache_additions: %r", cache_additions)
        self._cache.update(cache_additions)

        access_time = time.time()
        results = []
        if specific_tx_ids is not None:
            for tx_id in tx_ids:
                entry = self._cache.get(tx_id)
                if entry is None:
                    raise MissingRowError(tx_id)
                if self._entry_visible(entry.flags, flags, mask):
                    # self._cache_access[tx_id] = access_time
                    results.append((tx_id, entry))
        else:
            results = cache_additions
            # self._cache_access.update([ (t[0], access_time) for t in cache_additions ])
        return results

    def get_transactions(self, flags: Optional[int]=None, mask: Optional[int]=None,
            tx_ids: Optional[Iterable[str]]=None) -> List[Tuple[str, Transaction]]:
        # TODO: Load in txbytes + metadata.
        results = []
        for tx_id, entry in self.get_entries(flags, mask, tx_ids):
            transaction = entry.transaction
            if transaction is not None:
                results.append((tx_id, transaction))
        return results

    def get_height(self, tx_id: str) -> Optional[int]:
        entry = self.get_entry(tx_id, mask=TxFlags.StateCleared|TxFlags.StateSettled)
        return entry.metadata.height if entry is not None else None

    def get_unsynced_ids(self) -> List[str]:
        # The expectation is that we will be updating these, so it is to our advantage to
        # cache them to save on the later fetch.
        entries = self.get_entries(flags=TxFlags.Unset, mask=TxFlags.HasByteData)
        return [ t[0] for t in entries ]

    def get_unverified_entries(self, watermark_height: int) -> Dict[str, int]:
        # TODO: Revise how we track this if the load is too high.
        # It may be that this can ever knowingly be cached without extra metadata and we
        # can just maintain an internal list.
        results = self.get_entries(
            flags=TxFlags.HasByteData | TxFlags.HasHeight,
            mask=TxFlags.HasByteData | TxFlags.HasTimestamp | TxFlags.HasPosition |
                 TxFlags.HasHeight)
        return [ t for t in results if 0 < t[1].metadata.height <= watermark_height ]


class WalletData:
    def __init__(self, wallet_path: str, aeskey: bytes) -> None:
        self.tx_store = TransactionStore(wallet_path, aeskey)
        self.txin_store = TransactionInputStore(wallet_path, aeskey)
        self.txout_store = TransactionOutputStore(wallet_path, aeskey)

        self.tx_cache = TxCache(self.tx_store)
        self.txin_cache = TxXputCache(self.txin_store, preload=True)
        self.txout_cache = TxXputCache(self.txout_store, preload=True)

    @property
    def tx(self) -> TransactionStore:
        return self.tx_cache

    @property
    def txin(self) -> TxXputCache:
        return self.txin_cache

    @property
    def txout(self) -> TxXputCache:
        return self.txout_cache
