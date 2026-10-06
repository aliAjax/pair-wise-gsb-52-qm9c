"""HTTP 端到端：争议链路由、批次头、scope头与冲突详情。"""
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import BASE_DIR, build_service
from src.http_api import create_server
from src.repository import Repository
from src.rules import DomainRules
from src.audit import AuditRecorder


CREATE_DATA = {'student_id': 'S-300', 'disability': 'hearing', 'service_minutes': 600,
               'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}


class HttpChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = str(Path(self.temp.name) / "http.db")
        service = build_service(db)
        self.server = create_server("127.0.0.1", 0, service, BASE_DIR / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service = service

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _request(self, method, path, body=None, headers=None, expect_error=False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            if expect_error:
                return exc.code, payload
            raise AssertionError("unexpected %s: %s" % (exc.code, payload))

    def _headers(self, user, role, org="school-a", scopes=None, batch=None):
        headers = {"X-User-Id": user, "X-Role": role, "X-Org": org}
        if scopes:
            headers["X-Scopes"] = " ".join(scopes)
        if batch:
            headers["X-Batch-Id"] = batch
        return headers

    def test_full_chain_over_http(self):
        # 创建 + 同意 + 生效
        status, record = self._request("POST", "/api/records",
                                       {"reference": "IEP-H-1", "data": CREATE_DATA},
                                       self._headers("c", "case_manager"))
        self.assertEqual(status, 201)
        rid = record["id"]
        status, record = self._request("POST", "/api/records/%d/actions/consent" % rid,
                                       {"expected_version": 1, "data": {"guardian_confirmed": True, "consent_scope": "x"}},
                                       self._headers("p", "parent_rep"))
        status, record = self._request("POST", "/api/records/%d/actions/activate" % rid,
                                       {"expected_version": 2, "data": {}}, self._headers("m", "case_manager"))
        self.assertEqual(record["state"], "active")

        # 修订草案
        status, amendment = self._request("POST", "/api/records/%d/amendments" % rid,
                                          {"expected_version": 3, "data": {"amendment_reason": "加时",
                                                                           "updated_goals": ["g"], "service_minutes": 700}},
                                          self._headers("m", "case_manager", batch="batch-amend"))
        self.assertEqual(status, 201)
        self.assertEqual(amendment["status"], "proposed")

        # 家长异议（旧版本 -> 409 且保留 draft）
        status, err = self._request("POST", "/api/records/%d/disputes" % rid,
                                    {"expected_version": 3, "data": {"reason": "我的填写", "detail": "保留"}},
                                    self._headers("p", "parent_rep"), expect_error=True)
        self.assertEqual(status, 409)
        self.assertEqual(err["details"]["draft"]["reason"], "我的填写")
        self.assertEqual(err["details"]["current_version"], 4)

        # 用新版本提交异议并受理
        status, dispute = self._request("POST", "/api/records/%d/disputes" % rid,
                                        {"expected_version": 4, "data": {"reason": "缺口", "detail": "大"}},
                                        self._headers("p", "parent_rep", batch="batch-dispute"))
        did = dispute["dispute_id"]
        status, accepted = self._request("POST", "/api/disputes/%d/accept" % did, {},
                                         self._headers("a", "administrator"))
        self.assertEqual(accepted["frozen_amendments"], [amendment["amendment_id"]])

        # 复核人缺少 scope 被拒
        status, err = self._request("POST", "/api/disputes/%d/decide" % did,
                                    {"data": {"decision": "upheld", "decision_note": "n"}},
                                    self._headers("rv", "reviewer", scopes=["dispute:reject"]), expect_error=True)
        self.assertEqual(status, 403)

        # 正确 scope 裁定成立
        status, decision = self._request("POST", "/api/disputes/%d/decide" % did,
                                         {"data": {"decision": "upheld", "decision_note": "成立"}},
                                         self._headers("rv", "reviewer", scopes=["dispute:uphold"]))
        self.assertEqual(decision["status"], "upheld")

        # 批次查询
        status, batch = self._request("GET", "/api/batches/batch-dispute", None,
                                      self._headers("a", "administrator"))
        self.assertEqual(batch["status"], "committed")

        # 结案详情
        status, readiness = self._request("GET", "/api/records/%d/readiness" % rid, None,
                                          self._headers("a", "administrator"))
        self.assertIn("can_close", readiness)
        self.assertIn("blockers", readiness)


if __name__ == "__main__":
    unittest.main()
