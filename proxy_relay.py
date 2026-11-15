# -*- coding: utf-8 -*-
"""本地 CONNECT 代理中继 + 代理模板解析（TPS 引擎和两个地址抓取器共用）。

单独一个文件是因为这里只用标准库：地址抓取器用的是 nodriver，打包时不想为了
一个中继把 playwright 整套拖进 EXE。engine_core 从这里导入，行为跟以前一样。
"""
import base64
import re
import socket
import threading
import time
import urllib.parse

class LocalProxy:
    """本地 CONNECT 中继：Chromium 只连 127.0.0.1，由这里补代理账号认证。

    Playwright 自带的 proxy 用户名/密码在 Chromium 里会直接 ERR_TUNNEL_CONNECTION_FAILED，
    所以把认证挪到本地中继；同一浏览器实例的所有连接共用同一个 session（同一个出口 IP）。
    """

    def __init__(self, upstream_host, upstream_port, username, password, no_auth=False, port=0):
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {credentials}".encode()
        self.no_auth = bool(no_auth) or not (username or password)
        self._upstream = (upstream_host, int(upstream_port))
        self._stopped = False
        self.auth_failed = False
        self.conn_error = ""
        self.bytes_transferred = 0
        self._live = set()
        self._live_lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", int(port or 0)))
        self._sock.listen(128)
        self._sock.settimeout(1.0)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def rebind(self, upstream_host, upstream_port, username, password, no_auth=False):
        """只换出口（新 sid / 换 IP），本地端口不动：浏览器接着用同一个 127.0.0.1 就行，
        不用为了换 IP 重开一个 chromium。顺手掐掉在用的隧道，逼浏览器重新握手拿新出口。"""
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {credentials}".encode()
        self.no_auth = bool(no_auth) or not (username or password)
        self._upstream = (upstream_host, int(upstream_port))
        self.auth_failed = False
        self.conn_error = ""
        self.bytes_transferred = 0
        with self._live_lock:
            live = list(self._live)
            self._live.clear()
        for client, upstream in live:
            for sock in (client, upstream):
                try:
                    sock.close()
                except Exception:
                    pass

    def close_tunnels(self):
        with self._live_lock:
            live = list(self._live)
            self._live.clear()
        for client, upstream in live:
            for sock in (client, upstream):
                try:
                    sock.close()
                except Exception:
                    pass

    @property
    def server(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self._stopped = True
        self.close_tunnels()
        try:
            self._sock.close()
        except Exception:
            pass

    def _conn_err_text(self, exc):
        host, port = self._upstream
        if isinstance(exc, socket.gaierror):
            return f"DNS解析失败({host})"
        if isinstance(exc, ConnectionRefusedError):
            return f"连接被拒({host}:{port})"
        if isinstance(exc, ConnectionResetError):
            return f"连接被重置({host}:{port}，疑似并发线程超限)"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return f"连接超时({host}:{port})"
        return f"{type(exc).__name__}: {exc}"[:80]

    def _serve(self):
        while not self._stopped:
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    @staticmethod
    def _read_head(sock, limit=65536):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if len(data) > limit:
                break
        idx = data.find(b"\r\n\r\n")
        if idx < 0:
            return data, b""
        return data[:idx + 4], data[idx + 4:]

    @staticmethod
    def _drop_proxy_headers(head):
        lines = head.split(b"\r\n")
        kept = [lines[0]]
        for line in lines[1:]:
            low = line.lower()
            if low.startswith((b"proxy-authorization:", b"proxy-connection:")):
                continue
            if line:
                kept.append(line)
        return b"\r\n".join(kept)

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(45)
            head, pending = self._read_head(client)
            if not head:
                return
            lines = head.split(b"\r\n")
            parts = lines[0].decode("latin-1").split()
            if len(parts) < 3:
                return
            if parts[0].upper() != "CONNECT":
                return

            try:
                upstream = socket.create_connection(self._upstream, timeout=30)
            except Exception as exc:
                self.conn_error = self._conn_err_text(exc)
                return
            upstream.settimeout(45)
            request = self._drop_proxy_headers(head)
            if not self.no_auth:
                request += b"\r\nProxy-Authorization: " + self._auth
            request += b"\r\n\r\n"
            upstream.sendall(request)

            response, leftover = self._read_head(upstream)
            client.sendall(response)
            status_line = response.split(b"\r\n")[0]
            if b" 200" not in status_line:
                if b" 407" in status_line:
                    self.auth_failed = True
                    self.conn_error = "代理407认证失败(账号/密码错或余额不足)"
                else:
                    self.conn_error = f"代理拒绝: {status_line.decode('latin-1', 'replace')[:60]}"
                return

            client.settimeout(None)
            upstream.settimeout(None)
            self._tunnel(client, upstream, pending, leftover)
        except Exception as exc:
            if not self.conn_error:
                self.conn_error = self._conn_err_text(exc)
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _tunnel(self, client, upstream, client_first=b"", upstream_first=b""):
        def pipe(src, dst, first):
            try:
                if first:
                    dst.sendall(first)
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
                    self.bytes_transferred += len(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        to_upstream = threading.Thread(target=pipe, args=(client, upstream, client_first), daemon=True)
        to_client = threading.Thread(target=pipe, args=(upstream, client, upstream_first), daemon=True)
        pair = (client, upstream)
        with self._live_lock:
            self._live.add(pair)
        try:
            to_upstream.start()
            to_client.start()
            to_upstream.join()
            to_client.join()
        finally:
            with self._live_lock:
                self._live.discard(pair)


def parse_proxy_lines(text, default_pass=""):
    """把「代理模板」这一栏拆成一条条出口：一行一条（分号隔开也算），几种写法可以混着粘。

    IP:端口:账号:密码        静态 IP 的给法，例 64.205.177.79:3227:bKdZ7:fwCfS
    http://账号:密码@IP:端口  完整链接
    IP:端口                  免认证（IP 白名单型）
    账号串 + 单独「密码」栏    LinkUp 那种老写法，账号里带 {session} 就能自己换 IP

    密码里带冒号也算：按前三个冒号切，剩下全归密码。
    """
    out = []
    for raw in str(text or "").replace(";", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line:
            p = urllib.parse.urlsplit(line if "://" in line else "http://" + line)
            host, port = p.hostname or "", p.port or 80
            user = urllib.parse.unquote(p.username or "")
            pw = urllib.parse.unquote(p.password or "") or str(default_pass or "")
        else:
            seg = line.split(":", 3)
            if len(seg) == 4 and seg[1].isdigit():
                host, port, user, pw = seg[0], int(seg[1]), seg[2], seg[3]
            elif len(seg) == 2 and seg[1].isdigit():
                host, port, user, pw = seg[0], int(seg[1]), "", str(default_pass or "")
            elif len(seg) == 1:
                base = urllib.parse.urlsplit(PROXY_SERVER)
                host, port, user, pw = base.hostname, base.port or 80, seg[0], str(default_pass or "")
            else:
                continue
        if host:
            out.append({"host": host, "port": int(port or 80), "user": user, "pass": pw})
    return out
