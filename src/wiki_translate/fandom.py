"""MediaWiki API 客户端：抓取 + 推送。基于 httpx 实现。"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import PublishConfig

log = logging.getLogger(__name__)


_HTTP_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
)


class UploadError(RuntimeError):
    """上传失败时抛出，携带结构化错误信息便于上层判定幂等错误。"""

    def __init__(self, message: str, *, code: str = "", info: str = "", body: dict | None = None):
        super().__init__(message)
        self.code = code
        self.info = info
        self.body = body or {}


class FandomClient:
    """读端：列出页面、抓取 wikitext。"""

    def __init__(self, api_url: str, user_agent: str, timeout: float = 30.0) -> None:
        self.api_url = api_url
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> FandomClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @_HTTP_RETRY
    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "format": "json", "formatversion": "2"}
        resp = self.client.get(self.api_url, params=params)
        resp.raise_for_status()
        return resp.json()

    def list_category(self, category: str, ns: int = 0, limit: int = 50) -> list[str]:
        titles: list[str] = []
        cmcontinue: str | None = None
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": f"Category:{category}",
                "cmnamespace": ns,
                "cmlimit": min(500, limit - len(titles)),
                "cmtype": "page",
            }
            if cmcontinue:
                params["cmcontinue"] = cmcontinue
            data = self._get(params)
            for m in data.get("query", {}).get("categorymembers", []):
                titles.append(m["title"])
                if len(titles) >= limit:
                    return titles
            cmcontinue = data.get("continue", {}).get("cmcontinue")
            if not cmcontinue:
                break
        return titles

    def list_all_pages(
        self,
        namespaces: list[int],
        limit: int = 0,
        filter_redirects: str = "nonredirects",
    ) -> list[str]:
        titles: list[str] = []
        for ns in namespaces:
            apcontinue: str | None = None
            while True:
                if limit > 0 and len(titles) >= limit:
                    return titles
                remaining = (limit - len(titles)) if limit > 0 else 500
                params: dict[str, Any] = {
                    "action": "query",
                    "list": "allpages",
                    "apnamespace": ns,
                    "apfilterredir": filter_redirects,
                    "aplimit": min(500, remaining) if limit > 0 else 500,
                }
                if apcontinue:
                    params["apcontinue"] = apcontinue
                data = self._get(params)
                for m in data.get("query", {}).get("allpages", []):
                    titles.append(m["title"])
                    if limit > 0 and len(titles) >= limit:
                        return titles
                apcontinue = data.get("continue", {}).get("apcontinue")
                if not apcontinue:
                    break
        return titles

    def fetch_page(self, title: str) -> tuple[str, int] | None:
        params = {
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvprop": "ids|content",
            "rvslots": "main",
        }
        data = self._get(params)
        pages = data.get("query", {}).get("pages", [])
        if not pages:
            return None
        page = pages[0]
        if page.get("missing"):
            return None
        revs = page.get("revisions", [])
        if not revs:
            return None
        rev = revs[0]
        content = rev.get("slots", {}).get("main", {}).get("content", "")
        revid = rev.get("revid", 0)
        return content, revid


class FandomPublisher:
    """写端：用 BotPassword 登录目标 wiki 并通过 action=edit 推送。"""

    def __init__(self, cfg: PublishConfig, user_agent: str, timeout: float = 60.0) -> None:
        self.cfg = cfg
        self.client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        self._csrf: str | None = None

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> FandomPublisher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @_HTTP_RETRY
    def _post(self, data: dict[str, Any]) -> dict[str, Any]:
        data = {**data, "format": "json", "formatversion": "2"}
        resp = self.client.post(self.cfg.api_url, data=data)
        resp.raise_for_status()
        return resp.json()

    @_HTTP_RETRY
    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "format": "json", "formatversion": "2"}
        resp = self.client.get(self.cfg.api_url, params=params)
        resp.raise_for_status()
        return resp.json()

    def login(self) -> None:
        if not self.cfg.username or not self.cfg.password:
            raise RuntimeError(
                "缺少 FANDOM_BOT_USER / FANDOM_BOT_PASSWORD（请使用 BotPassword 而非账号密码）"
            )
        token_data = self._get({"action": "query", "meta": "tokens", "type": "login"})
        login_token = token_data["query"]["tokens"]["logintoken"]
        login_resp = self._post(
            {
                "action": "login",
                "lgname": self.cfg.username,
                "lgpassword": self.cfg.password,
                "lgtoken": login_token,
            }
        )
        if login_resp.get("login", {}).get("result") != "Success":
            raise RuntimeError(f"Wiki 登录失败: {login_resp}")
        csrf_data = self._get({"action": "query", "meta": "tokens", "type": "csrf"})
        self._csrf = csrf_data["query"]["tokens"]["csrftoken"]
        if not self._csrf or self._csrf == "+\\":
            raise RuntimeError("获取 CSRF token 失败：登录态可能未生效")
        log.info("已以 %s 登录目标 wiki", self.cfg.username)

    def map_title(self, source_title: str) -> str:
        if source_title in self.cfg.title_map:
            return self.cfg.title_map[source_title]
        return f"{self.cfg.title_prefix}{source_title}{self.cfg.title_suffix}"

    def fetch_page(self, title: str) -> tuple[str, int] | None:
        """读取目标 wiki 上的页面（无需登录）。返回 (wikitext, revid) 或 None（页面不存在）。"""
        params = {
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvprop": "ids|content",
            "rvslots": "main",
        }
        data = self._get(params)
        pages = data.get("query", {}).get("pages", [])
        if not pages:
            return None
        page = pages[0]
        if page.get("missing"):
            return None
        revs = page.get("revisions", [])
        if not revs:
            return None
        rev = revs[0]
        content = rev.get("slots", {}).get("main", {}).get("content", "")
        revid = rev.get("revid", 0)
        return content, revid

    def edit(self, title: str, content: str) -> dict[str, Any]:
        if not self._csrf:
            raise RuntimeError("尚未登录，无法 edit")
        data: dict[str, Any] = {
            "action": "edit",
            "title": title,
            "text": content,
            "summary": self.cfg.summary,
            "token": self._csrf,
        }
        if self.cfg.bot_flag:
            data["bot"] = "1"
        if self.cfg.minor:
            data["minor"] = "1"

        resp = self._post(data)
        if "error" in resp:
            raise RuntimeError(f"edit error: {resp['error']}")
        edit_info = resp.get("edit", {})
        if edit_info.get("result") != "Success":
            raise RuntimeError(f"edit not success: {resp}")
        return edit_info

    @_HTTP_RETRY
    def upload(self, filename: str, blob: bytes, comment: str = "") -> dict[str, Any]:
        """单次上传文件到目标 wiki。filename 不含 File: 前缀。"""
        if not self._csrf:
            raise RuntimeError("尚未登录，无法 upload")
        data: dict[str, Any] = {
            "action": "upload",
            "filename": filename,
            "comment": comment or self.cfg.summary,
            "token": self._csrf,
            "ignorewarnings": "1",
            "format": "json",
            "formatversion": "2",
        }
        files = {"file": (filename, blob)}
        resp = self.client.post(self.cfg.api_url, data=data, files=files)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise UploadError(
                f"upload error: {err}",
                code=err.get("code", ""),
                info=err.get("info", ""),
                body=body,
            )
        info = body.get("upload", {})
        if info.get("result") not in ("Success", "Warning"):
            raise UploadError(f"upload not success: {body}", body=body)
        return info

    def upload_chunked(
        self,
        filename: str,
        blob: bytes,
        *,
        chunk_size: int = 4 * 1024 * 1024,
        comment: str = "",
    ) -> dict[str, Any]:
        """分块上传：先按 chunk_size 通过 stash 累积，再一次性 commit。

        适合 >5 MB 文件；网络抖动或服务端 nginx 上传体积限制都能更鲁棒。
        """
        if not self._csrf:
            raise RuntimeError("尚未登录，无法 upload")
        if not blob:
            raise RuntimeError("upload_chunked: blob 为空")

        size = len(blob)
        offset = 0
        filekey: str | None = None
        chunk_idx = 0
        while offset < size:
            end = min(offset + chunk_size, size)
            chunk = blob[offset:end]
            chunk_idx += 1

            data: dict[str, Any] = {
                "action": "upload",
                "filename": filename,
                "stash": "1",
                "filesize": str(size),
                "offset": str(offset),
                "ignorewarnings": "1",
                "token": self._csrf,
                "format": "json",
                "formatversion": "2",
            }
            if filekey:
                data["filekey"] = filekey
            files = {"chunk": (filename, chunk)}
            resp = self.client.post(self.cfg.api_url, data=data, files=files)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise RuntimeError(f"upload chunk {chunk_idx} error: {body['error']}")
            info = body.get("upload", {})
            result = info.get("result")
            if result not in ("Continue", "Success", "Warning"):
                raise RuntimeError(f"upload chunk {chunk_idx} unexpected: {body}")
            filekey = info.get("filekey") or filekey
            if not filekey:
                raise RuntimeError(f"upload chunk {chunk_idx} 没返回 filekey: {body}")
            offset = end
            log.debug(
                "upload chunk %d/%d ok offset=%d/%d",
                chunk_idx,
                (size + chunk_size - 1) // chunk_size,
                offset,
                size,
            )

        # 全部分块就绪，commit
        commit_data: dict[str, Any] = {
            "action": "upload",
            "filename": filename,
            "filekey": filekey,
            "comment": comment or self.cfg.summary,
            "ignorewarnings": "1",
            "token": self._csrf,
            "format": "json",
            "formatversion": "2",
        }
        resp = self.client.post(self.cfg.api_url, data=commit_data)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            err = body["error"]
            raise UploadError(
                f"upload commit error: {err}",
                code=err.get("code", ""),
                info=err.get("info", ""),
                body=body,
            )
        info = body.get("upload", {})
        if info.get("result") not in ("Success", "Warning"):
            raise UploadError(f"upload commit not success: {body}", body=body)
        return info

    def logout(self) -> None:
        try:
            if self._csrf:
                self._post({"action": "logout", "token": self._csrf})
        except Exception:  # noqa: BLE001
            pass
