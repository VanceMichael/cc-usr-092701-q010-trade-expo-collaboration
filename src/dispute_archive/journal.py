"""仅追加的哈希链事件日志。

日志是本服务唯一的事实来源：状态机只通过重放日志恢复。每条记录都包含
前一条记录的 SHA-256，任何事后插入、删除或修改都会让链校验失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import ChainIntegrityError

GENESIS = "0" * 64


def canonical(value: Any) -> bytes:
    """稳定序列化：键排序、无空白，保证同一语义负载哈希一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    timestamp: str
    event_type: str
    payload: dict
    event_id: str
    actor: str
    prev_hash: str
    entry_hash: str

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload": self.payload,
            "event_id": self.event_id,
            "actor": self.actor,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


@dataclass
class Journal:
    """内存哈希链日志，可导出 JSONL、从 JSONL 重建并校验完整性。"""

    _entries: list[Entry] = field(default_factory=list)

    def append(
        self,
        *,
        timestamp: str,
        event_type: str,
        payload: dict,
        event_id: str,
        actor: str,
    ) -> Entry:
        prev_hash = self._entries[-1].entry_hash if self._entries else GENESIS
        seq = len(self._entries) + 1
        body = {
            "seq": seq,
            "timestamp": timestamp,
            "event_type": event_type,
            "payload": payload,
            "event_id": event_id,
            "actor": actor,
            "prev_hash": prev_hash,
        }
        entry = Entry(**body, entry_hash=digest(body))
        self._entries.append(entry)
        return entry

    def __iter__(self):
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def head(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def replay(self) -> Iterable[Entry]:
        """按顺序产出条目；调用方据此重建派生状态。"""
        yield from self._entries

    def export_jsonl(self) -> str:
        return "".join(json.dumps(e.as_dict(), ensure_ascii=False, sort_keys=True) + "\n" for e in self._entries)

    @classmethod
    def from_jsonl(cls, text: str, *, verify: bool = True) -> "Journal":
        journal = cls()
        for line_no, raw in enumerate(text.splitlines(), start=1):
            raw = raw.strip()
            if not raw:
                continue
            data = json.loads(raw)
            expected = {
                "seq": data["seq"],
                "timestamp": data["timestamp"],
                "event_type": data["event_type"],
                "payload": data["payload"],
                "event_id": data["event_id"],
                "actor": data["actor"],
                "prev_hash": data["prev_hash"],
            }
            if verify:
                if data["seq"] != line_no:
                    raise ChainIntegrityError(f"第 {line_no} 行序号不连续")
                want_prev = journal._entries[-1].entry_hash if journal._entries else GENESIS
                if data["prev_hash"] != want_prev:
                    raise ChainIntegrityError(f"第 {line_no} 行前驱哈希不匹配")
                if digest(expected) != data["entry_hash"]:
                    raise ChainIntegrityError(f"第 {line_no} 行内容哈希不匹配")
            journal._entries.append(Entry(**expected, entry_hash=data["entry_hash"]))
        return journal
