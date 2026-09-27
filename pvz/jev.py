"""TypeSafe / Jev 客户端。

本机到 api.typesafe.ai 的 TLS 被中间设备拦截：Python 的 ssl 会抛
UNEXPECTED_EOF，curl 的 schannel 会抛 CRYPT_E_REVOCATION_OFFLINE。
可用组合是 **curl + --ssl-no-revoke + --http1.1 + 重试**（实测 2-3 次内成功），
所以传输层一律走 subprocess 调 curl，Python 只负责 JSON 逻辑。

Jev 是 System One 决策模型，返回的是类型化判断（choice / noul / score），
不生成自由文本 —— 所以问题必须设计成"从给定选项里选一个"。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

API_URL = "https://api.typesafe.ai/v1/systemone"
MODELS_URL = "https://api.typesafe.ai/v1/models"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "jev_api_key")


class JevError(RuntimeError):
    pass


def load_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    env = os.environ.get("TYPESAFE_API_KEY")
    if env:
        return env.strip()
    try:
        with open(KEY_FILE, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except OSError:
        pass
    raise JevError(
        f"找不到 API key。请设置环境变量 TYPESAFE_API_KEY，或写入 {KEY_FILE}"
    )


def save_api_key(key: str, path: str = KEY_FILE) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(key.strip() + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------- 问题构造
def noul(instructions: str, true_desc: str = "是", false_desc: str = "否") -> dict:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": true_desc, "false": false_desc},
    }


def choice(instructions: str, options: dict[str, str]) -> dict:
    """options: {选项键: 选项说明}，选项数量上限 255。"""
    if not options:
        raise ValueError("choice 至少需要一个选项")
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict:
    if not (2 <= len(levels) <= 10):
        raise ValueError("score 需要 2..10 个等级")
    return {"type": "score", "instructions": instructions, "criteria": levels}


# ---------------------------------------------------------------- 结果
@dataclass
class JevAnswer:
    qid: str
    kind: str
    raw: dict
    latency_s: float = 0.0

    @property
    def choice(self) -> str | None:
        return self.raw.get("choice")

    @property
    def probabilities(self) -> dict:
        return self.raw.get("probabilities") or {}

    @property
    def confidence(self) -> float | None:
        return self.raw.get("confidence")

    @property
    def noul(self) -> float | None:
        return self.raw.get("noul")

    @property
    def score(self) -> int | None:
        return self.raw.get("score")

    @property
    def yes(self) -> bool:
        v = self.noul
        return bool(v is not None and v >= 0.5)

    def top(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def summary(self) -> str:
        if self.kind == "choice":
            top = ", ".join(f"{k}={v:.2f}" for k, v in self.top(3))
            return f"{self.choice} (conf={self.confidence:.2f}; {top})" if self.confidence is not None else f"{self.choice} ({top})"
        if self.kind == "noul":
            return f"{self.noul:.3f} -> {'是' if self.yes else '否'}"
        if self.kind == "score":
            return f"score={self.score} (conf={self.confidence})"
        return json.dumps(self.raw, ensure_ascii=False)


@dataclass
class JevResponse:
    answers: dict[str, JevAnswer] = field(default_factory=dict)
    model: str = ""
    latency_s: float = 0.0
    attempts: int = 0
    error: str | None = None

    def __getitem__(self, qid: str) -> JevAnswer:
        return self.answers[qid]

    def get(self, qid: str) -> JevAnswer | None:
        return self.answers.get(qid)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answers)


# ---------------------------------------------------------------- 客户端
class JevClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "jev-latest",
        timeout: int = 20,   # 2026-09-27：45s×3次重试最坏阻塞主循环 5 分钟（审查项）
        retries: int = 3,
        curl: str = "curl",
    ):
        self.api_key = load_api_key(api_key)
        self.model = model
        self.timeout = timeout
        self.retries = max(1, retries)
        self.curl = curl
        self.last_response: JevResponse | None = None
        self.total_calls = 0
        self.total_latency = 0.0

    # -- 传输 -----------------------------------------------------------
    def _post(self, payload: dict) -> tuple[str, int]:
        """返回 (body, http_code)。JSON 写临时文件再 --data @file，避免 CJK 在 shell 里被转坏。"""
        fd, path = tempfile.mkstemp(prefix="jev_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            cmd = [
                self.curl, "-sS", "--ssl-no-revoke", "--http1.1",
                "--max-time", str(self.timeout),
                "-w", "\n%{http_code}",
                "-X", "POST", API_URL,
                "-H", f"Authorization: Bearer {self.api_key}",
                "-H", "Content-Type: application/json",
                "--data", f"@{path}",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 20)
            out = proc.stdout or ""
            body, _, code = out.rpartition("\n")
            if not code:
                return (proc.stderr or out).strip(), 0
            return body.strip(), int(code.strip() or 0)
        except subprocess.TimeoutExpired:
            return "curl 超时", 0
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- 主入口 ---------------------------------------------------------
    def ask(self, state, questions: dict[str, dict], keep_raw: bool = False) -> JevResponse:
        if not questions:
            return JevResponse(error="没有要问的问题")
        payload = {"state": state, "model": self.model, "questions": questions}

        last_err = ""
        for attempt in range(1, self.retries + 1):
            t0 = time.time()
            body, code = self._post(payload)
            dt = time.time() - t0
            self.total_calls += 1
            self.total_latency += dt
            if code == 200:
                try:
                    data = json.loads(body)
                except ValueError as exc:
                    last_err = f"响应不是合法 JSON: {exc}; body={body[:300]}"
                    time.sleep(0.6 * attempt)
                    continue
                resp = self._parse(data, questions, dt, attempt)
                self.last_response = resp
                return resp
            last_err = f"HTTP {code}: {body[:300]}"
            if code in (400, 401, 403, 404):
                break  # 认证/请求错误，重试无意义
            if attempt < self.retries:
                time.sleep(0.6 * attempt)  # 末次失败后不再白等（2026-09-27 审查）

        resp = JevResponse(error=last_err or "未知错误")
        self.last_response = resp
        return resp

    def _parse(self, data: dict, questions: dict[str, dict], dt: float, attempt: int) -> JevResponse:
        resp = JevResponse(latency_s=dt, attempts=attempt, model=data.get("model", ""))
        # 兼容 {answers: {...}} / 直接 {qid: {...}} / {results: {...}}
        block = data.get("answers") or data.get("results") or data
        if isinstance(block, dict):
            for qid, spec in questions.items():
                raw = block.get(qid)
                if isinstance(raw, dict) and "answer" in raw and isinstance(raw["answer"], dict):
                    raw = raw["answer"]
                if isinstance(raw, dict):
                    resp.answers[qid] = JevAnswer(qid=qid, kind=spec.get("type", "?"), raw=raw, latency_s=dt)
        if not resp.answers:
            resp.error = f"响应里没有可解析的答案: {json.dumps(data, ensure_ascii=False)[:400]}"
        return resp

    # -- 便捷 -----------------------------------------------------------
    def models(self) -> dict:
        proc = subprocess.run(
            [self.curl, "-sS", "--ssl-no-revoke", "--http1.1", "--max-time", "30",
             "-w", "\n%{http_code}", MODELS_URL,
             "-H", f"Authorization: Bearer {self.api_key}"],
            capture_output=True, text=True, timeout=50,
        )
        body, _, code = (proc.stdout or "").rpartition("\n")
        if code.strip() != "200":
            raise JevError(f"GET /v1/models -> HTTP {code.strip()}: {body[:200]}")
        return json.loads(body)

    def stats(self) -> dict:
        return {
            "calls": self.total_calls,
            "total_latency_s": round(self.total_latency, 2),
            "avg_latency_s": round(self.total_latency / self.total_calls, 2) if self.total_calls else 0.0,
        }


class DecisionLog:
    """把每一次"状态 -> 提问 -> Jev 回答 -> 执行"落成 JSONL，供 HTML 复盘。"""

    def __init__(self, path: str):
        self.path = path
        self.event_path = os.path.splitext(path)[0] + '.events.jsonl'
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def append(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        out = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except (OSError, ValueError):
            pass
        return out

    def event(self, name, **details):
        record = dict(record_type='event', schema_version=2, event=name,
                      t=time.time(), iso=time.strftime('%Y-%m-%d %H:%M:%S'), **details)
        with open(self.event_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
