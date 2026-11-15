# -*- coding: utf-8 -*-
"""一批 IP 的轮换池：轮着查、谁被限流谁歇着，别死守一条。

设计要点（对应用户的判断「一个 IP 轮番查询，频繁的几率不大」）：
- 每条 IP 有最短间隔 min_interval：轮完一圈再回到它，至少隔这么久，
  单条 IP 的请求频率 = 并发数 / min_interval，而不是总吞吐。
- 被限流(rate limited / 429)只让那条 IP 进冷却，其它 IP 照跑；冷却按指数退避。
- 硬拒绝(403 / access denied)比限流更重：冷却更长，连续 N 次直接停用它，
  不再拿它浪费请求。
- 全部都在冷却时，pick() 返回 None 并给出还要等多久，上层等最短的那个，
  而不是抓一条正在冷却的 IP 硬撞。

只做数据结构和计数，不碰网络，好测也好复用（homes / realtor / 之后的 TPS 都能用）。
"""
import re


def parse_ips(text, default_port=None):
    """吃这几种写法，一条一行，# 开头算注释：
      1.2.3.4                      只有 IP，用 default_port
      1.2.3.4:8080                 IP:端口（免认证 / 白名单型）
      1.2.3.4:8080:user:pass       IP:端口:账号:密码（家用宽带路由型常见）
      http://user:pass@host:port   完整链接
    """
    out = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line or "://" in line:
            m = re.match(r"^(?:(\w+)://)?(?:([^:/@]+)(?::([^@]*))?@)?([^:/@]+)(?::(\d+))?", line)
            scheme, user, pw, host, port = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
            entry = {"host": host, "port": int(port) if port else int(default_port or 80),
                     "user": user or "", "pass": pw or "", "scheme": scheme or "http"}
        else:
            parts = line.split(":")
            if len(parts) == 1:
                entry = {"host": parts[0], "port": int(default_port or 80),
                         "user": "", "pass": "", "scheme": "http"}
            elif len(parts) == 2:
                entry = {"host": parts[0], "port": int(parts[1]),
                         "user": "", "pass": "", "scheme": "http"}
            elif len(parts) == 4:
                entry = {"host": parts[0], "port": int(parts[1]),
                         "user": parts[2], "pass": parts[3], "scheme": "http"}
            else:
                continue
        if not entry["host"]:
            continue
        entry.update(id=f'{entry["host"]}:{entry["port"]}', req=0, ok=0, rate=0, hard=0,
                     dead=0, streak=0, hard_streak=0, until=0.0, last=-1e9, disabled=False)
        out.append(entry)
    return out


class IpPool:
    def __init__(self, text, default_port=None, min_interval=8.0,
                 rate_base=90.0, rate_cap=900.0, hard_base=300.0, hard_cap=3600.0,
                 hard_disable=3, clock=None):
        self.entries = parse_ips(text, default_port)
        self.min_interval = float(min_interval)
        self.rate_base, self.rate_cap = float(rate_base), float(rate_cap)
        self.hard_base, self.hard_cap = float(hard_base), float(hard_cap)
        self.hard_disable = int(hard_disable)
        self.clock = clock or __import__("time").monotonic
        self.cursor = 0
        self.switches = 0

    def __len__(self):
        return sum(1 for e in self.entries if not e["disabled"])

    def ready(self):
        now = self.clock()
        return [e for e in self.entries
                if not e["disabled"] and e["until"] <= now and e["last"] + self.min_interval <= now]

    def pick(self):
        """返回下一条可用的 IP；全都在冷却/间隔里就返回 None。"""
        n = len(self.entries)
        if not n or not self.ready():
            return None
        for k in range(n):
            i = (self.cursor + k) % n
            e = self.entries[i]
            if e["disabled"]:
                continue
            now = self.clock()
            if e["until"] <= now and e["last"] + self.min_interval <= now:
                self.cursor = (i + 1) % n
                e["req"] += 1
                e["last"] = now
                if k:
                    self.switches += 1
                return e
        return None

    def wait(self):
        """还要等多久才有一条 IP 可用（秒）。0 = 现在就有。"""
        live = [e for e in self.entries if not e["disabled"]]
        if not live:
            return 1e9
        if self.ready():
            return 0.0
        now = self.clock()
        need = [max(e["until"], e["last"] + self.min_interval) - now for e in live]
        return max(0.0, min(need))

    def mark(self, entry, kind):
        if entry is None:
            return 0.0
        now = self.clock()
        if kind == "ok":
            entry["ok"] += 1
            entry["streak"] = 0
            entry["hard_streak"] = 0
            entry["until"] = min(entry["until"], now)
            return 0.0
        if kind == "rate":
            entry["rate"] += 1
            entry["streak"] += 1
            cool = min(self.rate_cap, self.rate_base * (2 ** min(entry["streak"] - 1, 4)))
            entry["until"] = max(entry["until"], now + cool)
            return cool
        if kind == "hard":
            entry["hard"] += 1
            entry["hard_streak"] += 1
            cool = min(self.hard_cap, self.hard_base * (2 ** min(entry["hard_streak"] - 1, 5)))
            entry["until"] = max(entry["until"], now + cool)
            if entry["hard_streak"] >= self.hard_disable:
                entry["disabled"] = True
                entry["dead"] += 1
            return cool
        return 0.0

    def rows(self):
        head = ["IP:端口", "有无认证", "请求", "成功", "限流", "硬拒", "停用", "冷却剩余s"]
        out = [head]
        now = self.clock()
        for e in self.entries:
            out.append([e["id"], "有" if (e["user"] or e["pass"]) else "无",
                        e["req"], e["ok"], e["rate"], e["hard"],
                        "是" if e["disabled"] else "",
                        f"{max(0.0, e['until'] - now):.0f}"])
        return out

    def summary(self):
        live = len(self)
        req = sum(e["req"] for e in self.entries)
        ok = sum(e["ok"] for e in self.entries)
        rate = sum(e["rate"] for e in self.entries)
        hard = sum(e["hard"] for e in self.entries)
        dead = sum(1 for e in self.entries if e["disabled"])
        return (f"IP 池 {len(self.entries)} 条(可用 {live})：请求{req} 成功{ok} "
                f"限流{rate} 硬拒{hard} 停用{dead} 换线{self.switches}次")
