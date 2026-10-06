"""SQLite 表结构与事务访问。

所有跨表写入走事务网关（TxGateway），支持乐观并发、幂等键与
write_batches 完整批次恢复；失败重放不会重复生成补服务记录。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: Any) -> Any:
    return json.loads(value) if value is not None else None


class TxGateway:
    """单次 BEGIN IMMEDIATE 事务内可用的全部原语。提交由调用方控制。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 主记录 ----
    def lock_record(self, record_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return row

    @staticmethod
    def hydrate(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def get_record(self, record_id: int) -> Dict[str, Any]:
        row = self.lock_record(record_id)
        return self.hydrate(row)

    def bump_record(self, record_id: int, expected_version: int, state: Optional[str], payload: Dict[str, Any], actor_id: str) -> int:
        row = self.connection.execute("SELECT version,state FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        self.connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state if state is not None else row["state"], version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, _now(), record_id),
        )
        return version

    def insert_snapshot(self, record_id: int, plan_version: int, label: str, snapshot: Dict[str, Any], actor_id: str) -> None:
        self.connection.execute(
            "INSERT INTO plan_snapshots(record_id,plan_version,label,snapshot,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, plan_version, label, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor_id, _now()),
        )

    def latest_snapshot_version(self, record_id: int) -> Optional[int]:
        row = self.connection.execute("SELECT MAX(plan_version) AS v FROM plan_snapshots WHERE record_id=?", (record_id,)).fetchone()
        value = row["v"] if row is not None else None
        return int(value) if value is not None else None

    def add_log(self, record_id: int, actor_id: str, action: str, version: int, details: Dict[str, Any], idem_key: Optional[str] = None) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,idem_key,created_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), idem_key, _now()),
        )

    # ---- 争议 ----
    def insert_dispute(self, record_id: int, reason: str, detail: str, actor_id: str, record_version: int) -> int:
        now = _now()
        cursor = self.connection.execute(
            "INSERT INTO disputes(record_id,status,reason,detail,raised_by,record_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (record_id, "open", reason, detail, actor_id, record_version, now, now),
        )
        return int(cursor.lastrowid)

    def lock_dispute(self, dispute_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM disputes WHERE id=?", (dispute_id,)).fetchone()
        if row is None:
            raise NotFound("争议不存在")
        return row

    @staticmethod
    def dispute_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def update_dispute(self, dispute_id: int, status: str, actor_id: str, decision_note: Optional[str] = None) -> None:
        self.connection.execute(
            "UPDATE disputes SET status=?,decided_by=?,decision_note=COALESCE(?,decision_note),updated_at=? WHERE id=?",
            (status, actor_id, decision_note, _now(), dispute_id),
        )

    def list_disputes(self, record_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM disputes WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 计划修订 ----
    def insert_amendment(self, record_id: int, expected_version: int, base_version: int, content: Dict[str, Any], actor_id: str) -> int:
        now = _now()
        cursor = self.connection.execute(
            "INSERT INTO amendments(record_id,status,base_version,confirmed_version,content,raised_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (record_id, "proposed", base_version, None, json.dumps(content, ensure_ascii=False, sort_keys=True), actor_id, now, now),
        )
        return int(cursor.lastrowid)

    def lock_amendment(self, amendment_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM amendments WHERE id=?", (amendment_id,)).fetchone()
        if row is None:
            raise NotFound("修订不存在")
        return row

    @staticmethod
    def amendment_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["content"] = json.loads(item["content"])
        return item

    def list_amendments(self, record_id: int, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self.connection.execute(
                "SELECT * FROM amendments WHERE record_id=? AND status IN (%s) ORDER BY id" % placeholders,
                [record_id] + list(statuses),
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM amendments WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item["content"])
            result.append(item)
        return result

    def update_amendment(self, amendment_id: int, status: str, confirmed_version: Optional[int] = None) -> None:
        self.connection.execute(
            "UPDATE amendments SET status=?,confirmed_version=COALESCE(?,confirmed_version),updated_at=? WHERE id=?",
            (status, confirmed_version, _now(), amendment_id),
        )

    # ---- 服务台账与补服务 ----
    def insert_service_entry(self, record_id: int, entry_type: str, minutes: int, basis_version: int, provider: str, idem_key: str, payload: Dict[str, Any]) -> int:
        now = _now()
        cursor = self.connection.execute(
            "INSERT INTO service_entries(record_id,entry_type,minutes,basis_version,plan_snapshot,provider,status,idem_key,created_at,confirmed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (record_id, entry_type, minutes, basis_version, json.dumps(payload, ensure_ascii=False, sort_keys=True),
             provider, "confirmed" if entry_type in ("regular", "opening") else "planned", idem_key, now, now if entry_type in ("regular", "opening") else None),
        )
        return int(cursor.lastrowid)

    def get_entry_by_idem(self, record_id: int, idem_key: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            "SELECT * FROM service_entries WHERE record_id=? AND idem_key=?", (record_id, idem_key)
        ).fetchone()
        return self._entry_dict(row) if row is not None else None

    def list_service_entries(self, record_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM service_entries WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._entry_dict(row) for row in rows]

    def lock_makeup(self, makeup_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM service_entries WHERE id=? AND entry_type='makeup'", (makeup_id,)).fetchone()
        if row is None:
            raise NotFound("补服务记录不存在")
        return row

    def confirm_makeup(self, makeup_id: int, basis_version: int, snapshot: Dict[str, Any], provider: str) -> None:
        self.connection.execute(
            "UPDATE service_entries SET status='confirmed',basis_version=?,plan_snapshot=?,provider=?,confirmed_at=? WHERE id=?",
            (basis_version, json.dumps(snapshot, ensure_ascii=False, sort_keys=True), provider, _now(), makeup_id),
        )

    def void_planned_makeup(self, record_id: int) -> int:
        cursor = self.connection.execute(
            "UPDATE service_entries SET status='void' WHERE record_id=? AND entry_type='makeup' AND status='planned'",
            (record_id,),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _entry_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["plan_snapshot"] = json.loads(item["plan_snapshot"])
        return item

    # ---- 快照查询 ----
    def list_snapshots(self, record_id: int) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM plan_snapshots WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            result.append(item)
        return result

    # ---- 批次 ----
    def reserve_batch(self, batch_id: str, record_id: int, operation: str, payload: Dict[str, Any], actor_id: str) -> Optional[sqlite3.Row]:
        """登记批次。已存在时返回旧行（恢复路径），冲突键返回None交给上层判冲突。"""
        now = _now()
        try:
            self.connection.execute(
                "INSERT INTO write_batches(batch_id,record_id,operation,request_payload,status,created_by,created_at,updated_at,response_payload,attempts) "
                "VALUES(?,?,?,?,?,?,?,?,?,0)",
                (batch_id, record_id, operation, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 "reserved", actor_id, now, now, None),
            )
            return None
        except sqlite3.IntegrityError:
            row = self.connection.execute("SELECT * FROM write_batches WHERE batch_id=?", (batch_id,)).fetchone()
            return row

    def lock_batch(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM write_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return row

    def mark_batch(self, batch_id: str, status: str, response: Optional[Dict[str, Any]] = None, increment_attempt: bool = False) -> None:
        self.connection.execute(
            "UPDATE write_batches SET status=?,response_payload=COALESCE(?,response_payload),"
            "attempts=attempts+?,updated_at=? WHERE batch_id=?",
            (status, json.dumps(response, ensure_ascii=False, sort_keys=True) if response is not None else None,
             1 if increment_attempt else 0, _now(), batch_id),
        )

    @staticmethod
    def batch_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["request_payload"] = json.loads(item["request_payload"])
        item["response_payload"] = json.loads(item["response_payload"]) if item["response_payload"] else None
        return item


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
                    owner_org TEXT NOT NULL DEFAULT '',
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
                    idem_key TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    plan_version INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(record_id, plan_version, label)
                );
                CREATE TABLE IF NOT EXISTS disputes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    raised_by TEXT NOT NULL,
                    decided_by TEXT,
                    decision_note TEXT,
                    record_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS amendments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    confirmed_version INTEGER,
                    content TEXT NOT NULL,
                    raised_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    entry_type TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    basis_version INTEGER NOT NULL,
                    plan_snapshot TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS write_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    operation TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    response_payload TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_disputes_record ON disputes(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_amendments_record ON amendments(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_service_record ON service_entries(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_service_idem ON service_entries(record_id, idem_key);
                CREATE INDEX IF NOT EXISTS idx_snapshots_record ON plan_snapshots(record_id, id);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "owner_org" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN owner_org TEXT NOT NULL DEFAULT ''")
            audit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "idem_key" not in audit_columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN idem_key TEXT")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    class _Transaction:
        def __init__(self, repository: "Repository") -> None:
            self.repository = repository
            self.connection: Optional[sqlite3.Connection] = None
            self.committed = False

        def __enter__(self) -> TxGateway:
            self.connection = self.repository._connect()
            self.connection.execute("BEGIN IMMEDIATE")
            return TxGateway(self.connection)

        def __exit__(self, exc_type, exc, tb) -> bool:
            assert self.connection is not None
            if exc_type is not None and not self.committed:
                self.connection.rollback()
            self.connection.close()
            return False

    def transaction(self) -> "Repository._Transaction":
        return self._Transaction(self)

    def commit(self, tx: "_Transaction") -> None:
        assert tx.connection is not None
        if getattr(self, "_fail_before_commit", 0) > 0:
            self._fail_before_commit -= 1
            tx.connection.rollback()
            raise sqlite3.OperationalError("注入失败：提交前写入中断（完整批次可恢复）")
        tx.connection.commit()
        tx.committed = True
        if getattr(self, "_fail_after_commit", 0) > 0:
            self._fail_after_commit -= 1
            raise sqlite3.OperationalError("注入失败：提交后响应丢失（重试幂等恢复）")

    def inject_failure(self, before_commit: int = 0, after_commit: int = 0) -> None:
        """测试钩子：注入提交点失败。"""
        self._fail_before_commit = before_commit
        self._fail_after_commit = after_commit

    # ---- 读路径（各自短事务） ----
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

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, owner_org: str = "") -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,owner_org,created_by,updated_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), owner_org, actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,idem_key,created_at) VALUES(?,?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), None, now),
                )
                connection.execute(
                    "INSERT INTO plan_snapshots(record_id,plan_version,label,snapshot,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, 1, "created", json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now),
                )
                if int(payload.get("delivered_minutes", 0)) > 0:
                    connection.execute(
                        "INSERT INTO service_entries(record_id,entry_type,minutes,basis_version,plan_snapshot,provider,status,idem_key,created_at,confirmed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (record_id, "opening", int(payload["delivered_minutes"]), 1,
                         json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, "confirmed",
                         "opening-v1", now, now),
                    )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

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

    # ---- 只读聚合：争议链详情 ----
    def disputes(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM disputes WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [dict(row) for row in rows]

    def amendments(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM amendments WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["content"] = json.loads(item["content"])
            result.append(item)
        return result

    def service_entries(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM service_entries WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["plan_snapshot"] = json.loads(item["plan_snapshot"])
            result.append(item)
        return result

    def snapshots(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM plan_snapshots WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            result.append(item)
        return result

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
