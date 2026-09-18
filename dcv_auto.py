#!/usr/bin/env python3
"""Cloudflare 边缘证书 TXT DCV 自动化 + GitHub Issue 提醒。

背景：证书涵盖通配符（如 *.mytemos.com）时，CA/B 论坛新规不允许使用 HTTP DCV，
只能走 TXT 验证。Cloudflare 会在续订窗口（到期前 30 天）生成 TXT token 并邮件通知。

本脚本每次运行：
  1. 读取 zone 下所有证书包，找出待验证（非 active）的 TXT 记录
  2. 若有待验证记录，先在当前仓库创建一条 issue 提醒（创建失败则本次运行失败，不继续续订）
  3. 幂等地把 token 写入 Cloudflare DNS（同值已存在则跳过）
  4. 调 PATCH ssl/verification 触发即时重新验证
  5. 待验证记录消失后，自动关闭此前创建的提醒 issue
  6. 可选：清理过期的 _acme-challenge TXT 记录

环境变量：
  CLOUDFLARE_API_TOKEN  Cloudflare API token（必须）
  CF_ZONE_NAME          域名，默认 mytemos.com
  DRY_RUN               true 时只打印，不做任何修改
  NO_TRIGGER            true 时只写 TXT，不调 PATCH
  CLEANUP               true 时清理过期的 _acme-challenge 记录
  CLEANUP_AGE_DAYS      清理阈值天数，默认 60
  TXT_TTL               TXT 记录 TTL，默认 120
  NOTIFY_ISSUE          true 时创建 GitHub issue 提醒
  CLOSE_ISSUES          true 时在验证完成后自动关闭提醒 issue，默认 true
  NOTIFY_MENTION        在 issue 正文里 @ 的用户名（可选）
  GITHUB_TOKEN          创建 issue 用的 token（Actions 里用 github.token 即可）
  GITHUB_REPOSITORY     形如 owner/repo
  GITHUB_API_URL        GitHub API 地址，默认 https://api.github.com
"""

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

CF_API = "https://api.cloudflare.com/client/v4"
GH_API = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
GH_REPO = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
GH_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
NOTIFY_MENTION = (os.environ.get("NOTIFY_MENTION") or "").strip()

# Windows 控制台默认使用本地编码，强制 UTF-8 让中文日志正常显示
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def env_flag(name, default=False):
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


TOKEN = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
ZONE_NAME = (os.environ.get("CF_ZONE_NAME") or "mytemos.com").strip()
DRY_RUN = env_flag("DRY_RUN")
NO_TRIGGER = env_flag("NO_TRIGGER")
CLEANUP = env_flag("CLEANUP")
NOTIFY_ISSUE = env_flag("NOTIFY_ISSUE")
CLOSE_ISSUES = env_flag("CLOSE_ISSUES", True)
CLEANUP_AGE_DAYS = int((os.environ.get("CLEANUP_AGE_DAYS") or "60").strip())
TXT_TTL = int((os.environ.get("TXT_TTL") or "120").strip())

MARKER_RE = re.compile(r"<!-- dcv-auto:\S+ -->")

changed = 0


def log(msg):
    print("[dcv-auto] " + msg, flush=True)


def fail(msg, code):
    print("[dcv-auto] [ERROR] " + msg, file=sys.stderr, flush=True)
    sys.exit(code)


def request_json(url, token, method, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP {0} {1} -> {2} {3}".format(method, path, exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("网络错误 {0} {1} -> {2}".format(method, path, exc.reason))


def cf(method, path, body=None):
    return request_json(CF_API, TOKEN, method, path, body)


def gh(method, path, body=None):
    return request_json(
        GH_API,
        GH_TOKEN,
        method,
        path,
        body,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dcv-auto",
        },
    )


def unquote(value):
    if value is None:
        return ""
    return value.strip().strip('"')


def collect_pending(zid):
    packs = cf("GET", "/zones/{0}/ssl/certificate_packs?per_page=50".format(zid)).get("result") or []
    todo = []
    for pack in packs:
        for record in (pack.get("validation_records") or []):
            if not record:
                continue
            if record.get("txt_name") and record.get("txt_value") and record.get("status") != "active":
                todo.append(
                    {
                        "pack_id": pack.get("id"),
                        "method": pack.get("validation_method") or "txt",
                        "name": record.get("txt_name"),
                        "value": record.get("txt_value"),
                        "status": record.get("status"),
                    }
                )
    return todo


def marker(value):
    return "<!-- dcv-auto:{0} -->".format(value)


def open_alert_issues():
    issues = []
    for page in range(1, 6):
        batch = gh("GET", "/repos/{0}/issues?state=open&per_page=100&page={1}".format(GH_REPO, page))
        if not batch:
            break
        issues.extend(batch)
        if len(batch) < 100:
            break
    return issues


def notify_pending(todo):
    """创建提醒 issue。创建失败直接结束本次运行，避免出现没提醒却默默续订的情况。"""
    if not NOTIFY_ISSUE:
        log("NOTIFY_ISSUE 未启用，跳过 issue 提醒。")
        return
    if DRY_RUN:
        for item in todo:
            log("[DryRun] 将创建 issue 提醒: {0}".format(item["name"]))
        return
    if not GH_REPO or not GH_TOKEN:
        fail("缺少 GITHUB_REPOSITORY 或 GITHUB_TOKEN，无法创建提醒 issue。", 5)

    existing_bodies = "\n".join((issue.get("body") or "") for issue in open_alert_issues())
    for item in todo:
        if marker(item["value"]) in existing_bodies:
            log("已存在对应的提醒 issue，跳过创建: " + item["name"])
            continue

        title = "[DCV] {0} 需要完成证书续订验证".format(ZONE_NAME)
        lines = [
            marker(item["value"]),
            "Cloudflare 检测到 `{0}` 的证书需要重新做域名控制验证（DCV）。".format(ZONE_NAME),
            "",
            "| 项目 | 值 |",
            "| --- | --- |",
            "| 证书包 | `{0}` |".format(item["pack_id"]),
            "| 验证方式 | `{0}` |".format(item["method"]),
            "| DNS 记录名 | `{0}` |".format(item["name"]),
            "| DNS 记录值 | `{0}` |".format(item["value"]),
            "",
            "工作流随后会自动把这条 TXT 写入 Cloudflare DNS 并触发验证，正常情况下无需任何人工操作。",
            "如果本次运行失败，请手动添加上面的 TXT 记录，或到 Actions 查看日志。",
        ]
        if NOTIFY_MENTION:
            lines += ["", "@" + NOTIFY_MENTION]

        try:
            issue = gh("POST", "/repos/{0}/issues".format(GH_REPO), {"title": title, "body": "\n".join(lines)})
        except RuntimeError as exc:
            fail("创建提醒 issue 失败，本次运行终止（不执行续订）: " + str(exc), 5)
        log("已创建提醒 issue #{0}: {1}".format(issue.get("number"), title))


def close_resolved(todo):
    if not (NOTIFY_ISSUE and CLOSE_ISSUES) or DRY_RUN or not GH_REPO or not GH_TOKEN:
        return
    alive = {marker(item["value"]) for item in todo}
    for issue in open_alert_issues():
        found = MARKER_RE.findall(issue.get("body") or "")
        if not found or any(item in alive for item in found):
            continue
        try:
            gh(
                "POST",
                "/repos/{0}/issues/{1}/comments".format(GH_REPO, issue["number"]),
                {"body": "DCV 已完成，证书可以正常续订，自动关闭本条提醒。"},
            )
            gh(
                "PATCH",
                "/repos/{0}/issues/{1}".format(GH_REPO, issue["number"]),
                {"state": "closed", "state_reason": "completed"},
            )
        except RuntimeError as exc:
            log("关闭 issue #{0} 失败（忽略）: {1}".format(issue["number"], exc))
            continue
        log("已自动关闭 issue #{0}".format(issue["number"]))


def ensure_txt(zid, item):
    global changed
    found = cf(
        "GET",
        "/zones/{0}/dns_records?type=TXT&name={1}&per_page=100".format(zid, item["name"]),
    ).get("result") or []
    for record in found:
        if record and unquote(record.get("content")) == item["value"]:
            log("TXT 已存在，跳过: " + item["name"])
            return
    if DRY_RUN:
        log("[DryRun] 将创建 TXT {0} = {1}".format(item["name"], item["value"]))
        return
    cf(
        "POST",
        "/zones/{0}/dns_records".format(zid),
        {
            "type": "TXT",
            "name": item["name"],
            "content": item["value"],
            "ttl": TXT_TTL,
            "comment": "auto DCV (dcv_auto.py)",
        },
    )
    changed += 1
    log("已创建 TXT {0} = {1}".format(item["name"], item["value"]))


def trigger(zid, todo):
    if NO_TRIGGER:
        log("NO_TRIGGER 已启用，跳过触发验证。")
        return
    for pack_id in sorted({item["pack_id"] for item in todo}):
        method = next((item["method"] for item in todo if item["pack_id"] == pack_id), "txt")
        if DRY_RUN:
            log("[DryRun] 将触发重新验证 pack={0} method={1}".format(pack_id, method))
            continue
        cf("PATCH", "/zones/{0}/ssl/verification/{1}".format(zid, pack_id), {"validation_method": method})
        log("已触发重新验证 pack={0} method={1}".format(pack_id, method))


def cleanup(zid, todo):
    global changed
    if not CLEANUP:
        return
    needed = {item["value"] for item in todo}
    records = cf(
        "GET",
        "/zones/{0}/dns_records?type=TXT&search=_acme-challenge&per_page=100".format(zid),
    ).get("result") or []
    now = datetime.datetime.now(datetime.timezone.utc)
    for record in records:
        if not record:
            continue
        name = record.get("name") or ""
        if not name.startswith("_acme-challenge."):
            continue
        if unquote(record.get("content")) in needed:
            continue
        try:
            created = datetime.datetime.fromisoformat((record.get("created_on") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        age_days = (now - created).total_seconds() / 86400.0
        if age_days < CLEANUP_AGE_DAYS:
            continue
        if DRY_RUN:
            log("[DryRun] 将删除过期 TXT {0}（存在 {1:.0f} 天）".format(name, age_days))
            continue
        cf("DELETE", "/zones/{0}/dns_records/{1}".format(zid, record.get("id")))
        changed += 1
        log("已删除过期 TXT {0}（存在 {1:.0f} 天）".format(name, age_days))


def main():
    if not TOKEN:
        fail("环境变量 CLOUDFLARE_API_TOKEN 未设置", 2)

    zones = cf("GET", "/zones?name={0}&per_page=1".format(ZONE_NAME)).get("result") or []
    if not zones:
        fail("找不到域名 {0}（或 token 缺少 Zone:Read 权限）".format(ZONE_NAME), 3)
    zid = zones[0]["id"]
    log("开始处理 {0} (zone_id={1})".format(ZONE_NAME, zid))

    todo = collect_pending(zid)
    if not todo:
        log("没有待处理的 DCV 记录。")
        close_resolved([])
        return
    log("发现待处理 DCV 记录 {0} 条".format(len(todo)))

    notify_pending(todo)
    for item in todo:
        ensure_txt(zid, item)
    trigger(zid, todo)
    cleanup(zid, todo)
    log("处理完成，本次变更 {0} 项".format(changed))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        fail(str(exc), 4)
