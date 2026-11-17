"""Tests for node config generation."""

from tests.conftest import create_member, create_pool


class TestNodeConfig:
    async def test_generate_nanio_only(self, client, mock_nginx):
        pool = await create_pool(client, "node-nanio")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nanio-only"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_type"] == "nanio-only"
        assert len(data["files"]) >= 1
        paths = [f["path"] for f in data["files"]]
        assert any("nanio" in p for p in paths)

    async def test_generate_nginx_only(self, client, mock_nginx):
        pool = await create_pool(client, "node-nginx", pool_type="http")
        member = await create_member(client, pool["id"], "10.0.0.1:8080", role="primary")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nginx-only"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_type"] == "nginx-only"
        paths = [f["path"] for f in data["files"]]
        assert any("nginx" in p for p in paths)

    async def test_generate_nginx_nanio(self, client, mock_nginx):
        pool = await create_pool(client, "node-both")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nginx-nanio"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_type"] == "nginx-nanio"
        assert len(data["files"]) >= 2

    async def test_invalid_node_type(self, client, mock_nginx):
        pool = await create_pool(client, "node-inv")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "invalid"},
        )
        assert resp.status_code == 422


class TestNodeConfigIsUsable:
    """The generated files have to work as they are on the node."""

    async def _generate(self, client, pool_type, address, **body):
        role = "active" if pool_type == "nanio" else "primary"
        pool = await create_pool(
            client, f"nc-{abs(hash((pool_type, address, str(body)))) % 10**8}", pool_type=pool_type
        )
        member = await create_member(client, pool["id"], address, role=role)
        resp = await client.post(f"/api/pools/{pool['id']}/members/{member['id']}/node-config", json=body)
        return resp

    def _file(self, data, name):
        return next(f["content"] for f in data["files"] if f["path"].endswith(name))

    async def test_nanio_listens_on_the_port_the_gateway_sends_to(self, client, mock_nginx):
        data = (
            await self._generate(
                client, "nanio", "10.0.0.1:9100", node_type="nanio-only", access_key="AK", secret_key="SK"
            )
        ).json()
        assert "port     = 9100" in self._file(data, "options.toml")
        assert data["warnings"] == []

    async def test_port_mismatch_is_reported(self, client, mock_nginx):
        data = (
            await self._generate(
                client,
                "nanio",
                "10.0.0.1:9100",
                node_type="nanio-only",
                nanio_port=9000,
                access_key="AK",
                secret_key="SK",
            )
        ).json()
        assert any("9100" in w and "9000" in w for w in data["warnings"])

    async def test_missing_credentials_are_reported_not_silently_omitted(self, client, mock_nginx):
        data = (await self._generate(client, "nanio", "10.0.0.1:9000", node_type="nanio-only")).json()
        assert any("refuses to start" in w for w in data["warnings"])
        assert "# [[users]]" in self._file(data, "options.toml")

    async def test_nginx_in_front_listens_on_member_port_and_nanio_on_loopback(self, client, mock_nginx):
        data = (
            await self._generate(
                client, "nanio", "10.0.0.1:80", node_type="nginx-nanio", access_key="AK", secret_key="SK"
            )
        ).json()
        proxy, options = self._file(data, "nanio-proxy.conf"), self._file(data, "options.toml")
        assert "listen 80;" in proxy
        assert "server 127.0.0.1:9000;" in proxy
        assert 'host     = "127.0.0.1"' in options and "port     = 9000" in options
        # Host must reach nanio as the client signed it
        assert "$http_host" in proxy and "$host;" not in proxy

    async def test_http_node_never_serves_nanio_internal_files(self, client, mock_nginx):
        data = (
            await self._generate(client, "http", "10.0.0.5:8080", node_type="nginx-only", data_dir="/srv/data")
        ).json()
        conf = self._file(data, "nanio-serve.conf")
        assert "listen 8080;" in conf and "root /srv/data;" in conf
        assert "location ~ /\\.nanio" in conf
        assert "limit_except GET HEAD { deny all; }" in conf

    async def test_unit_waits_for_the_data_mount_and_uses_the_given_binary(self, client, mock_nginx):
        data = (
            await self._generate(
                client,
                "nanio",
                "10.0.0.1:9000",
                node_type="nanio-only",
                data_dir="/mnt/shared",
                nanio_bin="/usr/local/bin/nanio",
                access_key="AK",
                secret_key="SK",
            )
        ).json()
        unit = self._file(data, "nanio.service")
        assert "RequiresMountsFor=/mnt/shared" in unit
        assert "ExecStart=/usr/local/bin/nanio serve --options /etc/nanio/options.toml" in unit
        assert "ReadWritePaths=/mnt/shared" in unit

    async def test_instructions_cover_install_permissions_and_check(self, client, mock_nginx):
        data = (
            await self._generate(
                client, "nanio", "10.0.0.1:9000", node_type="nanio-only", access_key="AK", secret_key="SK"
            )
        ).json()
        text = data["instructions"]
        for needle in (
            "pip install nanio",
            "useradd",
            "chmod 640 /etc/nanio/options.toml",
            "systemctl enable --now nanio",
            "curl -i http://10.0.0.1:9000/",
        ):
            assert needle in text, needle

    async def test_node_type_must_match_pool_type(self, client, mock_nginx):
        resp = await self._generate(client, "nanio", "10.0.0.1:9000", node_type="nginx-only")
        assert resp.status_code == 400
        resp = await self._generate(client, "http", "10.0.0.5:8080", node_type="nanio-only")
        assert resp.status_code == 400

    async def test_values_cannot_break_out_of_the_generated_files(self, client, mock_nginx):
        for field, value in (
            ("data_dir", '/data"\nhost = "x'),
            ("secret_key", 'a"b'),
            ("nanio_bin", "/bin/sh -c x"),
            ("nanio_host", "0.0.0.0\n"),
        ):
            resp = await self._generate(client, "nanio", "10.0.0.1:9000", node_type="nanio-only", **{field: value})
            assert resp.status_code == 422, field
