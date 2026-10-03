import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from remnawave.client import (
    _HTTP_GET_RETRY_BUDGET,
    RemnawaveAPIError,
    RemnawaveClient,
    RemnawaveInvalidResponseError,
    RemnawaveTransportError,
    _Route,
)


class RemnawaveClientPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_all_users_loads_every_page(self):
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        client._request = AsyncMock(side_effect=[
            {
                "response": {
                    "users": [{"uuid": "1", "username": "first"}],
                    "total": 2,
                }
            },
            {
                "response": {
                    "users": [{"uuid": "2", "username": "second"}],
                    "total": 2,
                }
            },
        ])

        users = await client.get_all_users()

        self.assertEqual([user["username"] for user in users], ["first", "second"])
        self.assertEqual(
            client._request.await_args_list,
            [
                call("GET", "/api/users", params={"size": 1000, "start": 0}),
                call("GET", "/api/users", params={"size": 1000, "start": 1}),
            ],
        )

    async def test_get_all_users_stops_on_empty_response(self):
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        client._request = AsyncMock(return_value={"response": {"users": [], "total": 0}})

        self.assertEqual(await client.get_all_users(), [])


class RemnawaveClientRequestTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler) -> RemnawaveClient:
        client = RemnawaveClient(
            "https://panel.example",
            "test-token",
            transport=httpx.MockTransport(handler),
        )
        self.addAsyncCleanup(client.aclose)
        return client

    def make_failover_client(self, direct_handler, proxy_handler) -> RemnawaveClient:
        """Клиент с прямым маршрутом + запасным прокси-маршрутом на MockTransport."""
        client = RemnawaveClient(
            "https://panel.example",
            "test-token",
            transport=httpx.MockTransport(direct_handler),
        )
        proxy_client = httpx.AsyncClient(
            transport=httpx.MockTransport(proxy_handler),
            follow_redirects=False,
        )
        client._routes.append(_Route("proxy", proxy_client))
        self.addAsyncCleanup(client.aclose)
        return client

    def test_no_proxy_creates_single_route(self):
        with (
            patch("remnawave.client.httpx.AsyncHTTPTransport") as transport_factory,
            patch("remnawave.client.httpx.AsyncClient") as client_factory,
        ):
            RemnawaveClient("https://panel.example", "test-token")

        self.assertEqual(transport_factory.call_count, 1)
        self.assertEqual(client_factory.call_count, 1)
        self.assertIsNone(transport_factory.call_args.kwargs.get("proxy"))

    def test_proxy_url_adds_failover_route(self):
        proxy_url = "socks5://proxy-user:proxy-password@127.0.0.1:1080"

        with (
            patch("remnawave.client.httpx.AsyncHTTPTransport") as transport_factory,
            patch("remnawave.client.httpx.AsyncClient") as client_factory,
        ):
            RemnawaveClient(
                "https://panel.example",
                "test-token",
                proxy_url=proxy_url,
            )

        # Два маршрута: прямой (без proxy) и запасной (через proxy_url).
        self.assertEqual(client_factory.call_count, 2)
        proxies = [
            call.kwargs.get("proxy")
            for call in transport_factory.call_args_list
        ]
        self.assertIn(None, proxies)
        self.assertIn(proxy_url, proxies)

    async def test_get_retries_temporary_status_once(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(
                    503,
                    json={"error": "temporary"},
                    headers={"Retry-After": "0"},
                )
            return httpx.Response(200, json={"response": {"uuid": "user-1"}})

        client = self.make_client(handler)
        with patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await client._request("GET", "/api/users/user-1")

        self.assertEqual(result, {"response": {"uuid": "user-1"}})
        self.assertEqual(len(requests), 2)
        sleep.assert_awaited_once_with(0.0)

    async def test_mutation_does_not_retry_temporary_status(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(503, json={"error": "temporary"})

        client = self.make_client(handler)
        with patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(RemnawaveAPIError) as caught:
                await client._request("POST", "/api/users", json={"username": "u"})

        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(request_count, 1)
        sleep.assert_not_awaited()

    async def test_get_retries_read_timeout_once(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(200, json={"response": {"ok": True}})

        client = self.make_client(handler)
        with (
            patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("remnawave.client.random.uniform", return_value=0.1),
        ):
            result = await client._request("GET", "/api/users/user-1")

        self.assertEqual(result, {"response": {"ok": True}})
        self.assertEqual(request_count, 2)
        sleep.assert_awaited_once_with(0.1)

    async def test_mutation_transport_error_is_not_retried(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            raise httpx.ReadTimeout("timed out", request=request)

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveTransportError) as caught:
            await client._request("PATCH", "/api/users", json={"uuid": "user-1"})

        self.assertEqual(caught.exception.method, "PATCH")
        self.assertEqual(caught.exception.category, "ReadTimeout")
        self.assertEqual(request_count, 1)
        self.assertIsInstance(caught.exception.__cause__, httpx.ReadTimeout)

    async def test_get_transport_retry_is_exhausted_after_four_attempts(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            raise httpx.ReadTimeout("timed out", request=request)

        client = self.make_client(handler)
        with (
            patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("remnawave.client.random.uniform", return_value=0.1),
        ):
            with self.assertRaises(RemnawaveTransportError) as caught:
                await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.category, "ReadTimeout")
        self.assertEqual(request_count, 4)
        self.assertEqual(sleep.await_args_list, [call(0.1), call(0.1), call(0.1)])

    async def test_get_status_retry_is_exhausted_after_four_attempts(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                503,
                json={"error": "temporary"},
                headers={"Retry-After": "0"},
            )

        client = self.make_client(handler)
        with patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(RemnawaveAPIError) as caught:
                await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(request_count, 4)
        self.assertEqual(sleep.await_args_list, [call(0.0), call(0.0), call(0.0)])

    async def test_get_retry_stops_when_time_budget_exhausted(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            raise httpx.ReadTimeout("timed out", request=request)

        client = self.make_client(handler)
        # monotonic: старт для дедлайна = 0, следующая проверка уже за бюджетом →
        # ретраи прекращаются после первой попытки, не дожидаясь штатных 4.
        times = iter([0.0, _HTTP_GET_RETRY_BUDGET + 1.0])
        with (
            patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("remnawave.client.time.monotonic", side_effect=lambda: next(times)),
        ):
            with self.assertRaises(RemnawaveTransportError):
                await client._request("GET", "/api/users/user-1")

        self.assertEqual(request_count, 1)
        sleep.assert_not_awaited()

    async def test_get_fails_over_to_proxy_after_direct_transport_error(self):
        direct_calls = 0
        proxy_calls = 0

        def direct(request: httpx.Request) -> httpx.Response:
            nonlocal direct_calls
            direct_calls += 1
            raise httpx.ConnectTimeout("panel unreachable", request=request)

        def proxy(request: httpx.Request) -> httpx.Response:
            nonlocal proxy_calls
            proxy_calls += 1
            return httpx.Response(200, json={"response": {"ok": True}})

        client = self.make_failover_client(direct, proxy)
        with (
            patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock),
            patch("remnawave.client.random.uniform", return_value=0.0),
        ):
            result = await client._request("GET", "/api/system")

        self.assertEqual(result, {"response": {"ok": True}})
        # Прямой маршрут исчерпал все 4 попытки, затем ушли на прокси.
        self.assertEqual(direct_calls, 4)
        self.assertEqual(proxy_calls, 1)

    async def test_get_fails_over_to_proxy_after_exhausted_temporary_status(self):
        direct_calls = 0
        proxy_calls = 0

        def direct(request: httpx.Request) -> httpx.Response:
            nonlocal direct_calls
            direct_calls += 1
            return httpx.Response(503, json={"error": "temporary"}, headers={"Retry-After": "0"})

        def proxy(request: httpx.Request) -> httpx.Response:
            nonlocal proxy_calls
            proxy_calls += 1
            return httpx.Response(200, json={"response": {"ok": True}})

        client = self.make_failover_client(direct, proxy)
        with patch("remnawave.client.asyncio.sleep", new_callable=AsyncMock):
            result = await client._request("GET", "/api/system")

        self.assertEqual(result, {"response": {"ok": True}})
        self.assertEqual(direct_calls, 4)
        self.assertEqual(proxy_calls, 1)

    async def test_mutation_fails_over_to_proxy_on_connect_error(self):
        direct_calls = 0
        proxy_calls = 0

        def direct(request: httpx.Request) -> httpx.Response:
            nonlocal direct_calls
            direct_calls += 1
            raise httpx.ConnectError("panel unreachable", request=request)

        def proxy(request: httpx.Request) -> httpx.Response:
            nonlocal proxy_calls
            proxy_calls += 1
            return httpx.Response(200, json={"response": {"uuid": "user-1"}})

        client = self.make_failover_client(direct, proxy)
        result = await client._request("POST", "/api/users", json={"username": "u"})

        self.assertEqual(result, {"response": {"uuid": "user-1"}})
        # Мутация выполнена ровно один раз на каждом маршруте (без дублей).
        self.assertEqual(direct_calls, 1)
        self.assertEqual(proxy_calls, 1)

    async def test_mutation_does_not_fail_over_on_read_timeout(self):
        direct_calls = 0
        proxy_calls = 0

        def direct(request: httpx.Request) -> httpx.Response:
            nonlocal direct_calls
            direct_calls += 1
            raise httpx.ReadTimeout("ambiguous", request=request)

        def proxy(request: httpx.Request) -> httpx.Response:
            nonlocal proxy_calls
            proxy_calls += 1
            return httpx.Response(200, json={"response": {"uuid": "user-1"}})

        client = self.make_failover_client(direct, proxy)
        with self.assertRaises(RemnawaveTransportError) as caught:
            await client._request("PATCH", "/api/users", json={"uuid": "user-1"})

        # ReadTimeout неоднозначен: панель могла применить изменение — прокси НЕ трогаем.
        self.assertEqual(caught.exception.category, "ReadTimeout")
        self.assertEqual(direct_calls, 1)
        self.assertEqual(proxy_calls, 0)

    async def test_auth_error_is_not_retried(self):
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(401, json={"error": "unauthorized"})

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveAPIError) as caught:
            await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(request_count, 1)

    async def test_redirect_is_an_api_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "/login"})

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveAPIError) as caught:
            await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.status, 302)

    async def test_html_success_response_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>proxy placeholder</html>",
                headers={"Content-Type": "text/html; charset=utf-8"},
            )

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveInvalidResponseError) as caught:
            await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.reason, "unexpected content type")

    async def test_malformed_json_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"{not-json",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveInvalidResponseError) as caught:
            await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.reason, "malformed JSON")

    async def test_json_suffix_content_type_is_accepted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b'{"type":"temporary"}',
                headers={"Content-Type": "application/problem+json"},
            )

        client = self.make_client(handler)
        result = await client._request("GET", "/api/system")

        self.assertEqual(result, {"type": "temporary"})

    async def test_non_object_json_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"uuid": "user-1"}])

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveInvalidResponseError) as caught:
            await client._request("GET", "/api/users")

        self.assertEqual(caught.exception.reason, "unexpected JSON root")

    async def test_empty_success_response_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        client = self.make_client(handler)
        self.assertIsNone(
            await client._request(
                "DELETE",
                "/api/users/user-1",
                allow_empty=True,
            )
        )

    async def test_unexpected_empty_success_response_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveInvalidResponseError) as caught:
            await client._request("GET", "/api/users/user-1")

        self.assertEqual(caught.exception.reason, "empty response")

    async def test_api_error_message_does_not_expose_response_body(self):
        secret = "https://subscription.example/secret-token"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                text=f"subscriptionUrl={secret}",
                headers={"x-request-id": "request-123"},
            )

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveAPIError) as caught:
            await client._request("POST", "/api/users", json={})

        self.assertNotIn(secret, str(caught.exception))
        self.assertIn(secret, caught.exception.body)
        self.assertEqual(caught.exception.request_id, "request-123")

    async def test_request_id_is_single_line_and_bounded(self):
        raw_request_id = "request-control-character-" + ("x" * 200)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": "bad request"},
                headers={"x-request-id": raw_request_id},
            )

        client = self.make_client(handler)
        with self.assertRaises(RemnawaveAPIError) as caught:
            await client._request("POST", "/api/users", json={})

        self.assertEqual(len(caught.exception.request_id), 128)
        self.assertNotIn("\n", str(caught.exception))


class RemnawaveClientSystemStatsTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> RemnawaveClient:
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_full_response_is_normalized(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={
            "response": {
                "onlineStats": {"onlineNow": "42"},
                "users": {
                    "totalUsers": 100,
                    "statusCounts": {"ACTIVE": 80, "DISABLED": 20},
                },
                "nodes": {"totalOnline": 5},
            }
        })

        stats = await client.get_system_stats()

        self.assertEqual(stats, {
            "online_now": 42,
            "users_total": 100,
            "status_counts": {"ACTIVE": 80, "DISABLED": 20},
            "nodes_online": 5,
        })

    async def test_missing_nested_keys_fall_back_to_defaults(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {}})

        stats = await client.get_system_stats()

        self.assertEqual(stats, {
            "online_now": 0,
            "users_total": 0,
            "status_counts": {},
            "nodes_online": 0,
        })

    async def test_none_response_falls_back_to_defaults(self):
        client = self.make_client()
        client._request = AsyncMock(return_value=None)

        stats = await client.get_system_stats()

        self.assertEqual(stats, {
            "online_now": 0,
            "users_total": 0,
            "status_counts": {},
            "nodes_online": 0,
        })


class RemnawaveClientNodesTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> RemnawaveClient:
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_nodes_list_is_normalized_with_defaults(self):
        client = self.make_client()
        full_node = {
            "uuid": "n1",
            "name": "Node One",
            "countryCode": "US",
            "isConnected": True,
            "isNodeOnline": True,
            "isXrayRunning": True,
            "usersOnline": 12,
            "cpuCount": 4,
            "totalRam": "8589934592",
        }
        minimal_node: dict = {}
        client._request = AsyncMock(return_value={"response": [full_node, minimal_node]})

        nodes = await client.get_nodes()

        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0], {
            "uuid": "n1",
            "name": "Node One",
            "country_code": "US",
            "is_connected": True,
            "is_online": True,
            "xray_running": True,
            "users_online": 12,
            "cpu_count": 4,
            "total_ram_bytes": 8589934592,
            "cpu_usage": None,
            "ram_usage": None,
            "raw": full_node,
        })
        self.assertEqual(nodes[1], {
            "uuid": "",
            "name": "Без имени",
            "country_code": "",
            "is_connected": False,
            "is_online": False,
            "xray_running": False,
            "users_online": 0,
            "cpu_count": None,
            "total_ram_bytes": None,
            "cpu_usage": None,
            "ram_usage": None,
            "raw": minimal_node,
        })

    async def test_node_cpu_ram_extracted_from_system(self):
        client = self.make_client()
        node = {
            "uuid": "n1",
            "name": "de-1",
            "isConnected": True,
            "usersOnline": 31,
            "system": {
                "info": {"cpus": 4, "memoryTotal": 4000},
                "stats": {"memoryUsed": 1000, "loadAvg": [0.4, 0.3, 0.2]},
            },
        }
        client._request = AsyncMock(return_value={"response": [node]})

        nodes = await client.get_nodes()

        # RAM% = 1000/4000*100 = 25; CPU% = 0.4/4*100 = 10.
        self.assertAlmostEqual(nodes[0]["ram_usage"], 25.0)
        self.assertAlmostEqual(nodes[0]["cpu_usage"], 10.0)

    async def test_node_load_missing_or_malformed_system_is_none(self):
        client = self.make_client()
        nodes_payload = [
            {"uuid": "a", "name": "a"},                       # нет system
            {"uuid": "b", "name": "b", "system": "oops"},     # system не dict
            {"uuid": "c", "name": "c", "system": {"stats": {"loadAvg": []}}},  # пустой loadAvg
        ]
        client._request = AsyncMock(return_value={"response": nodes_payload})

        nodes = await client.get_nodes()

        for node in nodes:
            self.assertIsNone(node["cpu_usage"])
            self.assertIsNone(node["ram_usage"])

    async def test_nodes_wrapped_in_dict_key_are_supported(self):
        client = self.make_client()
        node = {"uuid": "n1", "name": "Node One", "isConnected": True}
        client._request = AsyncMock(return_value={"response": {"nodes": [node]}})

        nodes = await client.get_nodes()

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["uuid"], "n1")
        self.assertTrue(nodes[0]["is_connected"])
        # isNodeOnline отсутствует — fallback на isConnected.
        self.assertTrue(nodes[0]["is_online"])

    async def test_empty_response_returns_empty_list(self):
        client = self.make_client()
        client._request = AsyncMock(return_value=None)

        self.assertEqual(await client.get_nodes(), [])

    async def test_unrecognizable_response_returns_empty_list(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"unexpected": "shape"}})

        self.assertEqual(await client.get_nodes(), [])


class RemnawaveClientNodesMetricsTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> RemnawaveClient:
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_list_under_nodes_key_is_parsed(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={
            "response": {
                "nodes": [
                    {"uuid": "n1", "cpuUsage": 55.5, "memoryUsage": 70.2},
                    {"nodeUuid": "n2", "cpu": "12.3", "ramUsagePercent": "40"},
                ]
            }
        })

        metrics = await client.get_nodes_metrics()

        self.assertEqual(metrics, {
            "n1": {"cpu_usage": 55.5, "ram_usage": 70.2},
            "n2": {"cpu_usage": 12.3, "ram_usage": 40.0},
        })

    async def test_dict_mapping_of_node_key_to_metrics_is_parsed(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={
            "response": {
                "node-a": {"cpu": 10.0, "ram": 55.0},
                "node-b": {"cpu": 20.0, "ram": 65.0},
            }
        })

        metrics = await client.get_nodes_metrics()

        self.assertEqual(metrics, {
            "node-a": {"cpu_usage": 10.0, "ram_usage": 55.0},
            "node-b": {"cpu_usage": 20.0, "ram_usage": 65.0},
        })

    async def test_unrecognizable_response_returns_empty_dict(self):
        client = self.make_client()
        client._request = AsyncMock(return_value={"response": {"foo": "bar"}})

        self.assertEqual(await client.get_nodes_metrics(), {})

    async def test_none_response_returns_empty_dict(self):
        client = self.make_client()
        client._request = AsyncMock(return_value=None)

        self.assertEqual(await client.get_nodes_metrics(), {})


if __name__ == "__main__":
    unittest.main()


class RevokeUserSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoke_posts_without_body_and_returns_user(self):
        """
        Тело не отправляем намеренно: с `revokeOnlyPasswords` панель оставила бы
        прежний shortUuid, то есть утёкшая ссылка продолжила бы работать.
        """
        client = RemnawaveClient("https://panel.example", "token")
        self.addAsyncCleanup(client.aclose)
        client._request = AsyncMock(
            return_value={"response": {"uuid": "u-1", "subscriptionUrl": "https://sub/new"}}
        )

        user = await client.revoke_user_subscription("u-1")

        self.assertEqual(user["subscriptionUrl"], "https://sub/new")
        client._request.assert_awaited_once_with("POST", "/api/users/u-1/actions/revoke")

