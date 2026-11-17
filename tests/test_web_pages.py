"""Server-rendered pages: what an operator sees with and without data, and the login flow."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
import pytest_asyncio
from nanio_orchestrator import __version__
from nanio_orchestrator.auth import COOKIE_NAME
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.web import routes as web_routes


async def sql(statement: str, params: tuple = ()) -> int:
    """Run one write statement against the test database; returns lastrowid."""
    async with get_db_ctx() as db:
        cur = await db.execute(statement, params)
        await db.commit()
        return cur.lastrowid


async def add_pool(name: str, pool_type: str = "nanio", **cols) -> int:
    names = ["name", "type", *cols]
    return await sql(
        f"INSERT INTO pools ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        (name, pool_type, *cols.values()),
    )


async def add_vhost(server_name: str, **cols) -> int:
    names = ["server_name", *cols]
    return await sql(
        f"INSERT INTO vhosts ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        (server_name, *cols.values()),
    )


async def add_migration(vhost_id: int, bucket: str, src: int, dst: int, phase: str, **cols) -> int:
    names = ["vhost_id", "bucket", "src_pool_id", "dst_pool_id", "phase", *cols]
    return await sql(
        f"INSERT INTO migrations ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
        (vhost_id, bucket, src, dst, phase, *cols.values()),
    )


def stat_of(html: str, card_title: str) -> str:
    """The number shown in a dashboard card."""
    m = re.search(rf"<h3>{re.escape(card_title)}</h3>\s*<div class=\"stat\">(\d+)</div>", html)
    assert m, f"no stat card titled {card_title!r}"
    return m.group(1)


def card_of(html: str, heading: str) -> str:
    """The HTML of the card whose <h3> is ``heading`` (up to the next card)."""
    start = html.index(f"<h3>{heading}</h3>")
    ends = [html.find(marker, start) for marker in ('<div class="card-header">', 'class="modal')]
    return html[start : min(e for e in [*ends, len(html)] if e != -1)]


@pytest_asyncio.fixture
async def web(client):
    """A browser session: no API key header, logged in through the form."""
    client.headers.pop("X-Orchestrator-Key")
    resp = await client.post("/login", data={"key": "test-key"})
    assert resp.status_code == 303
    assert client.cookies.get(COOKIE_NAME)
    return client


# ── Login / logout ────────────────────────────────────────────────────────────


class TestLoginFlow:
    async def test_login_page_shows_hostname_version_and_no_error(self, client, monkeypatch):
        monkeypatch.setattr(web_routes.socket, "gethostname", lambda: "edge-gw-01")
        client.headers.pop("X-Orchestrator-Key")
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert '<div class="login-host">edge-gw-01</div>' in resp.text
        assert f"nanio-orchestrator v{__version__}" in resp.text
        assert 'name="key"' in resp.text
        assert '<div class="login-error">' not in resp.text

    async def test_login_page_falls_back_when_the_hostname_is_unavailable(self, client, monkeypatch):
        def boom():
            raise OSError("no hostname")

        monkeypatch.setattr(web_routes.socket, "gethostname", boom)
        resp = await client.get("/login")
        assert '<div class="login-host">unknown</div>' in resp.text

    async def test_login_page_escapes_the_error_parameter(self, client):
        resp = await client.get("/login", params={"error": "<script>alert(1)</script>"})
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in resp.text
        assert "<script>alert(1)</script>" not in resp.text

    @pytest.mark.parametrize("key", ["wrong", "test-key ", "tést-kéy", ""])
    async def test_wrong_key_shows_the_error_and_sets_no_cookie(self, client, key):
        client.headers.pop("X-Orchestrator-Key")
        resp = await client.post("/login", data={"key": key}, follow_redirects=False)
        assert resp.status_code in (200, 422)  # an empty field never reaches the handler
        if resp.status_code == 200:
            assert '<div class="login-error">Invalid API key.</div>' in resp.text
        assert "set-cookie" not in resp.headers
        assert (await client.get("/", follow_redirects=False)).status_code == 302

    async def test_correct_key_redirects_home_with_a_session_that_opens_the_dashboard(self, client):
        client.headers.pop("X-Orchestrator-Key")
        resp = await client.post("/login", data={"key": "test-key"}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
        assert "Max-Age=28800" in cookie
        assert "Secure" not in cookie  # plain http request

        home = await client.get("/", follow_redirects=False)
        assert home.status_code == 200
        assert "<h1>Dashboard</h1>" in home.text

    async def test_session_cookie_is_secure_behind_a_tls_terminating_proxy(self, client):
        resp = await client.post(
            "/login", data={"key": "test-key"}, headers={"X-Forwarded-Proto": "HTTPS"}, follow_redirects=False
        )
        assert "Secure" in resp.headers["set-cookie"]

    async def test_session_cookie_is_secure_over_direct_https(self, client):
        resp = await client.post("https://test/login", data={"key": "test-key"}, follow_redirects=False)
        assert "Secure" in resp.headers["set-cookie"]

    async def test_logout_expires_the_cookie_and_the_pages_ask_for_login_again(self, web):
        resp = await web.post("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f'{COOKIE_NAME}="";') and "Max-Age=0" in cookie
        assert not web.cookies.get(COOKIE_NAME)

        after = await web.get("/web/pools", follow_redirects=False)
        assert after.status_code == 302
        assert after.headers["location"] == "/login"

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/web/pools",
            "/web/vhosts",
            "/web/config",
            "/web/audit",
            "/web/buckets",
            "/web/settings",
            "/web/migrations",
        ],
    )
    async def test_every_page_redirects_to_login_without_a_session_even_with_the_api_key(self, client, path):
        resp = await client.get(path, follow_redirects=False)  # header only, no cookie
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"


# ── Dashboard ─────────────────────────────────────────────────────────────────


class TestDashboard:
    async def test_empty_installation(self, web):
        html = (await web.get("/")).text
        assert stat_of(html, "Pools") == "0"
        assert stat_of(html, "Vhosts") == "0"
        assert stat_of(html, "Config Drift") == "0"
        assert stat_of(html, "Unrouted Buckets") == "0"
        assert stat_of(html, "Active Migrations") == "0"
        assert "IN SYNC" in html and "DRIFT DETECTED" not in html
        assert "ALL ROUTED" in html and "ACTION NEEDED" not in html
        assert '<span class="badge">—</span>' in html  # no reload yet
        assert "card-error" not in html
        assert f"v{__version__}" in html
        assert '<span class="badge dev">DEV</span>' in html
        assert 'action="/logout"' in html

    async def test_counts_pools_vhosts_and_unrouted_buckets(self, web):
        p1 = await add_pool("alpha")
        await add_pool("beta", "http")
        v = await add_vhost("s3.example.com", default_pool_id=p1)
        await add_vhost("cdn.example.com")
        await sql("INSERT INTO bucket_sync (vhost_id, bucket, status) VALUES (?, 'new-1', 'unrouted')", (v,))
        await sql("INSERT INTO bucket_sync (vhost_id, bucket, status) VALUES (?, 'new-2', 'unrouted')", (v,))
        await sql(
            "INSERT INTO bucket_sync (vhost_id, bucket, status, routed_pool_id) VALUES (?, 'old', 'routed', ?)", (v, p1)
        )
        html = (await web.get("/")).text
        assert stat_of(html, "Pools") == "2"
        assert stat_of(html, "Vhosts") == "2"
        assert stat_of(html, "Unrouted Buckets") == "2"
        assert "ACTION NEEDED" in html and "ALL ROUTED" not in html

    async def test_drift_counts_only_files_whose_known_hashes_differ(self, web):
        rows = [
            ("/n/pools/same.conf", "aaa", "aaa"),
            ("/n/pools/changed.conf", "bbb", "ccc"),
            ("/n/pools/never-checked.conf", None, "ddd"),
            ("/n/pools/untracked.conf", "eee", None),
        ]
        for path, disk, db_hash in rows:
            await sql("INSERT INTO config_files (path, sha256_disk, sha256_db) VALUES (?, ?, ?)", (path, disk, db_hash))
        html = (await web.get("/")).text
        assert stat_of(html, "Config Drift") == "1"
        assert "DRIFT DETECTED" in html
        assert html.count('<span class="badge warn">DRIFTED</span>') == 1
        for path, _, _ in rows:
            assert f"<code>{path}</code>" in html

    async def test_last_reload_failure_is_shown_with_its_output(self, web):
        await sql(
            "INSERT INTO audit_log (action, entity_type, nginx_reload_ok, nginx_reload_output, created_at)"
            " VALUES ('reload', 'config', 1, 'older ok', '2026-01-01 10:00:00')"
        )
        await sql(
            "INSERT INTO audit_log (action, entity_type, nginx_reload_ok, nginx_reload_output, created_at)"
            " VALUES ('reload', 'config', 0, 'nginx: [emerg] unknown directive', '2026-01-02 11:00:00')"
        )
        await sql("INSERT INTO audit_log (action, entity_type, entity_id) VALUES ('create', 'pool', 7)")
        html = (await web.get("/")).text
        assert '<span class="badge error">FAILED</span>' in html
        assert "card-error" in html
        assert "nginx: [emerg] unknown directive" in html
        assert "older ok" not in html
        assert "2026-01-02 11:00:00" in html
        # recent activity lists all three entries
        assert "<code>create</code>" in html and "pool #7" in html

    async def test_last_successful_reload_is_shown_as_ok(self, web):
        await sql(
            "INSERT INTO audit_log (action, entity_type, nginx_reload_ok, nginx_reload_output)"
            " VALUES ('reload', 'config', 1, 'signal process started')"
        )
        html = (await web.get("/")).text
        assert "card-ok" in html and "card-error" not in html
        assert "signal process started" in html

    async def test_recent_activity_is_limited_to_the_ten_newest_entries(self, web):
        for i in range(12):
            await sql("INSERT INTO audit_log (action, entity_type) VALUES (?, 'pool')", (f"act-{i:02d}",))
        html = (await web.get("/")).text
        assert "act-11" in html and "act-02" in html
        assert "act-01" not in html and "act-00" not in html
        assert html.index("act-11") < html.index("act-02")

    @pytest.mark.parametrize("phase", ["pending", "copying", "write_routing", "verifying", "switching"])
    async def test_migration_in_progress_is_listed_as_active(self, web, phase):
        src, dst = await add_pool("src"), await add_pool("dst")
        v = await add_vhost("s3.example.com", default_pool_id=src)
        mid = await add_migration(v, "photos", src, dst, phase, bytes_done=2048)
        html = (await web.get("/")).text
        assert stat_of(html, "Active Migrations") == "1"
        assert "<code>photos</code>" in html
        assert f'<span class="badge">{phase}</span>' in html
        assert f"cancelMigration({mid})" in html
        assert "2.0 kB" in html

    @pytest.mark.parametrize("phase", ["done", "error", "cancelled"])
    async def test_finished_migration_is_not_active(self, web, phase):
        src, dst = await add_pool("src"), await add_pool("dst")
        v = await add_vhost("s3.example.com", default_pool_id=src)
        await add_migration(v, "photos", src, dst, phase)
        html = (await web.get("/")).text
        assert stat_of(html, "Active Migrations") == "0"
        assert "<code>photos</code>" not in html


# ── Pools ─────────────────────────────────────────────────────────────────────


class TestPoolsPage:
    async def test_no_pools_shows_the_empty_state(self, web):
        html = (await web.get("/web/pools")).text
        assert "No pools configured yet." in html
        assert "editPool(" not in html

    async def test_pool_is_shown_with_its_settings_and_members(self, web):
        pid = await add_pool("alpha", description="Primary <cluster>", lb_method="ip_hash", keepalive=64)
        await sql(
            "INSERT INTO pool_members (pool_id, address, role, weight) VALUES (?, '10.0.0.1:9000', 'primary', 5)",
            (pid,),
        )
        await sql("INSERT INTO pool_members (pool_id, address, enabled) VALUES (?, '10.0.0.2:9000', 0)", (pid,))
        html = (await web.get("/web/pools")).text
        card = card_of(html, "alpha")
        assert '<span class="badge type-nanio">nanio</span>' in card
        assert '<span class="badge">ip_hash</span>' in card
        assert "keepalive=64" in card
        assert "Primary &lt;cluster&gt;" in card  # escaped
        assert "10.0.0.1:9000" in card and "10.0.0.2:9000" in card
        assert card.index("10.0.0.1:9000") < card.index("10.0.0.2:9000")
        assert card.count('<span class="badge ok">enabled</span>') == 1
        assert card.count('<span class="badge warn">disabled</span>') == 1
        assert "No members yet" not in card
        assert f"editPool({pid})" in card
        assert "No pools configured yet." not in html

    async def test_pool_without_members_says_so(self, web):
        await add_pool("empty-pool")
        html = (await web.get("/web/pools")).text
        assert "No members yet" in card_of(html, "empty-pool")

    async def test_http_pool_names_the_nanio_pool_backing_it(self, web):
        nanio = await add_pool("objects")
        await add_pool("other-nanio")
        await add_pool("static", "http", source_nanio_pool_id=nanio)
        await add_pool("plain-http", "http")
        html = (await web.get("/web/pools")).text
        static = card_of(html, "static")
        assert '<span class="badge type-http">http</span>' in static
        assert "backed by:" in static and "<strong>objects</strong>" in static
        assert "other-nanio" not in static.split("</p>")[0]
        assert "backed by:" not in card_of(html, "plain-http")
        assert "backed by:" not in card_of(html, "objects")
        # pools are listed alphabetically
        names = re.findall(r'<div class="card-header">\s*<h3>([^<]+)</h3>', html)
        assert names == ["objects", "other-nanio", "plain-http", "static"]


# ── Vhosts ────────────────────────────────────────────────────────────────────


class TestVhostsPage:
    async def test_no_vhosts_shows_the_empty_state(self, web):
        html = (await web.get("/web/vhosts")).text
        assert "No virtual hosts configured." in html
        assert "previewVhost(" not in html

    async def test_vhost_shows_default_pool_ssl_and_routes_longest_prefix_first(self, web):
        alpha, beta = await add_pool("alpha"), await add_pool("beta")
        vid = await add_vhost(
            "s3.example.com",
            listen_port=8443,
            ssl=1,
            ssl_cert_path="/etc/ssl/s3.pem",
            ssl_key_path="/etc/ssl/s3.key",
            default_pool_id=alpha,
        )
        await sql("INSERT INTO routes (vhost_id, path_prefix, pool_id) VALUES (?, '/', ?)", (vid, alpha))
        await sql(
            "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, '/logs/', ?, 0)", (vid, beta)
        )
        await sql("INSERT INTO routes (vhost_id, path_prefix, pool_id) VALUES (?, '/photos-archive/', ?)", (vid, beta))
        html = (await web.get("/web/vhosts")).text
        card = card_of(html, "s3.example.com")
        assert '<span class="badge">:8443</span>' in card
        assert '<span class="badge ok">SSL</span>' in card
        assert '<span class="badge type-nanio">nanio</span>' in card
        assert "Cert: <code>/etc/ssl/s3.pem</code>" in card
        assert "Default pool: <strong>alpha</strong>" in card
        assert card.index("/photos-archive/") < card.index("/logs/") < card.index(">auto<")
        assert card.count('<span class="badge warn">disabled</span>') == 1  # the /logs/ route
        assert "Extra blocks:" not in card and "IP rules:" not in card
        assert "No routes configured" not in card
        assert f"showExtraBlocks({vid}, [], null, null)" in card
        # pools are offered in the forms
        assert ">alpha" in html and ">beta" in html

    async def test_vhost_without_pool_or_routes(self, web):
        await add_vhost("bare.example.com", listen_port=80, ssl=0, enabled=0)
        card = card_of((await web.get("/web/vhosts")).text, "bare.example.com")
        assert '<span class="badge ok">SSL</span>' not in card
        assert '<span class="badge warn">disabled</span>' in card
        assert "Default pool:" not in card and "Cert:" not in card
        assert "No routes configured" in card
        assert "badge type-" not in card

    async def test_extra_blocks_and_ip_rules_are_summarised(self, web):
        pool = await add_pool("static", "http")
        await add_vhost(
            "cdn.example.com",
            default_pool_id=pool,
            extra_blocks_json='[{"zone": "top", "content": "gzip on;"}, {"zone": "end", "content": "# x"}]',
            ip_rule_mode="allow",
            ip_rule_ips_json='["10.0.0.0/8"]',
        )
        await add_vhost("deny.example.com", ip_rule_mode="deny", ip_rule_ips_json='["192.0.2.1", "198.51.100.0/24"]')
        html = (await web.get("/web/vhosts")).text
        cdn = card_of(html, "cdn.example.com")
        assert '<span class="badge type-http">http</span>' in cdn
        assert 'Extra blocks: <span class="badge">top</span> <span class="badge">end</span>' in cdn
        assert '<span class="badge warn">allowlist</span>' in cdn and "(1 entry)" in cdn
        deny = card_of(html, "deny.example.com")
        assert '<span class="badge warn">denylist</span>' in deny and "(2 entries)" in deny
        assert "Extra blocks:" not in deny

    async def test_unreadable_extra_blocks_do_not_break_the_page(self, web):
        vid = await add_vhost("broken.example.com", extra_blocks_json="{not json")
        resp = await web.get("/web/vhosts")
        assert resp.status_code == 200
        card = card_of(resp.text, "broken.example.com")
        assert "Extra blocks:" not in card
        assert f"showExtraBlocks({vid}, [], null, null)" in card


# ── Config ────────────────────────────────────────────────────────────────────


class TestConfigPage:
    async def test_no_tracked_files(self, web):
        html = (await web.get("/web/config")).text
        assert "No config files tracked yet" in html
        assert "DRIFTED" not in html

    async def test_file_state_reflects_the_disk_right_now(self, web, tmp_path):
        """The stored sha256_disk is ignored: the page hashes the file itself."""
        in_sync = tmp_path / "in-sync.conf"
        in_sync.write_text("upstream a {}\n")
        edited = tmp_path / "edited.conf"
        edited.write_text("upstream b {} # hand edit\n")
        gone = tmp_path / "gone.conf"
        sync_hash = hashlib.sha256(b"upstream a {}\n").hexdigest()
        edit_hash = hashlib.sha256(b"upstream b {} # hand edit\n").hexdigest()
        await sql(
            "INSERT INTO config_files (path, sha256_disk, sha256_db, last_synced_at) VALUES (?, 'stale', ?, '2026-03-04 05:06:07')",
            (str(in_sync), sync_hash),
        )
        await sql(
            "INSERT INTO config_files (path, sha256_disk, sha256_db) VALUES (?, ?, ?)",
            (str(edited), "f" * 64, "f" * 64),
        )
        await sql(
            "INSERT INTO config_files (path, sha256_disk, sha256_db) VALUES (?, 'x', 'abcdef0123456789')", (str(gone),)
        )

        html = (await web.get("/web/config")).text
        rows = {
            m.group(1): m.group(0) for m in re.finditer(r"<tr>\s*<td><code>([^<]+)</code></td>.*?</tr>", html, re.S)
        }
        assert set(rows) == {str(in_sync), str(edited), str(gone)}

        assert '<span class="badge ok">OK</span>' in rows[str(in_sync)]
        assert f'<code class="hash">{sync_hash[:12]}</code>' in rows[str(in_sync)]
        assert "2026-03-04 05:06:07" in rows[str(in_sync)]
        assert "rewriteFile(" not in rows[str(in_sync)]

        assert '<span class="badge warn">DRIFTED</span>' in rows[str(edited)]
        assert f'<code class="hash">{edit_hash[:12]}</code>' in rows[str(edited)]
        assert f"rewriteFile('{edited}')" in rows[str(edited)] and f"absorbFile('{edited}')" in rows[str(edited)]

        # a missing file has no disk hash and is not offered for rewrite here
        assert '<code class="hash">—</code>' in rows[str(gone)]
        assert '<code class="hash">abcdef012345</code>' in rows[str(gone)]
        assert "DRIFTED" not in rows[str(gone)]

    async def test_unreadable_file_is_shown_without_a_disk_hash(self, web, tmp_path, monkeypatch):
        locked = tmp_path / "locked.conf"
        locked.write_text("secret")
        await sql("INSERT INTO config_files (path, sha256_db) VALUES (?, 'abc')", (str(locked),))
        real_read_text = Path.read_text

        def read_text(self, *args, **kwargs):
            if self == locked:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        resp = await web.get("/web/config")
        assert resp.status_code == 200
        assert f"<code>{locked}</code>" in resp.text
        assert '<code class="hash">—</code>' in resp.text
        assert "DRIFTED" not in resp.text

    async def test_file_never_written_by_the_orchestrator_is_not_drift(self, web, tmp_path):
        found = tmp_path / "found.conf"
        found.write_text("x")
        await sql("INSERT INTO config_files (path) VALUES (?)", (str(found),))
        html = (await web.get("/web/config")).text
        assert "DRIFTED" not in html
        assert f'<code class="hash">{hashlib.sha256(b"x").hexdigest()[:12]}</code>' in html


# ── Audit ─────────────────────────────────────────────────────────────────────


class TestAuditPage:
    async def test_no_entries(self, web):
        html = (await web.get("/web/audit")).text
        assert "No audit entries yet" in html
        assert "showAuditDetail(" not in html

    async def test_entries_newest_first_with_reload_outcome(self, web):
        a = await sql("INSERT INTO audit_log (action, entity_type, entity_id) VALUES ('create', 'pool', 3)")
        b = await sql("INSERT INTO audit_log (action, entity_type, nginx_reload_ok) VALUES ('reload', 'config', 1)")
        c = await sql(
            "INSERT INTO audit_log (actor, action, entity_type, nginx_reload_ok) VALUES ('engine', 'migrate', 'bucket', 0)"
        )
        html = (await web.get("/web/audit")).text
        assert "No audit entries yet" not in html
        assert (
            html.index(f"showAuditDetail({c})")
            < html.index(f"showAuditDetail({b})")
            < html.index(f"showAuditDetail({a})")
        )
        assert "pool #3" in html
        assert "<td>engine</td>" in html and "<td>api</td>" in html
        assert html.count('<span class="badge ok">OK</span>') == 1
        assert html.count('<span class="badge error">FAIL</span>') == 1

    async def test_only_the_hundred_newest_entries_are_shown(self, web):
        async with get_db_ctx() as db:
            for i in range(103):
                await db.execute("INSERT INTO audit_log (action, entity_type) VALUES (?, 'pool')", (f"act-{i:03d}",))
            await db.commit()
        html = (await web.get("/web/audit")).text
        assert html.count("showAuditDetail(") == 100
        assert "<code>act-102</code>" in html and "<code>act-003</code>" in html
        assert "<code>act-002</code>" not in html


# ── Buckets ───────────────────────────────────────────────────────────────────


class TestBucketsPage:
    async def test_nothing_configured(self, web):
        html = (await web.get("/web/buckets")).text
        assert "No vhosts with a nanio default pool configured." in html
        assert "No nanio pools configured." in html
        assert "HTTP vhosts backed by nanio pools" not in html

    async def test_buckets_of_a_nanio_vhost_with_their_state(self, web):
        alpha, beta = await add_pool("alpha"), await add_pool("beta")
        v = await add_vhost("s3.example.com", default_pool_id=alpha)
        rows = [
            ("fresh", "unrouted", None, None),
            ("moved", "routed", beta, None),
            ("moving", "migrating", beta, None),
            ("skipped", "ignored", None, None),
            ("vanished", "routed", beta, "2026-05-01 00:00:00"),
            ("removed-but-routed", "deleted", beta, None),
            ("removed-and-forgotten", "deleted", None, None),
        ]
        for bucket, status, pool, missing in rows:
            await sql(
                "INSERT INTO bucket_sync (vhost_id, bucket, status, routed_pool_id, missing_since) VALUES (?, ?, ?, ?, ?)",
                (v, bucket, status, pool, missing),
            )
        html = (await web.get("/web/buckets")).text
        assert "No vhosts with a nanio default pool configured." not in html
        section = html[html.index(f'id="vhost-{v}"') : html.index("<!-- /pane-vhost -->")]
        listed = re.findall(
            r'<tr class="bucket-row" data-status="(\w+)" data-missing="(1?)">\s*<td><code>([^<]+)</code>', section
        )
        # ordered by status then name; a deleted bucket nobody routes any more is left out
        assert listed == [
            ("deleted", "", "removed-but-routed"),
            ("ignored", "", "skipped"),
            ("migrating", "", "moving"),
            ("routed", "", "moved"),
            ("routed", "1", "vanished"),
            ("unrouted", "", "fresh"),
        ]
        assert "removed-and-forgotten" not in html
        assert "missing on pool" in section and "since 2026-05-01 00:00:00" in section
        assert section.count("<td>beta</td>") == 4
        assert "No buckets synced yet" not in section
        # both nanio pools appear in the per-pool pane
        assert f'id="scan-btn-{alpha}"' in html and f'id="scan-btn-{beta}"' in html
        assert "No nanio pools configured." not in html

    async def test_vhost_without_synced_buckets_invites_a_sync(self, web):
        alpha = await add_pool("alpha")
        v = await add_vhost("s3.example.com", default_pool_id=alpha)
        html = (await web.get("/web/buckets")).text
        assert f"syncVhost({v}, this)" in html
        assert "No buckets synced yet — click Sync" in html

    async def test_http_vhosts_are_listed_only_when_backed_by_a_nanio_pool(self, web):
        objects = await add_pool("objects")
        linked = await add_pool("static-linked", "http", source_nanio_pool_id=objects)
        plain = await add_pool("static-plain", "http")
        hv = await add_vhost("cdn.example.com", default_pool_id=linked)
        pv = await add_vhost("www.example.com", default_pool_id=plain)
        await add_vhost("nopool.example.com")
        html = (await web.get("/web/buckets")).text
        assert "HTTP vhosts backed by nanio pools" in html
        assert f'id="http-vhost-{hv}"' in html
        assert "backed by objects" in html
        assert f'id="http-vhost-{pv}"' not in html
        assert "www.example.com" not in html and "nopool.example.com" not in html
        # http vhosts have no bucket table of their own, and http pools cannot be scanned
        assert f'id="vhost-{hv}"' not in html
        assert "No vhosts with a nanio default pool configured." in html
        assert f'id="scan-btn-{objects}"' in html
        assert f'id="scan-btn-{linked}"' not in html and f'id="scan-btn-{plain}"' not in html


# ── Settings ──────────────────────────────────────────────────────────────────


class TestSettingsPage:
    async def test_settings_page_renders_the_shell_without_any_secret(self, web):
        resp = await web.get("/web/settings")
        assert resp.status_code == 200
        assert "<h1>Settings</h1>" in resp.text
        assert 'id="settings-body"' in resp.text
        assert "test-key" not in resp.text


# ── Migrations ────────────────────────────────────────────────────────────────


class TestMigrationsPage:
    async def test_no_migrations(self, web):
        html = (await web.get("/web/migrations")).text
        assert "No migrations yet" in html
        assert "showMigrationLog(" not in html

    async def test_migrations_newest_first_with_progress_and_actions(self, web):
        src, dst = await add_pool("old-cluster"), await add_pool("new-cluster")
        await add_pool("static", "http")
        v = await add_vhost("s3.example.com", default_pool_id=src)
        done = await add_migration(
            v, "photos", src, dst, "done", objects_total=10, objects_done=10,
            started_at="2026-02-01 10:00:00", finished_at="2026-02-01 11:00:00",
        )  # fmt: skip
        failed = await add_migration(v, "logs", src, dst, "error", folder="2025/")
        cancelled = await add_migration(v, "tmp", src, dst, "cancelled")
        running = await add_migration(v, "videos", src, dst, "copying", objects_total=100, objects_done=40)

        html = (await web.get("/web/migrations")).text
        assert "No migrations yet" not in html
        order = [html.index(f"<td>#{m}</td>") for m in (running, cancelled, failed, done)]
        assert order == sorted(order)
        assert html.count("old-cluster → new-cluster") == 4
        assert '<span class="badge ok">done</span>' in html
        assert '<span class="badge error">error</span>' in html
        assert '<span class="badge warn">cancelled</span>' in html
        assert '<span class="badge">copying</span>' in html
        assert "<code>logs/2025/</code>" in html and "<code>photos</code>" in html
        assert "10/10 objects" in html and "40/100 objects" in html
        assert "2026-02-01 10:00:00" in html and "2026-02-01 11:00:00" in html
        # only the running one can be cancelled; every one has a log
        assert re.findall(r"cancelMigration\((\d+)\)\"", html) == [str(running)]
        assert html.count("showMigrationLog(") >= 4
        # only nanio pools are offered as source/destination
        options = re.findall(r"<option value=\"\d+\">\s*([^<\s]+)", html)
        assert "old-cluster" in options and "new-cluster" in options and "static" not in options
