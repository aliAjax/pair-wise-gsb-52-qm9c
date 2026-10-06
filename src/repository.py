"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS disputes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    topic TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    basis_revision INTEGER NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    raised_by TEXT NOT NULL,
                    resolved_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS amendments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    basis_version INTEGER NOT NULL,
                    proposed_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS service_rows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    basis_revision INTEGER NOT NULL,
                    basis_snapshot TEXT NOT NULL,
                    batch_key TEXT NOT NULL DEFAULT '',
                    voided_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS write_batches (
                    batch_key TEXT PRIMARY KEY,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'running',
                    operations INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    actor_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    conflict_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, actor_id, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_disputes_record ON disputes(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_amendments_record ON amendments(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_service_record ON service_rows(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _json_row(row: sqlite3.Row, *columns: str) -> Dict[str, Any]:
        item = dict(row)
        for column in columns:
            if item.get(column):
                item[column] = json.loads(item[column])
        return item

    # -- 基础记录 --------------------------------------------------------

    def create(
        self,
        reference: str,
        state: str,
        payload: Dict[str, Any],
        actor_id: str,
        seed_services: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                for seed in seed_services or []:
                    connection.execute(
                        "INSERT INTO service_rows(record_id,kind,minutes,provider,status,basis_revision,basis_snapshot,batch_key,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (record_id, seed["kind"], int(seed["minutes"]), seed.get("provider", ""), "confirmed",
                         int(seed["basis_revision"]), json.dumps(seed["basis_snapshot"], ensure_ascii=False, sort_keys=True),
                         seed.get("batch_key", ""), now, now),
                    )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    # -- 事务工作区：一次业务动作 = 版本自增 + 审计，可附带多个写入 -------

    def workspace(self, record_id: int, expected_version: int, actor_id: str):
        return _Workspace(self, record_id, expected_version, actor_id)

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        with self.workspace(record_id, expected_version, actor_id) as ws:
            ws.apply_record(state, payload, action, details)
            result = ws.record
        return result

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # -- 争议 ------------------------------------------------------------

    def insert_dispute(self, connection: sqlite3.Connection, record_id: int, data: Dict[str, Any], actor_id: str, now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO disputes(record_id,topic,reason,status,basis_revision,raised_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, data["topic"], data["reason"], "open", int(data["basis_revision"]), actor_id, now),
        )
        return int(cursor.lastrowid)

    def list_disputes(self, record_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM disputes WHERE record_id=? AND status=? ORDER BY id", (record_id, status)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM disputes WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_dispute(self, record_id: int, dispute_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM disputes WHERE id=? AND record_id=?", (dispute_id, record_id)).fetchone()
        if row is None:
            raise NotFound("异议不存在")
        return dict(row)

    def resolve_dispute(self, connection: sqlite3.Connection, dispute_id: int, data: Dict[str, Any], actor_id: str, now: str) -> None:
        connection.execute(
            "UPDATE disputes SET status='resolved',outcome=?,note=?,resolved_by=?,resolved_at=? WHERE id=?",
            (data["outcome"], data["note"], actor_id, now, dispute_id),
        )

    # -- 修订 ------------------------------------------------------------

    def insert_amendment(self, connection: sqlite3.Connection, record_id: int, data: Dict[str, Any], actor_id: str, now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO amendments(record_id,revision,reason,changes,status,basis_version,proposed_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (record_id, int(data["revision"]), data["amendment_reason"],
             json.dumps(data["changes"], ensure_ascii=False, sort_keys=True), "proposed",
             int(data["basis_version"]), actor_id, now),
        )
        return int(cursor.lastrowid)

    def list_amendments(self, record_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if status:
                rows = connection.execute("SELECT * FROM amendments WHERE record_id=? AND status=? ORDER BY id", (record_id, status)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM amendments WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._json_row(row, "changes") for row in rows]

    def get_amendment(self, record_id: int, amendment_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM amendments WHERE id=? AND record_id=?", (amendment_id, record_id)).fetchone()
        if row is None:
            raise NotFound("修订不存在")
        return self._json_row(row, "changes")

    def set_amendment_status(self, connection: sqlite3.Connection, amendment_id: int, status: str, actor_id: str, now: str) -> None:
        connection.execute(
            "UPDATE amendments SET status=?,confirmed_by=?,confirmed_at=? WHERE id=?",
            (status, actor_id, now if status == "confirmed" else "", amendment_id),
        )

    # -- 服务记录 --------------------------------------------------------

    def insert_service_row(self, connection: sqlite3.Connection, record_id: int, row: Dict[str, Any], now: str) -> int:
        cursor = connection.execute(
            "INSERT INTO service_rows(record_id,kind,minutes,provider,status,basis_revision,basis_snapshot,batch_key,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (record_id, row["kind"], int(row["minutes"]), row.get("provider", ""), row.get("status", "confirmed"),
             int(row["basis_revision"]), json.dumps(row["basis_snapshot"], ensure_ascii=False, sort_keys=True),
             row.get("batch_key", ""), now, now),
        )
        return int(cursor.lastrowid)

    def list_service_rows(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM service_rows WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._json_row(row, "basis_snapshot") for row in rows]

    def get_service_row(self, record_id: int, service_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM service_rows WHERE id=? AND record_id=?", (service_id, record_id)).fetchone()
        if row is None:
            raise NotFound("服务记录不存在")
        return self._json_row(row, "basis_snapshot")

    def pending_makeup_rows(self, connection: sqlite3.Connection, record_id: int) -> List[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM service_rows WHERE record_id=? AND kind='makeup' AND status='pending' ORDER BY id",
            (record_id,),
        ).fetchall()

    def confirmed_minutes_conn(self, connection: sqlite3.Connection, record_id: int) -> int:
        row = connection.execute(
            "SELECT COALESCE(SUM(minutes),0) AS total FROM service_rows WHERE record_id=? AND status='confirmed'",
            (record_id,),
        ).fetchone()
        return int(row["total"])

    def open_disputes_conn(self, connection: sqlite3.Connection, record_id: int) -> List[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM disputes WHERE record_id=? AND status='open' ORDER BY id",
            (record_id,),
        ).fetchall()

    def get_dispute_conn(self, connection: sqlite3.Connection, record_id: int, dispute_id: int) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM disputes WHERE id=? AND record_id=?", (dispute_id, record_id)
        ).fetchone()

    def amendments_conn(self, connection: sqlite3.Connection, record_id: int, status: Optional[str] = None) -> List[sqlite3.Row]:
        if status:
            return connection.execute(
                "SELECT * FROM amendments WHERE record_id=? AND status=? ORDER BY id", (record_id, status)
            ).fetchall()
        return connection.execute("SELECT * FROM amendments WHERE record_id=? ORDER BY id", (record_id,)).fetchall()

    def get_amendment_conn(self, connection: sqlite3.Connection, record_id: int, amendment_id: int) -> Optional[sqlite3.Row]:
        return connection.execute(
            "SELECT * FROM amendments WHERE id=? AND record_id=?", (amendment_id, record_id)
        ).fetchone()

    def set_amendments_status_conn(
        self, connection: sqlite3.Connection, record_id: int, from_status: str, to_status: str
    ) -> int:
        cursor = connection.execute(
            "UPDATE amendments SET status=? WHERE record_id=? AND status=?",
            (to_status, record_id, from_status),
        )
        return cursor.rowcount

    def void_pending_makeup_conn(
        self, connection: sqlite3.Connection, record_id: int, reason: str, now: str, basis_revision: Optional[int] = None
    ) -> int:
        if basis_revision is None:
            cursor = connection.execute(
                "UPDATE service_rows SET status='void',voided_reason=?,updated_at=? WHERE record_id=? AND kind='makeup' AND status='pending'",
                (reason, now, record_id),
            )
        else:
            cursor = connection.execute(
                "UPDATE service_rows SET status='void',voided_reason=?,updated_at=? WHERE record_id=? AND kind='makeup' AND status='pending' AND basis_revision=?",
                (reason, now, record_id, int(basis_revision)),
            )
        return cursor.rowcount

    def update_service_row_conn(self, connection: sqlite3.Connection, service_id: int, fields: Dict[str, Any], now: str) -> None:
        assignments = ", ".join("%s=?" % key for key in fields)
        connection.execute(
            "UPDATE service_rows SET %s,updated_at=? WHERE id=?" % assignments,
            list(fields.values()) + [now, service_id],
        )

    def delete_batch_service_rows_conn(self, connection: sqlite3.Connection, batch_key: str) -> None:
        connection.execute("DELETE FROM service_rows WHERE batch_key=? AND status != 'confirmed'", (batch_key,))

    def void_pending_makeup(self, connection: sqlite3.Connection, record_id: int, reason: str, now: str) -> int:
        cursor = connection.execute(
            "UPDATE service_rows SET status='void',voided_reason=?,updated_at=? WHERE record_id=? AND kind='makeup' AND status='pending'",
            (reason, now, record_id),
        )
        return cursor.rowcount

    def set_service_status(self, connection: sqlite3.Connection, service_id: int, minutes: int, status: str, now: str) -> None:
        connection.execute(
            "UPDATE service_rows SET status=?,minutes=?,updated_at=? WHERE id=?",
            (status, int(minutes), now, service_id),
        )

    def confirmed_minutes(self, record_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(minutes),0) AS total FROM service_rows WHERE record_id=? AND status='confirmed'",
                (record_id,),
            ).fetchone()
        return int(row["total"])

    def stale_confirmed_rows(self, record_id: int, current_revision: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,basis_revision FROM service_rows WHERE record_id=? AND status='confirmed' AND basis_revision>?",
                (record_id, current_revision),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- 写入批次：失败后从完整批次恢复，重试幂等 ------------------------

    def get_batch(self, batch_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM write_batches WHERE batch_key=?", (batch_key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    def start_batch(self, batch_key: str, record_id: int, operations: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM write_batches WHERE batch_key=?", (batch_key,)).fetchone()
            if existing is not None:
                connection.rollback()
                item = dict(existing)
                item["result"] = json.loads(item["result"]) if item["result"] else None
                return item
            connection.execute(
                "INSERT INTO write_batches(batch_key,record_id,status,operations,result,actor_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (batch_key, record_id, "running", int(operations), "", actor_id, now, now),
            )
            connection.commit()
        return {"batch_key": batch_key, "record_id": record_id, "status": "running", "operations": operations, "result": None}

    def complete_batch(self, connection: sqlite3.Connection, batch_key: str, result: Dict[str, Any], now: str) -> None:
        connection.execute(
            "UPDATE write_batches SET status='done',result=?,updated_at=? WHERE batch_key=?",
            (json.dumps(result, ensure_ascii=False, sort_keys=True), now, batch_key),
        )

    def batch_workspace(self, batch_key: str, record_id: int, expected_version: int, actor_id: str):
        return _Workspace(self, record_id, expected_version, actor_id, batch_key=batch_key)

    # -- 并发草稿：后到者保留填写内容并看到版本冲突 ----------------------

    def save_draft(
        self,
        record_id: int,
        actor_id: str,
        kind: str,
        payload: Dict[str, Any],
        base_version: int,
        conflict_version: int,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO drafts(record_id,actor_id,kind,payload,base_version,conflict_version,created_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(record_id,actor_id,kind) DO UPDATE SET"
                " payload=excluded.payload,base_version=excluded.base_version,"
                "conflict_version=excluded.conflict_version,created_at=excluded.created_at",
                (record_id, actor_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 int(base_version), int(conflict_version), now),
            )
            row = connection.execute(
                "SELECT * FROM drafts WHERE record_id=? AND actor_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                (record_id, actor_id, kind),
            ).fetchone()
        return self._json_row(row, "payload")

    def list_drafts(self, record_id: int, actor_id: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM drafts WHERE record_id=?"
        params: List[Any] = [record_id]
        if actor_id:
            sql += " AND actor_id=?"
            params.append(actor_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._json_row(row, "payload") for row in rows]

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


class _Workspace:
    """单事务工作区：进入即锁定并校验乐观版本；所有写入随事务一起提交或回滚。"""

    def __init__(self, repository: Repository, record_id: int, expected_version: int, actor_id: str, batch_key: str = "") -> None:
        self.repo = repository
        self.record_id = record_id
        self.expected_version = int(expected_version)
        self.actor_id = actor_id
        self.batch_key = batch_key
        self.connection: Optional[sqlite3.Connection] = None
        self.now = _now()
        self.record: Optional[Dict[str, Any]] = None
        self.next_version = self.expected_version
        self.last_audit_id: Optional[int] = None

    def __enter__(self) -> "_Workspace":
        self.connection = self.repo._connect()
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute("SELECT * FROM records WHERE id=?", (self.record_id,)).fetchone()
        if row is None:
            self.connection.rollback()
            raise NotFound("记录不存在")
        if int(row["version"]) != self.expected_version:
            current = self.repo._row(row)
            self.connection.rollback()
            raise Conflict(
                "版本冲突，请刷新后重试",
                {"expected_version": self.expected_version, "current_version": current["version"], "state": current["state"]},
            )
        self.record = self.repo._row(row)
        self.next_version = self.expected_version + 1
        return self

    def apply_record(self, state: str, payload: Dict[str, Any], action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        assert self.connection is not None and self.record is not None
        self.connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, self.next_version, json.dumps(payload, ensure_ascii=False, sort_keys=True),
             self.actor_id, self.now, self.record_id),
        )
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (self.record_id, action, self.actor_id, self.next_version,
             json.dumps(details, ensure_ascii=False, sort_keys=True), self.now),
        )
        self.last_audit_id = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        self.record["state"] = state
        self.record["version"] = self.next_version
        self.record["payload"] = payload
        self.record["updated_by"] = self.actor_id
        self.record["updated_at"] = self.now
        self.next_version += 1
        return self.record

    def audit(self, action: str, details: Dict[str, Any], version: Optional[int] = None) -> None:
        assert self.connection is not None
        cursor = self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (self.record_id, action, self.actor_id, version or (self.record["version"] if self.record else 1),
             json.dumps(details, ensure_ascii=False, sort_keys=True), self.now),
        )
        self.last_audit_id = int(cursor.lastrowid)

    def merge_last_audit(self, key: str, value: Any) -> None:
        """把附加结果并入本动作已写入的审计详情，保证一个动作只有一条审计事件。"""
        assert self.connection is not None
        if self.last_audit_id is None:
            return
        row = self.connection.execute("SELECT details FROM audit_events WHERE id=?", (self.last_audit_id,)).fetchone()
        details = json.loads(row["details"]) if row and row["details"] else {}
        details[key] = value
        self.connection.execute("UPDATE audit_events SET details=? WHERE id=?",
                                (json.dumps(details, ensure_ascii=False, sort_keys=True), self.last_audit_id))

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.connection is not None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
            self.connection = None
