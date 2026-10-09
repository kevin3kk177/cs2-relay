#!/usr/bin/env python3
"""从 PandaScore 取 CS2 赛程/比分，写成中继 JSON。

给 GitHub Actions 定时执行用。**只用标准库**，不需要 pip install，
所以跑得又快又不会有依赖问题。

输出 `data/cs2.json`：

{
  "fetched_at": "2026-10-08T05:00:00+00:00",
  "source": "pandascore",
  "past_days": 4,
  "upcoming_days": 3,
  "counts": {"running": 1, "upcoming": 118, "past": 205},
  "running":  [ ...精简后的 match 对象... ],
  "upcoming": [ ... ],
  "past":     [ ... ]
}

两个刻意的设计：

1. **只保留用得上的字段**（`slim()`）。PandaScore 的原始对象很大，
   全存下来会让仓库和每次下载都变得臃肿；精简后体积约为原来的十分之一。
2. **输出是确定性的**：列表按 id 排序、键名排序。数据没变就生成完全一样的
   文件，工作流里 `git diff --cached --quiet` 就会跳过提交，不会每两小时
   往仓库里塞一个空提交。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

BASE = "https://api.pandascore.co"
OUT_PATH = Path("data/cs2.json")
PAST_DAYS = 4
UPCOMING_DAYS = 3
PAGE_SIZE = 100
MAX_PAGES = 5
RETRIES = 4


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(path: str, params: Dict[str, Any], token: str) -> List[Dict[str, Any]]:
    """带重试的 GET。返回列表（PandaScore 这些接口都返回数组）。"""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "cs2-relay/1.0",
        },
    )

    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            if not isinstance(data, list):
                raise RuntimeError(f"{path} 返回的不是数组：{str(data)[:200]}")
            return data
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            last_error = RuntimeError(f"HTTP {exc.code} {body}")
            # 401/403/404 是配置问题，重试没意义
            if exc.code in (401, 403, 404):
                raise last_error from exc
        except Exception as exc:  # noqa: BLE001 - 网络类问题一律重试
            last_error = exc

        if attempt < RETRIES:
            wait = min(2 ** (attempt - 1), 8)
            print(f"  [!] {path} 第 {attempt}/{RETRIES} 次失败：{last_error}；{wait} 秒后重试")
            time.sleep(wait)

    raise RuntimeError(f"{path} 取数失败：{last_error}")


def fetch_all_pages(path: str, params: Dict[str, Any], token: str) -> List[Dict[str, Any]]:
    collected: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = fetch(path, {**params, "per_page": PAGE_SIZE, "page": page}, token)
        collected.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return collected


def slim(raw: Dict[str, Any]) -> Dict[str, Any]:
    """只留下组装日报真正用得上的字段。"""
    opponents = []
    for entry in (raw.get("opponents") or [])[:2]:
        opponent = (entry or {}).get("opponent") or {}
        opponents.append(
            {
                "opponent": {
                    "id": opponent.get("id"),
                    "name": opponent.get("name"),
                    "acronym": opponent.get("acronym"),
                    # 队标地址：下一步会被 mirror_logos 换成仓库内的相对路径
                    "image_url": opponent.get("image_url"),
                }
            }
        )

    results = []
    for item in raw.get("results") or []:
        if isinstance(item, dict):
            results.append({"team_id": item.get("team_id"), "score": item.get("score")})

    tournament = raw.get("tournament") or {}
    serie = raw.get("serie") or {}
    league = raw.get("league") or {}

    return {
        "id": raw.get("id"),
        "begin_at": raw.get("begin_at"),
        "scheduled_at": raw.get("scheduled_at"),
        "status": raw.get("status"),
        "number_of_games": raw.get("number_of_games"),
        "winner_id": raw.get("winner_id"),
        "opponents": opponents,
        "results": results,
        "tournament": {"name": tournament.get("name"), "tier": tournament.get("tier")},
        "serie": {"name": serie.get("name"), "full_name": serie.get("full_name"), "tier": serie.get("tier")},
        "league": {"name": league.get("name")},
    }


# ----------------------------------------------------------------- 队标镜像
# 为什么要镜像：机器人所在网络访问不了 cdn.pandascore.co（会被重置），
# 但 GitHub Actions 能访问。所以让 Actions 把队标下载进仓库，
# 机器人直接从 GitHub 取 —— 和赛程数据走同一条路。
LOGO_DIR = Path("logos")
_LOGO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _logo_name(team_id: Any, url: str) -> str:
    """确定性的文件名：同一个队永远同一个名字，避免仓库里堆重复文件。"""
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext not in _LOGO_EXTS:
        ext = ".png"
    safe = str(team_id).replace("/", "_").replace("\\", "_")
    return f"{safe}{ext}"


def download(url: str, retries: int = 3) -> bytes | None:
    """下载一个文件，失败返回 None（队标缺失不该让整次取数失败）。"""
    request = urllib.request.Request(url, headers={"User-Agent": "cs2-relay/1.0"})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - 网络问题一律重试
            if attempt >= retries:
                print(f"    [!] 下载失败 {url}：{exc}")
                return None
            time.sleep(min(2 ** (attempt - 1), 4))
    return None


def mirror_logos(groups: List[List[Dict[str, Any]]]) -> None:
    """把出现过的战队队标下载到 logos/，并把 JSON 里的 image_url 换成仓库相对路径。

    已经存在的文件会跳过，所以日常只会有「新战队」的少量新增，仓库不会一直膨胀。
    """
    LOGO_DIR.mkdir(parents=True, exist_ok=True)

    # 收集所有引用到的队标（按 team_id 去重）
    wanted: Dict[Any, str] = {}
    for items in groups:
        for item in items:
            for entry in item.get("opponents") or []:
                opponent = entry.get("opponent") or {}
                url = opponent.get("image_url")
                team_id = opponent.get("id")
                if url and team_id is not None:
                    wanted[team_id] = url

    downloaded = skipped = failed = 0
    for team_id, url in sorted(wanted.items(), key=lambda kv: str(kv[0])):
        name = _logo_name(team_id, url)
        target = LOGO_DIR / name
        if not target.exists():
            data = download(url)
            if data and len(data) > 100:
                target.write_bytes(data)
                downloaded += 1
            else:
                failed += 1
        else:
            skipped += 1

        # 不管下载成功与否，都把路径写进 JSON：机器人取不到时会退化成字母占位图
        relative = f"{LOGO_DIR.as_posix()}/{name}"
        for items in groups:
            for item in items:
                for entry in item.get("opponents") or []:
                    opponent = entry.get("opponent") or {}
                    if opponent.get("id") == team_id:
                        opponent["logo"] = relative
                        opponent.pop("image_url", None)

    print(f"  [*] 队标：新下载 {downloaded} 个 / 已存在 {skipped} 个 / 失败 {failed} 个")



def clean(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """精简 + 去重 + 按 id 排序（保证输出确定性）。"""
    by_id: Dict[Any, Dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, dict) or raw.get("id") is None:
            continue
        by_id[raw["id"]] = slim(raw)
    return [by_id[key] for key in sorted(by_id, key=lambda x: str(x))]


def main() -> int:
    token = (os.environ.get("PANDASCORE_TOKEN") or "").strip()
    if not token:
        print("[x] 没有拿到 PANDASCORE_TOKEN。")
        print("    请到仓库 Settings -> Secrets and variables -> Actions 里新建一个 secret，")
        print("    名字填 PANDASCORE_TOKEN，值填你的 PandaScore token。")
        return 1

    now = datetime.now(timezone.utc)
    result: Dict[str, Any] = {
        "fetched_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "source": "pandascore",
        "past_days": PAST_DAYS,
        "upcoming_days": UPCOMING_DAYS,
    }

    try:
        print("[*] 取正在进行的比赛...")
        result["running"] = clean(fetch("/csgo/matches/running", {"per_page": PAGE_SIZE}, token))

        print(f"[*] 取未来 {UPCOMING_DAYS} 天的赛程...")
        result["upcoming"] = clean(
            fetch_all_pages(
                "/csgo/matches/upcoming",
                {
                    "range[begin_at]": f"{iso(now)},{iso(now + timedelta(days=UPCOMING_DAYS))}",
                    "sort": "begin_at",
                },
                token,
            )
        )

        print(f"[*] 取过去 {PAST_DAYS} 天的赛果...")
        result["past"] = clean(
            fetch_all_pages(
                "/csgo/matches/past",
                {
                    "range[begin_at]": f"{iso(now - timedelta(days=PAST_DAYS))},{iso(now)}",
                    "sort": "begin_at",
                },
                token,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 取数失败，保留上一次的数据不覆盖：{exc}")
        return 1

    result["counts"] = {
        "running": len(result["running"]),
        "upcoming": len(result["upcoming"]),
        "past": len(result["past"]),
    }

    # 全都为空通常是 token 失效或接口变了，这时宁可报错也别把空数据当成真的发布出去
    if result["counts"]["upcoming"] == 0 and result["counts"]["past"] == 0:
        print("[x] 一场比赛都没取到，判定为异常，不覆盖已有数据。请检查 token 是否有效。")
        return 1

    print("[*] 镜像战队队标...")
    mirror_logos([result["running"], result["upcoming"], result["past"]])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    size_kb = OUT_PATH.stat().st_size / 1024
    print(
        f"[✓] 已写入 {OUT_PATH}（{size_kb:.1f} KB）："
        f"进行中 {result['counts']['running']} 场 / "
        f"待开赛 {result['counts']['upcoming']} 场 / "
        f"已结束 {result['counts']['past']} 场"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
