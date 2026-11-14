"""接入网关分批切换的回归用例：端口冲突、单批回退、重试幂等与路由同步。

只依赖标准库 unittest，直接用服务层构造独立的内存仓库，避免污染运行中的示例数据。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.gateway import GatewayService
from app.services.router import RouterService
from app.store import Store, store


def _reset_store() -> None:
    fresh = Store()
    store._tables = fresh._tables  # noqa: SLF001 - 测试里重置单例仓库
    store._meta = {}  # noqa: SLF001


class GatewayBatchSwitchTest(unittest.TestCase):
    def setUp(self) -> None:
        _reset_store()
        self.service = GatewayService()
        self.router_service = RouterService()

    def _submit(self, batches, drain_seconds=10):
        return self.service.switch_batches(batches, drain_seconds=drain_seconds)

    def test_three_batches_second_rolls_back_only_itself(self):
        result = self._submit(
            [
                {"配置编号": "GATE-0001", "监听端口": 19081, "并发上限": 600},
                {"配置编号": "GATE-0002", "监听端口": 19082, "并发上限": 900},
                {"配置编号": "GATE-0003", "监听端口": 19083, "并发上限": 350},
            ]
        )
        statuses = [item["状态"] for item in result["results"]]
        self.assertEqual(statuses, ["已生效", "已回退", "已生效"])
        self.assertFalse(result["ok"])

        gateways = {row["配置编号"]: row for row in store.rows("gateway")}
        self.assertEqual(gateways["GATE-0001"]["监听端口"], 19081)
        self.assertEqual(gateways["GATE-0001"]["上一版监听端口"], 18081)
        self.assertEqual(gateways["GATE-0001"]["完成位置"], "第1/3批")
        self.assertEqual(gateways["GATE-0002"]["监听端口"], 18082)
        self.assertEqual(gateways["GATE-0002"]["完成位置"], "未执行")
        self.assertEqual(gateways["GATE-0003"]["监听端口"], 19083)
        self.assertEqual(gateways["GATE-0003"]["上一版监听端口"], 18083)

    def test_rollback_keeps_old_connections_and_closes_new_ones(self):
        result = self._submit(
            [
                {"配置编号": "GATE-0002", "监听端口": 19082},
            ]
        )
        batch_id = result["results"][0]["batch_id"]
        connections = self.service.list_connections(gateway_id=2)
        old = [row for row in connections if row["端口"] == 18082]
        new = [row for row in connections if row["端口"] == 19082]
        self.assertTrue(old)
        self.assertTrue(all(row["状态"] == "使用中" for row in old))
        self.assertTrue(new)
        self.assertTrue(all(row["状态"] == "已关闭" for row in new))
        self.assertTrue(all(row["批次编号"] == batch_id for row in new))

        plan = self.service.get_batch(batch_id)
        self.assertEqual(plan["状态"], "已回退")
        self.assertIsNone(plan["已完成位置"])
        self.assertEqual(plan["变更前"]["监听端口"], 18082)

    def test_success_batches_are_not_overwritten_by_retry(self):
        payload = [
            {"配置编号": "GATE-0001", "监听端口": 19081},
            {"配置编号": "GATE-0003", "监听端口": 19083},
        ]
        self._submit(payload)
        first_connections = [
            (row["连接编号"], row["状态"]) for row in self.service.list_connections(gateway_id=1)
        ]
        router_before = {
            row["规则编号"]: (row["目标端口"], row.get("同步批次"))
            for row in store.rows("router")
        }

        retry = self._submit(payload)
        self.assertEqual([item["状态"] for item in retry["results"]], ["已跳过", "已跳过"])
        self.assertEqual(
            [(row["连接编号"], row["状态"]) for row in self.service.list_connections(gateway_id=1)],
            first_connections,
        )
        self.assertEqual(
            {row["规则编号"]: (row["目标端口"], row.get("同步批次")) for row in store.rows("router")},
            router_before,
        )
        self.assertEqual(store.find("gateway", 1)["监听端口"], 19081)

    def test_same_port_only_first_batch_applies(self):
        result = self._submit(
            [
                {"配置编号": "GATE-0001", "监听端口": 19100},
                {"配置编号": "GATE-0003", "监听端口": 19100},
            ]
        )
        self.assertEqual(result["results"][0]["状态"], "已生效")
        self.assertEqual(result["results"][1]["状态"], "已回退")
        self.assertIn("仅首个通过校验", result["results"][1]["消息"])
        self.assertEqual(store.find("gateway", 3)["监听端口"], 18083)

    def test_router_sync_follows_each_batch(self):
        self._submit(
            [
                {"配置编号": "GATE-0001", "监听端口": 19081},
                {"配置编号": "GATE-0002", "监听端口": 19082},
            ]
        )
        routers = {row["规则编号"]: row for row in store.rows("router")}
        self.assertEqual(routers["ROUT-0001"]["目标端口"], 19081)
        self.assertEqual(routers["ROUT-0001"]["网关同步状态"], "已同步")
        self.assertEqual(routers["ROUT-0002"]["目标端口"], 18082)
        self.assertEqual(routers["ROUT-0002"]["网关同步状态"], "已回退")

    def test_duplicate_config_in_one_submission_is_rejected(self):
        result = self._submit(
            [
                {"配置编号": "GATE-0001", "监听端口": 19081},
                {"配置编号": "GATE-0001", "监听端口": 19082},
            ]
        )
        self.assertEqual(result["results"][0]["状态"], "已生效")
        self.assertEqual(result["results"][1]["状态"], "已回退")
        self.assertEqual(store.find("gateway", 1)["监听端口"], 19081)


if __name__ == "__main__":
    unittest.main()
